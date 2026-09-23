"""内核：InteractiveAssistant 主类。

职责：
- 装配各层（runtime / tools / memory / models / security）；
- 驱动多轮对话（模型调用 -> 工具执行 -> 结果回填）；
- 提供热重载能力（原地刷新代码，保留上下文与 shell 会话）。
"""

import glob
import importlib.util
import os
import sys

from langchain_core.messages import SystemMessage

from core.prompts import SYSTEM_PROMPT


class _Interrupted(Exception):
    """内部信号：表示用户中断了当前操作。"""


def _is_timeout_error(exc: Exception) -> bool:
    """判断异常是否为超时/网络类错误（此类错误不应盲目重发请求）。"""
    name = type(exc).__name__.lower()
    if "timeout" in name or "connect" in name:
        return True
    text = str(exc).lower()
    return "timeout" in text or "timed out" in text
from memory import ConversationMemory
import models as _models
from runtime import PersistentBash
from tools import make_bash_tool, make_install_skill_tool, make_reload_tool

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
        self.model_profile = _models.DEFAULT_PROFILE
        self.bash = PersistentBash(confirm_callback=self._confirm_command)
        self.tools_registry = {}
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
    # 兼容属性：messages 直接映射到 memory.messages
    # ------------------------------------------------------------------
    @property
    def messages(self):
        return self.memory.messages

    @messages.setter
    def messages(self, value):
        self.memory.messages = value

    # ------------------------------------------------------------------
    # 工具注册
    # ------------------------------------------------------------------
    @staticmethod
    def _confirm_command(cmd: str, reason: str) -> bool:
        """终端交互确认：危险命令执行前询问用户。

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
            from cli.repl import _read_input
            ans = _read_input("是否执行？[y/N] > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        finally:
            # 恢复监听，并清除期间可能残留的中断标志
            ctrl.clear()
            ctrl.start()
        return ans in ("y", "yes")

    def register_tool(self, tool_func):
        t = tool_func if hasattr(tool_func, "name") else tool_func
        self.tools_registry[t.name] = t

    def _on_tool_output(self, command: str, output: str) -> None:
        """run_bash 的输出回调：转发给当前注册的 on_tool_output 处理器。

        由 chat() 在每轮对话开始时设置 self._tool_output_handler，
        使 CLI 能决定如何呈现（如折叠）。未设置时回退为直接打印。
        """
        handler = getattr(self, "_tool_output_handler", None)
        if handler is not None:
            handler(command, output)
        else:
            print(f"\n💻 [Shell]: {command}")
            print(f"📄 [Output]:\n{output}")

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
        return (
            f"✅ 模型已切换：{old} → {name}（{prof['label']}）\n"
            f"   下一轮对话即生效，对话上下文与 shell 会话保持不变。"
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
        }

        try:
            # 按依赖顺序原地重载各模块（策略/提示词可能被改过）
            for name in RELOADABLE_MODULES:
                if name in sys.modules:
                    self._reload_module(sys.modules[name])
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
    # 对话驱动
    # ------------------------------------------------------------------
    def chat(self, user_input: str, on_tool_call=None, on_tool_output=None) -> str:
        """多轮对话单步驱动器（支持 Esc 中断）。

        Args:
            on_tool_call: 可选回调，用于工具调用流式提示。
                签名 on_tool_call(name, args=None)：
                - 模型刚决定调用工具时，以 (name, None) 调用（参数尚未生成）；
                - 参数聚合完成后，以 (name, args) 再次调用。
            on_tool_output: 可选回调 on_tool_output(command, output)，
                用于接管 run_bash 的命令回显与输出呈现（如折叠）。
        """
        # 兜底：确保系统提示词始终位于对话历史首位（热重载/异常后自愈）
        self.memory.ensure_system_prompt()

        from langchain_core.messages import ToolMessage

        from runtime.interrupt import get_interrupt

        ctrl = get_interrupt()
        ctrl.clear()

        # 记录本轮起始位置，便于中断时回滚，保证历史一致
        start_len = len(self.memory)
        self.memory.add_user(user_input)

        # 设置本轮的工具输出处理器，供 run_bash 回调使用
        self._tool_output_handler = on_tool_output

        ctrl.start()  # 启动 Esc 监听（仅 TTY 下生效）
        try:
            while True:
                # ---- 模型调用（流式，便于及时响应中断）----
                try:
                    ai_msg = self._stream_model(ctrl, on_tool_call=on_tool_call)
                except _Interrupted:
                    self._rollback(start_len)
                    return "⏹️ 已中断（模型调用阶段）。已回到对话。"
                except Exception as e:
                    self._rollback(start_len)
                    return f"❌ 模型调用失败: {e}"

                if ai_msg is None:  # 被中断
                    self._rollback(start_len)
                    return "⏹️ 已中断（模型调用阶段）。已回到对话。"

                self.memory.add(ai_msg)

                if not ai_msg.tool_calls:
                    return ai_msg.content

                # ---- 工具执行 ----
                for call in ai_msg.tool_calls:
                    if ctrl.is_set():
                        self._rollback(start_len)
                        return "⏹️ 已中断（工具执行阶段）。已回到对话。"

                    fn_name = call["name"]
                    fn_args = call["args"]

                    # 参数已聚合完成，通知回调显示完整参数（工具名此前已流式提示过）
                    if on_tool_call:
                        on_tool_call(fn_name, fn_args)

                    target_tool = self.tools_registry.get(fn_name)
                    if not target_tool:
                        res = f"Error: 未找到工具 {fn_name}"
                    else:
                        if fn_name not in ["run_bash", "install_skill"]:
                            print(f"\n🔥 [触发已安装技能]: {fn_name}({fn_args})")
                        try:
                            res = self._invoke_tool(target_tool, fn_args, ctrl)
                        except _Interrupted:
                            self._rollback(start_len)
                            return "⏹️ 已中断（工具执行阶段）。已回到对话。"
                        except Exception as e:
                            # 工具异常不中断对话，回填给模型让它自行纠错
                            res = f"Error: 工具 {fn_name} 执行失败: {type(e).__name__}: {e}"
                            print(f"⚠️ [工具异常]: {res}")

                    self.memory.add(ToolMessage(
                        content=str(res),
                        tool_call_id=call["id"]
                    ))
        finally:
            self._tool_output_handler = None
            ctrl.stop()

    def _stream_model(self, ctrl, on_tool_call=None):
        """流式调用模型并聚合为完整 AIMessage；中断时返回 None。

        流式的好处：每收到一个 chunk 就检查一次中断标志，
        用户按 Esc 后能在极短时间内停止等待。

        Args:
            on_tool_call: 可选回调 on_tool_call(name, args=None)。
                当模型开始生成工具调用时，以 (name, None) 实时通知，
                便于 CLI 在模型"刚决定调用工具"时就给出提示。
        """
        from langchain_core.messages import AIMessageChunk

        aggregated = None
        try:
            for chunk in self.model.stream(self.memory.messages):
                if ctrl.is_set():
                    return None
                # 工具调用流式：工具名一出现就通知（参数逐字符生成，此处不通知）
                if on_tool_call and getattr(chunk, "tool_call_chunks", None):
                    for tc in chunk.tool_call_chunks:
                        if tc.get("name"):
                            on_tool_call(tc["name"])
                aggregated = chunk if aggregated is None else aggregated + chunk
        except Exception as e:
            # 中断优先：用户已按 Esc 时直接返回，不再重发请求。
            if ctrl.is_set():
                return None
            # 仅在"尚未收到任何 chunk"时才回退到一次性调用（兼容不支持流式的供应商）。
            # 若已经收到部分 chunk 再失败，重发会导致重复计费/重复输出，故直接抛出。
            if aggregated is None and not _is_timeout_error(e):
                return self.model.invoke(self.memory.messages)
            raise

        if aggregated is None:
            return self.model.invoke(self.memory.messages)
        # 聚合结果已是 AIMessageChunk，转换为 AIMessage 语义一致
        return aggregated

    def _invoke_tool(self, target_tool, fn_args, ctrl):
        """执行工具。run_bash 内部通过全局中断控制器实现命令级中断。"""
        return target_tool.invoke(fn_args)

    def _rollback(self, start_len: int) -> None:
        """回滚本轮对话产生的消息，保证历史一致（不残留残缺 tool_call）。"""
        del self.memory.messages[start_len:]
