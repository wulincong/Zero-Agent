"""交互层：启动横幅与 REPL 主循环。

输入采用 prompt_toolkit（若可用），提供完整行编辑能力：
- 左右方向键移动光标、Home/End、Ctrl+A/E、Ctrl+W 删词、历史上下翻；
- 多行输入：Alt+Enter（或 Esc 后 Enter）插入换行，Enter 提交；
- Ctrl+C 清空当前输入行（不退出），Ctrl+D 空行时退出。
若 prompt_toolkit 不可用或非 TTY 环境，自动降级为内置 input()（仅单行）。
"""

import sys

# ----------------------------------------------------------------------
# 输出后端：优先 rich 渲染 Markdown，失败则降级为纯文本
# ----------------------------------------------------------------------
try:
    from rich.console import Console
    from rich.markdown import Markdown

    _HAS_RICH = True
except Exception:  # pragma: no cover - 环境缺失时降级
    _HAS_RICH = False

_CONSOLE = None

# 渲染宽度上限：避免 Markdown（尤其表格/段落）铺满整个终端。
# 可用环境变量 AGENT_RENDER_WIDTH 覆盖；设为 0 表示不限制（用终端全宽）。
_DEFAULT_MAX_WIDTH = 100


def _resolve_width() -> int | None:
    """计算渲染宽度：min(终端宽度, 上限)。返回 None 表示不限制。

    上限来自 config.toml 的 [cli].render_width（环境变量 AGENT_RENDER_WIDTH 可覆盖）。
    """
    import shutil

    from core import config as _config

    limit = _config.get_int("cli.render_width", _DEFAULT_MAX_WIDTH)
    if limit <= 0:
        return None

    term_cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    return min(term_cols, limit)


def _get_console():
    """惰性构造 rich Console（仅在 TTY 下启用渲染），并限制渲染宽度。"""
    global _CONSOLE
    if _CONSOLE is None:
        _CONSOLE = Console(width=_resolve_width())
    return _CONSOLE


def render_markdown(text: str, prefix: str = "🤖 Agent > ") -> None:
    """将模型输出的 Markdown 渲染后打印；非 TTY 或 rich 不可用时降级为纯文本。

    前缀单独成行打印，保证与正文之间有换行。
    """
    if not text:
        return
    if _HAS_RICH and sys.stdout.isatty():
        console = _get_console()
        console.print()  # 空行：与上一段输出分隔
        console.print(prefix, highlight=False)
        console.print(Markdown(text))
    else:
        print(f"\n{prefix}\n{text}")


def make_tool_call_printer():
    """构造工具调用流式提示回调。

    模型刚决定调用工具时打印工具名；参数聚合完成后打印完整参数。
    返回的回调签名：on_tool_call(name, args=None)。
    """
    def on_tool_call(name: str, args=None) -> None:
        if args is None:
            print(f"\n🔧 调用工具: {name}", flush=True)
        else:
            print(f"   参数: {args}", flush=True)
    return on_tool_call


# 最近一次工具输出（供 /expand 展开）
_LAST_TOOL_OUTPUT = {"command": None, "output": None}


def make_tool_output_handler():
    """构造工具输出处理器：默认折叠，仅显示命令与输出行数。

    返回的回调签名：on_tool_output(command, output)。
    完整输出被暂存，用户输入 /expand 时可展开查看。
    """
    def on_tool_output(command: str, output: str) -> None:
        _LAST_TOOL_OUTPUT["command"] = command
        _LAST_TOOL_OUTPUT["output"] = output
        lines = output.count("\n") + (1 if output and not output.endswith("\n") else 0)
        print(f"\n💻 [Shell]: {command}", flush=True)
        print(f"   └─ 输出 {lines} 行已折叠（/expand 查看）", flush=True)
    return on_tool_output


def expand_last_tool_output() -> None:
    """展开最近一次工具输出。"""
    cmd = _LAST_TOOL_OUTPUT["command"]
    out = _LAST_TOOL_OUTPUT["output"]
    if cmd is None:
        print("\n（暂无可展开的工具输出）")
        return
    print(f"\n💻 [Shell]: {cmd}")
    print(f"📄 [Output]:\n{out}")


BANNER = "=" * 60
HELP_TEXT = """\
📖 可用指令：
   /model   查看可用模型；/model <名称> 切换模型（如 /model gemini）
   /reload  重新加载内核与安全模块（保留对话上下文与 shell 会话）
   /clear   清空对话上下文（仅保留系统提示词）
   /context 查看上下文占用（消息数 / 字符数 / 预算）
   /config  查看当前生效的配置（来自 config.toml）
   /expand  展开最近一次工具输出（默认折叠）
   /help    显示本帮助
   exit/q   退出

⌨️  输入技巧：
   Enter          提交
   Alt+Enter      换行（多行输入；部分终端为 Esc 后按 Enter）
   ← → / Home/End 移动光标
   ↑ ↓            翻阅历史
   Tab            补全指令与模型名（如 /model 后按 Tab）
   Esc            中断当前操作（模型调用/命令执行），回到对话
   Ctrl+C         清空当前输入行

⏱️  超时保护：
   模型调用若长时间无响应，会周期性提示"仍在响应中"；
   超过首字节超时（默认 60s）或整体超时（默认 300s）会自动放弃并报错，
   不会无限卡死。可在 config.toml 的 [timeout] 段调整。"""

# ----------------------------------------------------------------------
# 输入后端：优先 prompt_toolkit，失败则降级 input()
# ----------------------------------------------------------------------
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings

    _HAS_PTK = True
except Exception:  # pragma: no cover - 环境缺失时降级
    _HAS_PTK = False


# 顶层指令（用于 Tab 补全）
_COMMANDS = ["/model", "/reload", "/clear", "/context", "/config", "/expand", "/help", "exit", "quit"]


if _HAS_PTK:
    class _AgentCompleter(Completer):
        """REPL 补全器：

        - 行首输入 `/` 时补全指令名；
        - 输入 `/model ` 后补全模型档案名（由 model_provider 动态提供）。
        """

        def __init__(self, model_provider=None):
            # model_provider: 返回当前可用档案名列表的可调用对象
            self._model_provider = model_provider

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            # 仅对单行输入补全（多行输入不干扰）
            if "\n" in text:
                return

            stripped = text.lstrip()
            # 场景一：/model <前缀> —— 补全模型档案名
            if stripped.startswith("/model "):
                prefix = stripped[len("/model "):]
                if " " in prefix:  # 已经输入了完整参数，不再补全
                    return
                for name in self._model_names():
                    if name.startswith(prefix):
                        yield Completion(name, start_position=-len(prefix))
                return

            # 场景二：行首以 / 开头 —— 补全指令名
            if stripped.startswith("/") and " " not in stripped:
                for cmd in _COMMANDS:
                    if cmd.startswith(stripped):
                        yield Completion(cmd, start_position=-len(stripped))

        def _model_names(self):
            if self._model_provider is None:
                return []
            try:
                return list(self._model_provider())
            except Exception:
                return []


def _build_session(assistant=None):
    """构造带行编辑、多行与 Tab 补全的输入会话；不可用时返回 None。"""
    if not _HAS_PTK:
        return None
    # 仅在真正的 TTY 下启用，避免管道/重定向场景报错
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None

    kb = KeyBindings()

    @kb.add("enter")  # 显式绑定：Enter 提交（multiline 下默认会变成换行）
    def _(event):
        """提交当前输入。"""
        event.app.current_buffer.validate_and_handle()

    def _newline(event):
        """插入换行，实现多行输入。"""
        event.app.current_buffer.insert_text("\n")

    # 换行：仅保留 Alt+Enter（部分终端为 Esc 后按 Enter）。
    # 说明：Shift+Enter 在 VS Code 终端会直接触发提交、Ctrl+J 与 VS Code 冲突，
    # 故均不绑定，避免误提交/冲突。
    kb.add("escape", "enter")(_newline)

    @kb.add("c-c")
    def _(event):
        """Ctrl+C：清空当前输入行，不退出程序。"""
        event.app.current_buffer.reset()

    @kb.add("c-d")
    def _(event):
        """Ctrl+D：空行时退出。"""
        if not event.app.current_buffer.text:
            event.app.exit(exception=EOFError)

    # 补全器：模型档案名从 assistant 动态获取（切换/热重载后自动同步）
    def _model_names():
        from models import MODEL_PROFILES
        return list(MODEL_PROFILES.keys())

    completer = _AgentCompleter(model_provider=_model_names)

    return PromptSession(
        history=InMemoryHistory(),
        key_bindings=kb,
        completer=completer,
        complete_while_typing=False,  # 仅在按 Tab 时补全，避免干扰正常输入
        multiline=True,  # 允许缓冲区含换行；Enter 仍提交（见下方绑定）
    )


_SESSION = None


def _read_input(prompt: str, assistant=None) -> str:
    """读取用户输入（支持多行），优先使用 prompt_toolkit。"""
    global _SESSION
    if _SESSION is None:
        _SESSION = _build_session(assistant)
    if _SESSION is not None:
        try:
            return _SESSION.prompt(prompt, prompt_continuation="... ")
        except EOFError:
            raise
        except KeyboardInterrupt:
            return ""
    # 降级路径：仅单行
    return input(prompt)


def print_banner(assistant) -> None:
    print(BANNER)
    print("🚀 个人专属智能助手已启动！")
    print("   指令: /model 切换模型 | /reload 热重载代码(保留上下文) | /clear 清空上下文 | /help 帮助 | exit/q 退出")
    print("   输入: Enter 提交 | Alt+Enter 换行 | Esc 中断操作 | ←→ 移动光标")
    print(f"🧠 当前模型: {getattr(assistant, 'model_profile', '?')}")
    print(f"📦 已激活技能库: {list(assistant.tools_registry.keys())}")
    print(BANNER)


def _render_config() -> str:
    """渲染当前生效的配置（来自 config.toml，含环境变量覆盖）。"""
    from core import config as _config

    data = _config.as_dict()
    lines = [f"⚙️  当前配置（{_config.CONFIG_FILE}）"]
    for section, values in data.items():
        lines.append(f"  [{section}]")
        if isinstance(values, dict):
            for k, v in values.items():
                lines.append(f"    {k} = {v}")
        else:
            lines.append(f"    {values}")
    lines.append("\n修改 config.toml 后，用 /reload 即可生效（密钥仍在 .env）。")
    return "\n".join(lines)


def _save_session_state(assistant) -> None:
    """退出时持久化会话配置（当前模型档案），供下次启动恢复。"""
    try:
        from core import state as _state
        profile = getattr(assistant, "model_profile", None)
        if profile:
            _state.save_model_profile(profile)
    except Exception:
        pass  # 保存失败不影响退出


def run_repl(assistant) -> None:
    """启动交互式 REPL，直到用户退出。"""
    print_banner(assistant)
    try:
        while True:
            try:
                user_prompt = _read_input("\n👤 You > ", assistant).strip()
            except EOFError:
                _save_session_state(assistant)
                print("\n👋 再见！环境与技能已保存。")
                break
            if not user_prompt:
                continue
            if user_prompt.lower() in ["exit", "quit", "q"]:
                _save_session_state(assistant)
                print("👋 再见！环境与技能已保存。")
                break
            if user_prompt.lower().startswith("/model"):
                arg = user_prompt[len("/model"):].strip()
                print("\n" + assistant.switch_model(arg))
                continue
            if user_prompt.lower() in ["/reload", "/r"]:
                print("\n" + assistant.reload_code())
                continue
            if user_prompt.lower() in ["/clear", "/c"]:
                assistant.memory.clear()
                print("\n🧹 已清空对话上下文，仅保留系统提示词。")
                continue
            if user_prompt.lower() in ["/context", "/ctx"]:
                print("\n" + assistant.context_report())
                continue
            if user_prompt.lower() in ["/config", "/cfg"]:
                print("\n" + _render_config())
                continue
            if user_prompt.lower() in ["/expand", "/e"]:
                expand_last_tool_output()
                continue
            if user_prompt.lower() in ["/help", "/h"]:
                print("\n" + HELP_TEXT)
                continue
            response = assistant.chat(
                user_prompt,
                on_tool_call=make_tool_call_printer(),
                on_tool_output=make_tool_output_handler(),
            )
            render_markdown(response)

            if getattr(assistant, "_pending_reload", False):
                print("\n" + assistant.reload_code())
    finally:
        assistant.bash.close()
