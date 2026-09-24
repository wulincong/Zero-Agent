"""事件总线：内核与外部（CLI / 日志 / 记忆）之间的解耦通道。

设计目标
--------
把"内核主动回调外部"（on_tool_call / on_tool_output）反转为
"内核发布事件、外部订阅事件"，从而：

1. 解耦：内核只负责 emit，不关心谁在听、怎么呈现；
2. 可观测：所有状态变化都是事件，天然可审计、可回放；
3. 可扩展：新增行为（日志、审批、多 agent）只需加订阅者，不动主循环；
4. 为异步铺路：阶段 2 起事件派发将改为 async，本模块已预留 async 接口。

派发策略
--------
**顺序 await，不并发派发**——保证事件处理顺序与产生顺序一致，
避免输出乱序（如工具结果先于工具调用提示打印）。

兼容性
------
本模块同时提供 `emit`（async）与 `emit_sync`（同步）两个入口：
- 阶段 1（同步内核）使用 `emit_sync`；
- 阶段 2（异步内核）改用 `await emit`。
两者派发逻辑一致，仅调用方式不同。

会话代次（阶段 3 引入）
----------------------
中断/回滚后，工作线程可能仍有"在途事件"（如 `emit_threadsafe` 投递、
尚未派发的 ToolResultEvent）。若不加区分地派发，会污染新一轮对话
（例如打印出已被丢弃的工具输出）。

为此引入**会话代次**：内核每轮 `achat()` 开始时调用 `new_generation()`
递增代次，事件经 `stamp()` 打上代次标识；派发时 `_is_stale()` 会丢弃
代次不符的旧事件。未开启代次或未打标识的事件不受影响（向后兼容）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Type


# ----------------------------------------------------------------------
# 事件基类与具体事件类型
# ----------------------------------------------------------------------
@dataclass
class Event:
    """所有事件的基类。

    Attributes:
        timestamp: 事件产生时刻（time.time()），用于排序与审计。
        session_id: 会话代次标识（阶段 3 起由 EventBus.stamp 填充）。
            用于区分不同轮次，使中断后残留的旧事件被丢弃。
    """

    timestamp: float = field(default_factory=time.time)
    session_id: str = ""


@dataclass
class UserMessageEvent(Event):
    """用户提交了一条消息。"""

    content: str = ""


@dataclass
class ModelStartEvent(Event):
    """模型开始响应（请求已发出）。"""


@dataclass
class ModelChunkEvent(Event):
    """模型流式输出的一个增量片段（可选订阅，用于实时渲染）。"""

    text: str = ""


@dataclass
class ModelEndEvent(Event):
    """模型响应完成。

    Attributes:
        message: 聚合后的 AIMessage（含 content 与 tool_calls）。
    """

    message: Any = None


@dataclass
class ModelStopEvent(Event):
    """模型阶段结束（无论成功、失败、中断还是超时）。

    与 ModelEndEvent 的区别：
      - ModelEndEvent 表示"成功拿到完整响应"，携带 message，仅在成功路径 emit；
      - ModelStopEvent 是**兜底信号**，在模型调用的 finally 中 emit，
        保证订阅者（如 CLI 的等待指示器）无论走哪条路径都能收到"结束"通知，
        不会因中断/超时/异常而残留转圈动画。

    Attributes:
        ok: 是否成功完成（False 表示中断/超时/异常）。
    """

    ok: bool = True


@dataclass
class ToolCallEvent(Event):
    """模型决定调用某个工具。

    Attributes:
        name: 工具名。
        args: 工具参数；模型刚开始生成时为 None（参数尚未聚合完成）。
    """

    name: str = ""
    args: Any = None


@dataclass
class ToolResultEvent(Event):
    """工具执行完成。

    Attributes:
        name: 工具名。
        command: 命令原文（仅 run_bash 类工具有意义，供 CLI 回显）。
        result: 工具返回值（字符串）。
    """

    name: str = ""
    command: str = ""
    result: str = ""


@dataclass
class InterruptEvent(Event):
    """用户中断了当前操作。

    Attributes:
        stage: 中断发生的阶段（如 "model" / "tool"）。
    """

    stage: str = ""


@dataclass
class ErrorEvent(Event):
    """发生异常。

    Attributes:
        stage: 出错阶段。
        error: 异常对象或描述文本。
    """

    stage: str = ""
    error: Any = None


@dataclass
class RollbackEvent(Event):
    """本轮对话被回滚（中断/异常后清理残缺消息）。

    Attributes:
        dropped: 被丢弃的消息条数。
    """

    dropped: int = 0


# ----------------------------------------------------------------------
# 事件总线
# ----------------------------------------------------------------------
class EventBus:
    """极简事件总线：按事件类型订阅，顺序派发。

    用法：
        bus = EventBus()
        bus.subscribe(ToolCallEvent, lambda e: print(e.name))
        bus.emit_sync(ToolCallEvent(name="run_bash"))

    订阅粒度：按**具体事件类型**精确匹配（不做基类继承匹配），
    避免订阅 Event 基类时收到所有事件造成的意外耦合。
    """

    def __init__(self) -> None:
        # 事件类型 -> 处理器列表（保持注册顺序，保证派发顺序确定）
        self._handlers: Dict[Type[Event], List[Callable[[Event], None]]] = {}
        # 绑定的 asyncio 事件循环（供 emit_threadsafe 从工作线程投递事件）
        self._loop = None
        # 当前会话代次（阶段 3 引入）：用于丢弃中断后残留的旧事件。
        # 每轮对话开始时由内核调用 new_generation() 递增；
        # 派发时若事件带 session_id 且与当前代次不符，则丢弃（见 _is_stale）。
        self._generation: str = ""

    def subscribe(self, event_type: Type[Event],
                  handler: Callable[[Event], None]) -> None:
        """订阅某类事件。

        Args:
            event_type: 事件类型（如 ToolCallEvent）。
            handler: 处理器，签名 handler(event) -> None。
                允许为同步函数；阶段 2 起也允许 async 函数（由 emit 负责 await）。
        """
        self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: Type[Event],
                    handler: Callable[[Event], None]) -> bool:
        """取消订阅。返回是否成功移除。"""
        handlers = self._handlers.get(event_type)
        if not handlers or handler not in handlers:
            return False
        handlers.remove(handler)
        return True

    def clear(self) -> None:
        """清空所有订阅（热重载重建时使用）。"""
        self._handlers.clear()

    def handlers_for(self, event_type: Type[Event]) -> List[Callable[[Event], None]]:
        """返回某类事件的处理器列表副本（供调试/测试）。"""
        return list(self._handlers.get(event_type, []))

    # ------------------------------------------------------------------
    # 派发
    # ------------------------------------------------------------------
    def emit_sync(self, event: Event) -> None:
        """同步派发事件（阶段 1 使用）。

        顺序调用所有订阅者；单个订阅者抛异常不影响其它订阅者
        （异常被吞掉并打印，避免一个坏订阅者拖垮整个对话）。
        """
        if self._is_stale(event):
            return
        for handler in self._handlers.get(type(event), []):
            try:
                result = handler(event)
                # 若订阅者误传 async 函数，同步上下文无法 await，明确提示
                if _is_awaitable(result):
                    _warn_async_in_sync(type(event).__name__)
            except Exception as e:  # noqa: BLE001 - 订阅者异常不应中断主流程
                print(f"⚠️ [事件处理器异常] {type(event).__name__}: "
                      f"{type(e).__name__}: {e}")

    async def emit(self, event: Event) -> None:
        """异步派发事件（阶段 2 起使用）。

        顺序 await 所有订阅者；同步订阅者直接调用，async 订阅者被 await。
        同样保证单个订阅者异常不影响其它订阅者。
        """
        if self._is_stale(event):
            return
        for handler in self._handlers.get(type(event), []):
            try:
                result = handler(event)
                if _is_awaitable(result):
                    await result
            except Exception as e:  # noqa: BLE001
                print(f"⚠️ [事件处理器异常] {type(event).__name__}: "
                      f"{type(e).__name__}: {e}")

    def emit_threadsafe(self, event: Event) -> None:
        """从**工作线程**安全地派发事件（阶段 2 引入）。

        背景：`run_bash` 工具通过 `asyncio.to_thread` 在独立线程中执行阻塞 IO，
        其输出回调 `_on_tool_output` 因此运行在工作线程里，无法 await。
        本方法把事件投递回事件循环所在线程，由主循环顺序派发，
        从而保证：
          1. 派发顺序与产生顺序一致（不并发、不乱序）；
          2. 订阅者（可能含 async 函数）始终在事件循环线程中被调用。

        实现：若当前线程就是事件循环线程（或没有运行中的循环），
        直接同步派发；否则用 `loop.call_soon_threadsafe` 投递。

        注意：投递是"即发即忘"（fire-and-forget），调用方不等待派发完成。
        对工具输出呈现而言这没有问题（顺序由事件循环保证）。
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            # 无运行中的事件循环：退化为同步派发（如单元测试/非异步场景）
            self.emit_sync(event)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            # 已在事件循环线程中，直接同步派发即可
            self.emit_sync(event)
            return
        # 工作线程：投递回事件循环线程，由主循环顺序派发
        loop.call_soon_threadsafe(self.emit_sync, event)

    def bind_loop(self, loop) -> None:
        """绑定事件循环（阶段 2 引入）。

        由 REPL 在启动时调用，使 `emit_threadsafe` 知道该把工作线程的事件
        投递到哪个循环。热重载保留 bus 实例，因此绑定关系不会丢失。
        """
        self._loop = loop

    # ------------------------------------------------------------------
    # 会话代次（阶段 3 引入）：丢弃中断后残留的旧事件
    # ------------------------------------------------------------------
    def new_generation(self) -> str:
        """开启新的一轮会话代次，返回新的代次标识。

        由内核在每轮 `achat()` 开始时调用。此后产生的事件应带上该标识
        （见 `stamp`）。中断/回滚后，上一代次残留的事件（如工作线程
        通过 `emit_threadsafe` 投递、但尚未派发的 ToolResultEvent）
        会被 `_is_stale` 判定为过期并丢弃，避免污染新一轮对话。
        """
        self._generation = f"gen-{time.time_ns()}"
        return self._generation

    @property
    def generation(self) -> str:
        """当前会话代次标识（空串表示尚未开启代次，此时不做过滤）。"""
        return self._generation

    def stamp(self, event: Event) -> Event:
        """给事件打上当前代次标识（若事件尚未带 session_id）。

        内核在 emit 前调用，使事件可被代次过滤。已带 session_id 的事件
        （如工作线程中提前构造的）保持原值不变。
        """
        if not event.session_id:
            event.session_id = self._generation
        return event

    def _is_stale(self, event: Event) -> bool:
        """判断事件是否属于已过期的旧代次（应被丢弃）。

        规则：仅当"总线已开启代次"且"事件带 session_id"且"两者不符"时，
        判定为过期。这样：
          - 未开启代次（如单元测试直接 emit）时不过滤，保持向后兼容；
          - 未打标识的事件（session_id 为空）不过滤，避免误伤。
        """
        if not self._generation:
            return False
        if not event.session_id:
            return False
        return event.session_id != self._generation


def _is_awaitable(obj: Any) -> bool:
    """判断对象是否为可 await 对象（协程 / Future / 自定义 awaitable）。"""
    import inspect

    return inspect.isawaitable(obj)


def _warn_async_in_sync(event_name: str) -> None:
    """提示：在同步上下文中订阅了 async 处理器。"""
    print(f"⚠️ [事件总线] {event_name} 的订阅者是 async 函数，"
          f"但当前为同步派发（emit_sync），该处理器未被 await。"
          f"阶段 2 异步化后将自动生效。")


# 全局默认总线（供不方便持有引用的场景使用；内核优先用实例级总线）
_DEFAULT_BUS = EventBus()


def get_default_bus() -> EventBus:
    """获取全局默认事件总线。"""
    return _DEFAULT_BUS
