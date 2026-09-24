"""内核：InteractiveAssistant 主类。

职责：
- 装配各层（runtime / tools / memory / models / security）；
- 驱动多轮对话（模型调用 -> 工具执行 -> 结果回填）；
- 提供热重载能力（原地刷新代码，保留上下文与 shell 会话）。
"""

import asyncio
import glob
import importlib.util
import os
import sys
import time

from core.events import (
    EventBus,
    ErrorEvent,
    InterruptEvent,
    ModelChunkEvent,
    ModelEndEvent,
    ModelStartEvent,
    RollbackEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserMessageEvent,
)
from core.prompts import SYSTEM_PROMPT


class _Interrupted(Exception):
    """内部信号：表示用户中断了当前操作。"""


from core import state as _state
from memory import ConversationMemory
import models as _models
from runtime import PersistentBash
from tools import make_bash_tool, make_install_skill_tool, make_reload_tool


def _is_timeout_error(exc: Exception) -> bool:
    """判断异常是否为超时/网络类错误（此类错误不应盲目重发请求）。"""
    name = type(exc).__name__.lower()
    if "timeout" in name or "connect" in name:
        return True
    text = str(exc).lower()
    return "timeout" in text or "timed out" in text


def _is_interrupt_result(res) -> bool:
    """判断工具返回值是否表示"工具内部被用户中断"（阶段 3）。

    目前只有 run_bash 会返回中断标记（见 runtime/bash.py 的 INTERRUPT_MARKER）。
    内核据此回滚本轮对话，而不是把残缺结果写入历史。
    """
    try:
        from runtime.bash import INTERRUPT_MARKER
    except Exception:
        return False
    return isinstance(res, str) and res.startswith(INTERRUPT_MARKER)


def _tool_is_concurrent(tool) -> bool:
    """判断工具是否可并发执行（阶段 4）。

    读取工具对象上的 `_concurrency` 元数据（见 tools/builtin.py）：
      - 显式标注 False 的工具（run_bash / install_skill / reload_self）
        不可并发——它们分别共享 shell 会话、改技能注册表、改自身状态；
      - 未标注的工具（技能库纯计算函数）默认可并发。

    Args:
        tool: 已注册的工具对象（StructuredTool 或裸可调用对象）。

    Returns:
        True 表示该工具可与其他可并发工具同批执行。
    """
    from tools.builtin import CONCURRENCY_ATTR

    return bool(getattr(tool, CONCURRENCY_ATTR, True))

# 技能库目录（相对项目根目录）
SKILLS_DIR = os.path.abspath("./skills")
os.makedirs(SKILLS_DIR, exist_ok=True)
if SKILLS_DIR not in sys.path:
    sys.path.append(SKILLS_DIR)

# 热重载时需要原地刷新的模块（顺序：被依赖者在前）
RELOADABLE_MODULES = [
    # 注意：security 相关模块【故意】不在热重载名单内。
    # 安全策略只在进程冷启动时加载，任何修改都必须由用户显式重启进程才生效，
    # 防止 Agent 通过 reload_self 自行放宽/绕过自身的安全约束。
    "core.config",
    "runtime.bash",
    "tools.builtin",
    "models.llm",
    "core.prompts",
    "core.assistant",
]


class InteractiveAssistant:
    def __init__(self, api_key):
        self.api_key = api_key
        # 当前激活的模型档案名（可在运行时通过 /model 切换）
        # 优先恢复上次退出时保存的档案；若已失效（档案被删/密钥缺失）则回退默认。
        self.model_profile = self._restore_model_profile()
        self.bash = PersistentBash(confirm_callback=self._confirm_command)
        self.tools_registry = {}
        # 事件总线：内核只负责 emit，CLI/日志等外部通过 subscribe 订阅。
        # 阶段 2 起统一用 await bus.emit(...) 顺序派发；
        # 事件总线：内核只负责 emit，CLI/日志等外部通过 subscribe 订阅。
        # 阶段 5 起 bash 为原生异步，工具事件在事件循环线程中产生；
        # emit_threadsafe 保留以兼容可能的工作线程场景（如技能库中的阻塞调用）。
        self.bus = EventBus()
        # 常驻对话历史：首条固定为系统提示词（自我认知），后续为对话消息
        self.memory = ConversationMemory(
            SYSTEM_PROMPT,
            max_chars=_models.CONTEXT_MAX_CHARS,
            keep_recent=_models.CONTEXT_KEEP_RECENT,
        )

        # 延迟重载标志：工具只置位，真正的重载在 chat() 返回后由主循环执行，
        # 避免在调用栈未清空时替换 self.__dict__ 导致行为不一致。
        self._pending_reload = False

        self._register_builtin_tools()
        self._bootstrap_existing_skills()  # 安装技能库

    # ------------------------------------------------------------------
    # 工具注册
    # ------------------------------------------------------------------
    @staticmethod
    async def _confirm_command(cmd: str, reason: str) -> bool:
        """终端交互确认：危险命令执行前询问用户（阶段 5：异步）。

        阶段 5 起 bash 为原生异步，确认流程运行在**事件循环线程**中，
        因此本方法为 async，可直接 await 异步输入函数（无需线程桥接）。

        注意：确认期间必须暂停 Esc 监听线程。否则监听线程会与
        prompt_toolkit 争抢同一个 stdin，导致用户输入的 y/Enter 被
        监听线程读走（或其中的字节被误判为 Esc），进而产生"幽灵中断"。
        """
        print("\n" + "=" * 60)
        print(f"⚠️  需要确认：{reason}")
        print(f"    命令: {cmd}")
        print("=" * 60)

        # 暂停 Esc 监听，避免与确认输入争抢 stdin
        from runtime.interrupt import get_interrupt
        ctrl = get_interrupt()
        ctrl.stop()
        try:
            # 阶段 5：运行在事件循环线程中，使用异步输入函数。
            from cli.repl import _read_input
            ans = (await _read_input("是否执行？[y/N] > ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        finally:
            # 恢复监听，并清除期间可能残留的中断标志
            ctrl.clear()
            ctrl.start()
        return ans in ("y", "yes")

    def register_tool(self, tool_func):
        """注册一个工具到工具表。

        传入裸函数时自动用 @tool 包装为 function call 工具；
        传入已包装的 StructuredTool 时直接注册（幂等）。
        """
        from langchain_core.tools import tool as _tool

        t = tool_func if hasattr(tool_func, "name") else _tool(tool_func)
        self.tools_registry[t.name] = t

    def _on_tool_output(self, command: str, output: str) -> None:
        """run_bash 的输出回调：发布 ToolResultEvent 到事件总线。

        阶段 2 起统一走事件总线：内核不再持有 CLI 的临时回调，
        由 CLI 在启动时订阅 ToolResultEvent 决定如何呈现（如折叠）。

        阶段 5 起 bash 为原生异步，本回调在**事件循环线程**中被调用
        （不再是工作线程），因此可直接同步派发。仍用 `emit_threadsafe`
        以保持向后兼容（它在事件循环线程中会退化为同步派发）。
        """
        self.bus.emit_threadsafe(ToolResultEvent(
            name="run_bash", command=command, result=output,
        ))

    def _register_builtin_tools(self):
        self.register_tool(make_bash_tool(self.bash, on_output=self._on_tool_output))
        self.register_tool(make_install_skill_tool(SKILLS_DIR, self._load_skill_file))
        self.register_tool(make_reload_tool(self))

    def _bootstrap_existing_skills(self):
        """扫描本地 skills 目录并自动热加载（单个文件出错不影响整体启动）"""
        skill_files = glob.glob(os.path.join(SKILLS_DIR, "*.py"))
        for fpath in skill_files:
            skill_name = os.path.splitext(os.path.basename(fpath))[0]
            try:
                self._load_skill_file(skill_name, fpath)
            except Exception as e:
                print(f"⚠️ 技能 [{skill_name}] 加载失败，已跳过: {e}")

    def _load_skill_file(self, skill_name: str, file_path: str):
        spec = importlib.util.spec_from_file_location(skill_name, file_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        func = getattr(mod, skill_name, None)
        if func:
            from langchain_core.tools import tool
            self.register_tool(tool(func))

    # ------------------------------------------------------------------
    # 模型
    # ------------------------------------------------------------------
    @property
    def model(self):
        """按当前激活的模型档案构造客户端（每次调用即时构造，便于切换后立即生效）。"""
        return _models.build_model_from_profile(
            self.model_profile,
            tools=self.tools_registry.values(),
        )

    def context_report(self) -> str:
        """展示当前上下文占用情况（消息数 / 字符数 / 预算 / 已丢弃）。"""
        st = self.memory.stats()
        pct = (st["chars"] / st["max_chars"] * 100) if st["max_chars"] else 0
        return (
            "🧠 上下文占用：\n"
            f"   消息条数 : {st['messages']}\n"
            f"   字符总量 : {st['chars']:,} / {st['max_chars']:,}（{pct:.1f}%）\n"
            f"   已丢弃   : {st['dropped']} 条（超出预算时从最旧处成组丢弃）\n"
            "   提示     : 预算可用环境变量 AGENT_CONTEXT_MAX_CHARS 调整。"
        )

    @staticmethod
    def _restore_model_profile() -> str:
        """确定启动时的模型档案：优先上次保存值，失效则回退默认。

        校验两点：档案仍存在、密钥环境变量已就绪。
        任一不满足都静默回退到 DEFAULT_PROFILE，保证启动不被阻断。
        """
        saved = _state.get_saved_model_profile()
        if saved and saved in _models.MODEL_PROFILES:
            if _models.resolve_api_key(_models.MODEL_PROFILES[saved]):
                return saved
        return _models.DEFAULT_PROFILE

    def list_models(self) -> str:
        """列出所有可用模型档案，标注当前激活项与密钥是否就绪。"""
        lines = ["📦 可用模型档案："]
        for name, prof in _models.MODEL_PROFILES.items():
            active = "▶" if name == self.model_profile else " "
            key_ok = "✅" if _models.resolve_api_key(prof) else "❌ 缺少 " + prof["api_key_env"]
            lines.append(
                f"  {active} {name:<18} {prof['label']:<24} [{key_ok}]"
            )
        lines.append("\n用法：/model <名称>  切换模型（如 /model gemini）")
        return "\n".join(lines)

    def switch_model(self, name: str) -> str:
        """切换当前模型档案。切换前校验档案存在且密钥就绪。

        Args:
            name: _models.MODEL_PROFILES 中的档案名。

        Returns:
            面向用户的提示文本（成功或失败原因）。
        """
        name = (name or "").strip()
        if not name:
            return self.list_models()
        try:
            prof = _models.get_profile(name)
        except KeyError as e:
            return f"❌ {e}"
        if not _models.resolve_api_key(prof):
            return (
                f"❌ 无法切换到 '{name}'：环境变量 {prof['api_key_env']} 未设置。\n"
                f"   请在 .env 中补充 {prof['api_key_env']}=xxx 后重启进程。"
            )
        old = self.model_profile
        self.model_profile = name
        saved = _state.save_model_profile(name)
        tail = "已记住该选择，下次启动自动恢复。" if saved else "（⚠️ 状态保存失败，下次启动将回到默认模型）"
        return (
            f"✅ 模型已切换：{old} → {name}（{prof['label']}）\n"
            f"   下一轮对话即生效，对话上下文与 shell 会话保持不变。\n"
            f"   {tail}"
        )

    # ------------------------------------------------------------------
    # 热重载
    # ------------------------------------------------------------------
    @staticmethod
    def _reload_module(module):
        """重载一个已加载的模块，兼容 `python xxx.py` 启动的场景。

        读取模块源码并在其自身命名空间中原地执行，刷新代码逻辑；
        执行时跳过 `if __name__ == "__main__":` 主循环块。
        模块对象身份不变，sys.modules 与外部引用仍指向同一对象。
        """
        file_path = getattr(module, "__file__", None)
        if not file_path:
            raise RuntimeError(f"模块 {module.__name__} 无 __file__，无法重载")

        with open(file_path, "r", encoding="utf-8") as f:
            source = f.read()
        code = compile(source, file_path, "exec")

        # 在模块自身命名空间中执行，原地刷新；临时改名以跳过 __main__ 块
        ns = module.__dict__
        original_name = ns.get("__name__")
        try:
            if original_name == "__main__":
                ns["__name__"] = "__reloaded_main__"
            exec(code, ns)
        finally:
            ns["__name__"] = original_name
        return module

    def reload_code(self) -> str:
        """热重载：重新加载内核与安全模块，替换代码逻辑，
        但保留对话上下文（messages）与 shell 会话（bash 进程/cwd/env）。
        在对本Agent进行功能更新时自动热重载。

        【能力边界与限制说明】
        ------------------------------------------------------------------------
        ✅ 适用场景（推荐使用热重载，秒级生效）：
          - 纯逻辑修复：修改方法体内部代码、调整 Prompt 模版、优化异常处理与日志。
          - 工具增删改：新增/移除 Tool、修改现有 Tool 的执行逻辑或入参描述。
          - 规则配置更新：更新 core/ 中的提示词、工具描述等。
            ⚠️ security/ 下的安全策略【不参与热重载】，修改后必须冷重启进程才生效。
          - 新增普通方法：类中新增的方法随原型链立即生效。

        ❌ 不支持 / 存在风险（必须进行完整冷重启进程）：
          - 状态结构（Schema）变更：如重构了 `messages` 的数据结构。
          - 常驻资源与后台任务：若旧代码启动了独立的后台线程（Thread）、异步任务
            （asyncio.Task）或网络 Socket 监听，reload 不会自动清理旧资源。
          - 跨模块静态引用：若外部模块通过 `from xxx import InteractiveAssistant`
            持有类引用，外部依然会指向旧类指针，无法同步更新。
        ------------------------------------------------------------------------
        """
        # 记录需保留的状态
        preserved = {
            "memory": self.memory,
            "bash": self.bash,
            "api_key": self.api_key,
            "model_profile": getattr(self, "model_profile", _models.DEFAULT_PROFILE),
            # 事件总线连同其订阅者一起保留：重载不应丢失 CLI 注册的订阅者
            "bus": getattr(self, "bus", None),
        }

        try:
            # 按依赖顺序原地重载各模块（策略/提示词可能被改过）
            for name in RELOADABLE_MODULES:
                if name in sys.modules:
                    self._reload_module(sys.modules[name])
            # 重载后强制刷新配置缓存，使 config.toml 的改动立即生效
            if "core.config" in sys.modules:
                sys.modules["core.config"].reload()
            mod = sys.modules["core.assistant"]
            new_cls = mod.InteractiveAssistant
            new_prompt = sys.modules["core.prompts"].SYSTEM_PROMPT
        except Exception as e:
            return f"❌ 热重载失败（代码未替换，上下文完好）: {type(e).__name__}: {e}"

        try:
            # 用旧状态构造新实例（不新建 bash，避免丢 shell 状态）
            new_obj = new_cls.__new__(new_cls)
            new_obj.api_key = preserved["api_key"]
            new_obj.model_profile = preserved["model_profile"]
            new_obj.bash = preserved["bash"]
            new_obj.tools_registry = {}
            new_obj.memory = preserved["memory"]
            # 恢复事件总线（保留订阅者）；旧实例无 bus 时新建一个
            new_obj.bus = preserved["bus"] or EventBus()
            new_obj._pending_reload = False
            # 重新注册工具（使用新代码里的工具定义）
            new_obj._register_builtin_tools()
            new_obj._bootstrap_existing_skills()
        except Exception as e:
            return f"❌ 热重载失败（新代码初始化出错，上下文完好）: {type(e).__name__}: {e}"

        # 原地替换：self 引用不变，但方法/属性全部换成新的
        self.__class__ = new_cls
        self.__dict__.update(new_obj.__dict__)
        # 刷新系统提示词：若新代码改了 SYSTEM_PROMPT，立即替换历史首条 system 消息
        self.memory.ensure_system_prompt(new_prompt)
        return (
            f"✅ 热重载成功。上下文保留 {len(self.memory)} 条消息，"
            f"shell 会话未重启，已激活技能: {list(self.tools_registry.keys())}"
        )

    # ------------------------------------------------------------------
    # 对话驱动（异步）
    # ------------------------------------------------------------------
    async def achat(self, user_input: str) -> str:
        """多轮对话单步驱动器（异步，支持 Esc 中断）。

        阶段 2 起本方法为 async：模型调用改用原生 `astream` / `ainvoke`，
        不再需要线程化调用器（_run_interruptible 已删除）。
        中断通过 `asyncio.wait_for` + 中断标志轮询实现，超时用 asyncio 原生机制。

        事件派发：所有状态变化都通过 `await self.bus.emit(...)` 顺序派发，
        CLI 通过订阅事件决定如何呈现（不再有 on_tool_call / on_tool_output 回调）。

        Args:
            user_input: 用户本轮输入。

        Returns:
            面向用户的回复文本（模型输出或中断/错误提示）。
        """
        # 兜底：确保系统提示词始终位于对话历史首位（热重载/异常后自愈）
        self.memory.ensure_system_prompt()

        from langchain_core.messages import ToolMessage

        from runtime.interrupt import get_interrupt

        ctrl = get_interrupt()
        # 消费掉上一轮可能残留的中断标志（阶段 3）：
        # 用 consume() 而非 clear()，语义是"确认并清除"，避免把
        # 上一轮的中断误当作本轮的中断，也避免吞掉真实中断。
        ctrl.consume()

        # 开启新一轮会话代次（阶段 3）：此后产生的事件都带本代次标识，
        # 中断后残留的旧代次事件会被事件总线丢弃，不再污染新一轮对话。
        self.bus.new_generation()

        # 记录本轮起始位置，便于中断时回滚，保证历史一致
        start_len = len(self.memory)
        self.memory.add_user(user_input)
        await self.bus.emit(UserMessageEvent(content=user_input))

        ctrl.start()  # 启动 Esc 监听（仅 TTY 下生效）
        try:
            while True:
                # ---- 模型调用（流式，便于及时响应中断）----
                try:
                    ai_msg = await self._astream_model(ctrl)
                except _Interrupted:
                    await self._rollback(start_len, stage="model")
                    return "⏹️ 已中断（模型调用阶段）。已回到对话。"
                except TimeoutError as e:
                    await self._rollback(start_len, stage="model")
                    return (
                        f"⏱️ 模型调用超时：{e}\n"
                        f"   可重试，或用 /model 切换到其它模型。"
                    )
                except Exception as e:
                    await self._rollback(start_len, stage="model")
                    await self.bus.emit(ErrorEvent(stage="model", error=e))
                    return f"❌ 模型调用失败: {type(e).__name__}: {e}"

                if ai_msg is None:  # 被中断
                    await self._rollback(start_len, stage="model")
                    return "⏹️ 已中断（模型调用阶段）。已回到对话。"

                self.memory.add(ai_msg)
                await self.bus.emit(ModelEndEvent(message=ai_msg))

                if not ai_msg.tool_calls:
                    return ai_msg.content

                # ---- 工具执行（阶段 4：并发执行 + 按原序回填）----
                # 同一轮内的多个 tool_calls 中，可并发的工具（技能库纯计算函数）
                # 会被合并成批次用 asyncio.gather 并发执行；不可并发的工具
                # （run_bash / install_skill / reload_self）单独串行执行。
                # 无论并发与否，结果都按 tool_calls 的原始顺序回填 memory，
                # 否则模型会因结果错位而困惑。
                results, interrupted = await self._run_tool_calls(
                    ai_msg.tool_calls, ctrl
                )
                if interrupted:
                    await self._rollback(start_len, stage="tool")
                    return "⏹️ 已中断（工具执行阶段）。已回到对话。"

                for call, res in results:
                    self.memory.add(ToolMessage(
                        content=str(res),
                        tool_call_id=call["id"]
                    ))
        finally:
            ctrl.stop()

    async def _run_tool_calls(self, tool_calls, ctrl):
        """执行一轮内的全部工具调用（阶段 4：并发 + 按原序回填）。

        调度策略
        --------
        按 tool_calls 的原始顺序扫描，把**连续的可并发工具**聚成一个批次，
        用 `asyncio.gather` 并发执行；遇到不可并发工具（run_bash /
        install_skill / reload_self）则单独串行执行。批次之间保持顺序，
        因此"并发"只发生在相邻的可并发工具之间，不会打乱与串行工具的
        相对次序（例如 [技能A, run_bash, 技能B] 中 A 与 B 不会并发）。

        结果顺序
        --------
        无论并发与否，返回值都按 tool_calls 的原始下标排序，
        保证调用方按原序回填 memory（否则模型会因结果错位而困惑）。

        中断处理
        --------
        每个批次开始前用 `ctrl.consume()` 检查中断（一次 Esc 只响应一次）。
        批次执行期间若发生中断，会取消尚未完成的并发任务并返回
        interrupted=True，由调用方回滚本轮对话。

        Args:
            tool_calls: 模型给出的工具调用列表（AIMessage.tool_calls）。
            ctrl: 中断控制器。

        Returns:
            (results, interrupted)：
                results 为 [(call, result_str), ...]，按原始顺序排列；
                interrupted 为 True 表示执行期间被用户中断（results 无意义）。
        """
        n = len(tool_calls)
        results = [None] * n  # 按原始下标占位，最后统一按序返回

        # 先把每个调用解析为 (下标, call, tool, 是否可并发)
        resolved = []
        for idx, call in enumerate(tool_calls):
            fn_name = call["name"]
            target_tool = self.tools_registry.get(fn_name)
            concurrent = target_tool is not None and _tool_is_concurrent(target_tool)
            resolved.append((idx, call, target_tool, concurrent))

        # 按"连续可并发"切分成批次：每个批次要么是单个不可并发工具，
        # 要么是一段连续的可并发工具。
        batches = []
        current = []
        for item in resolved:
            if item[3]:  # 可并发
                current.append(item)
            else:  # 不可并发：先冲刷已积累的并发批次，再单独成批
                if current:
                    batches.append(current)
                    current = []
                batches.append([item])
        if current:
            batches.append(current)

        for batch in batches:
            # 批次开始前检查中断（阶段 3 语义：consume 一次只响应一次）
            if ctrl.consume():
                return [], True

            if len(batch) == 1:
                # 单个工具（含所有不可并发工具）：直接串行执行
                idx, call, target_tool, _ = batch[0]
                res, was_interrupted = await self._execute_one_tool(call, target_tool, ctrl)
                if was_interrupted:
                    return [], True
                results[idx] = (call, res)
            else:
                # 多个可并发工具：并发执行，结果按各自下标回填
                tasks = [
                    asyncio.ensure_future(self._execute_one_tool(call, target_tool, ctrl))
                    for (_, call, target_tool, _) in batch
                ]
                try:
                    # 并发等待；期间轮询中断，命中则取消全部未完成任务
                    done = await self._gather_with_interrupt(tasks, ctrl)
                except _Interrupted:
                    return [], True
                for (idx, call, _, _), (res, was_interrupted) in zip(batch, done):
                    if was_interrupted:
                        return [], True
                    results[idx] = (call, res)

        return results, False

    async def _gather_with_interrupt(self, tasks, ctrl):
        """并发等待一组任务，同时支持 Esc 中断。

        与 `_await_with_interrupt` 同源思路：以 50ms 为周期轮询中断标志，
        命中则取消所有未完成任务并抛出 `_Interrupted`。

        Args:
            tasks: asyncio.Task 列表（每个返回 (result, was_interrupted)）。
            ctrl: 中断控制器。

        Returns:
            与 tasks 等长的结果列表（顺序与 tasks 一致）。

        Raises:
            _Interrupted: 用户按 Esc 中断。
        """
        pending = set(tasks)
        try:
            while pending:
                if ctrl.consume():
                    for t in pending:
                        t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    raise _Interrupted()
                done, pending = await asyncio.wait(pending, timeout=0.05)
        except asyncio.CancelledError:
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise
        # 全部完成：按原顺序取结果
        return [t.result() for t in tasks]

    async def _execute_one_tool(self, call, target_tool, ctrl):
        """执行单个工具调用，返回 (结果字符串, 是否被中断)。

        本方法被设计为可安全并发调用（阶段 4）：它只读取共享状态、
        不修改 memory，结果通过返回值交给调用方按序回填。

        事件派发：工具调用事件（ToolCallEvent）与结果事件（ToolResultEvent）
        都带当前会话代次（`bus.stamp`），使中断后残留的并发结果事件
        被事件总线丢弃，不污染新一轮对话。

        Args:
            call: 单个工具调用（含 name / args / id）。
            target_tool: 已注册的工具对象；None 表示未找到。
            ctrl: 中断控制器。

        Returns:
            (res, was_interrupted)：
                res 为工具返回的字符串（或错误描述）；
                was_interrupted 为 True 表示工具内部被中断（如 run_bash 收到 Esc）。
        """
        fn_name = call["name"]
        fn_args = call["args"]

        # 参数已聚合完成，发布完整参数事件（工具名此前已流式提示过）。
        # 打上当前代次，避免中断后残留事件污染下一轮。
        await self.bus.emit(self.bus.stamp(ToolCallEvent(name=fn_name, args=fn_args)))

        if target_tool is None:
            return f"Error: 未找到工具 {fn_name}", False

        if fn_name not in ["run_bash", "install_skill"]:
            print(f"\n🔥 [触发已安装技能]: {fn_name}({fn_args})")
        try:
            # 工具统一以异步方式调用：
            # - 内置工具（run_bash）为原生异步（asyncio 子进程）；
            # - 技能库纯计算函数由 langchain 的 ainvoke 在线程池中执行。
            res = await target_tool.ainvoke(fn_args)
        except _Interrupted:
            return "", True
        except asyncio.CancelledError:
            # 并发批次被中断时，未完成的任务会被 cancel，这里向上传播
            raise
        except Exception as e:
            # 工具异常不中断对话，回填给模型让它自行纠错
            res = f"Error: 工具 {fn_name} 执行失败: {type(e).__name__}: {e}"
            print(f"⚠️ [工具异常]: {res}")

        # 工具内部被中断（如 run_bash 收到 Esc 后返回中断标记）：
        # 阶段 3 起不再把残缺结果写入历史，而是回滚本轮对话。
        if _is_interrupt_result(res):
            return "", True

        return res, False

    @staticmethod
    def _make_wait_notifier(threshold: float = 5.0, interval: float = 15.0):
        """构造"仍在等待"提示回调：等待超过阈值后周期性打印一次。

        目的：模型调用卡住时用户看不到任何反馈，容易误以为程序死机。
        这里在等待期间给出明确提示（含已等待秒数与 Esc 提示），
        让用户知道进程仍在工作、可以按 Esc 中断。

        Args:
            threshold: 首次提示的等待秒数（默认 5s）。
            interval: 之后每隔多少秒再提示一次（默认 15s）。

        Returns:
            回调 on_wait(elapsed)，可安全传入异步等待循环。
        """
        state = {"next": threshold}

        def on_wait(elapsed: float) -> None:
            if elapsed >= state["next"]:
                print(
                    f"\n⏳ 模型仍在响应中…（已等待 {elapsed:.0f}s，"
                    f"按 Esc 可中断）",
                    flush=True,
                )
                state["next"] = elapsed + interval

        return on_wait

    async def _await_with_interrupt(self, coro, ctrl, *, first_byte_timeout=None,
                                    total_timeout=None, on_wait=None,
                                    first_byte_flag=None):
        """在异步上下文中等待一个协程，同时支持 Esc 中断与超时。

        阶段 2 用原生 asyncio 取代了旧的线程化调用器（_run_interruptible）：
        把模型调用包成一个 asyncio.Task，主协程以 50ms 为周期轮询中断标志、
        首字节超时与整体超时。这样即使底层 socket 完全 hang 住，
        Esc 也能在毫秒级生效，超时后也能主动放弃等待。

        Args:
            coro: 待等待的协程对象（如 self._consume_stream(...)）。
            ctrl: 中断控制器（提供 is_set()）。
            first_byte_timeout: 首个数据到达前的最大等待秒数（None 表示不限）。
            total_timeout: 整体最大等待秒数（None 表示不限）。
            on_wait: 可选回调 on_wait(elapsed)，等待期间周期性调用。
            first_byte_flag: 可选单元素列表（如 [False]），由被等待的协程在
                收到首个数据时置为 True，用于实现首字节超时判定。

        Returns:
            (status, value)：
                status == "ok"          -> value 为协程返回值
                status == "interrupted" -> 用户按 Esc
                status == "timeout"     -> 超时（value 为超时描述）
                status == "error"       -> value 为协程抛出的异常对象
        """
        task = asyncio.ensure_future(coro)
        start = time.monotonic()
        try:
            while True:
                if task.done():
                    break
                # 用 consume() 消费中断（阶段 3）：读取即清除，
                # 保证同一次 Esc 不会被后续的 is_set()/consume() 重复响应。
                if ctrl.consume():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return "interrupted", None
                now = time.monotonic()
                elapsed = now - start
                if total_timeout is not None and elapsed > total_timeout:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return "timeout", f"整体等待超过 {total_timeout:g}s"
                first = bool(first_byte_flag[0]) if first_byte_flag is not None else True
                if (first_byte_timeout is not None and not first
                        and elapsed > first_byte_timeout):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return "timeout", f"连接后 {first_byte_timeout:g}s 内未收到任何数据"
                if on_wait is not None:
                    try:
                        on_wait(elapsed)
                    except Exception:
                        pass
                # 等待 task 完成或 50ms 超时（用于轮询中断/超时）
                await asyncio.wait({task}, timeout=0.05)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

        if task.cancelled():
            return "interrupted", None
        exc = task.exception()
        if exc is not None:
            return "error", exc
        return "ok", task.result()

    async def _astream_model(self, ctrl):
        """异步流式调用模型并聚合为完整 AIMessage；中断/超时返回 None。

        实现要点：真正的网络调用是原生异步的（`self.model.astream`），
        由 `_await_with_interrupt` 负责轮询中断标志与超时，
        因此即使底层 socket 完全 hang 住，Esc 也能立即生效。

        Args:
            ctrl: 中断控制器。

        Returns:
            聚合后的 AIMessage（或 AIMessageChunk，语义一致）；
            被中断时返回 None。
        """
        # 首字节超时：连接建立后迟迟不吐字（服务端挂起）时及时失败。
        # 整体超时：给足长思考/长输出的空间，但兜底防止无限等待。
        first_byte_timeout = _models.FIRST_BYTE_TIMEOUT
        total_timeout = _models.TOTAL_TIMEOUT

        state = {"aggregated": None, "chunks": 0}
        first_flag = [False]
        on_wait = self._make_wait_notifier()

        await self.bus.emit(ModelStartEvent())

        async def _consume():
            """消费异步流，逐 chunk 聚合，并发布流式事件。"""
            async for chunk in self.model.astream(self.memory.messages):
                first_flag[0] = True
                state["chunks"] += 1
                # 工具调用流式：工具名一出现就通知（参数逐字符生成，此处不通知）
                if getattr(chunk, "tool_call_chunks", None):
                    for tc in chunk.tool_call_chunks:
                        if tc.get("name"):
                            await self.bus.emit(ToolCallEvent(name=tc["name"]))
                # 文本增量事件（供需要实时渲染的订阅者使用）
                text = getattr(chunk, "content", "") or ""
                if isinstance(text, str) and text:
                    await self.bus.emit(ModelChunkEvent(text=text))
                agg = state["aggregated"]
                state["aggregated"] = chunk if agg is None else agg + chunk
            return state["aggregated"]

        status, value = await self._await_with_interrupt(
            _consume(), ctrl,
            first_byte_timeout=first_byte_timeout,
            total_timeout=total_timeout,
            on_wait=on_wait,
            first_byte_flag=first_flag,
        )

        if status == "interrupted":
            # 中断事件统一由 _rollback 发布（避免重复）
            return None
        if status == "timeout":
            raise TimeoutError(
                f"模型调用超时（{value}）。可能是网络不通、服务端无响应或"
                f"请求过大。已放弃等待，可重试或切换模型（/model）。"
            )
        if status == "error":
            e = value
            # 中断优先：用户已按 Esc 时直接返回，不再重发请求。
            if ctrl.is_set():
                return None
            # 仅在"尚未收到任何 chunk"时才回退到一次性调用（兼容不支持流式的供应商）。
            # 若已经收到部分 chunk 再失败，重发会导致重复计费/重复输出，故直接抛出。
            if state["chunks"] == 0 and not _is_timeout_error(e):
                return await self._ainvoke_model(ctrl)
            raise e

        if value is None:
            # 流式未产出任何内容（部分供应商对空响应如此表现），回退一次性调用
            return await self._ainvoke_model(ctrl)
        # 聚合结果已是 AIMessageChunk，转换为 AIMessage 语义一致
        return value

    async def _ainvoke_model(self, ctrl):
        """非流式兜底调用（原生异步，同样可中断、可超时）。"""
        first_flag = [False]

        async def _do_invoke():
            result = await self.model.ainvoke(self.memory.messages)
            first_flag[0] = True
            return result

        status, value = await self._await_with_interrupt(
            _do_invoke(), ctrl,
            first_byte_timeout=_models.FIRST_BYTE_TIMEOUT,
            total_timeout=_models.TOTAL_TIMEOUT,
            on_wait=self._make_wait_notifier(),
            first_byte_flag=first_flag,
        )
        if status == "interrupted":
            # 中断事件统一由 _rollback 发布（避免重复）
            return None
        if status == "timeout":
            raise TimeoutError(
                f"模型调用超时（{value}）。已放弃等待，可重试或切换模型（/model）。"
            )
        if status == "error":
            raise value
        return value

    async def _rollback(self, start_len: int, stage: str = "") -> None:
        """回滚本轮对话产生的消息，保证历史一致（不残留残缺 tool_call）。"""
        dropped = len(self.memory.messages) - start_len
        del self.memory.messages[start_len:]
        if dropped > 0:
            await self.bus.emit(RollbackEvent(dropped=dropped))
        if stage:
            await self.bus.emit(InterruptEvent(stage=stage))
