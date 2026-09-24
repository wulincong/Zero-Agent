"""交互层：启动横幅与 REPL 主循环。

输入采用 prompt_toolkit（若可用），提供完整行编辑能力：
- 左右方向键移动光标、Home/End、Ctrl+A/E、Ctrl+W 删词、历史上下翻；
- 多行输入：Alt+Enter（或 Esc 后 Enter）插入换行，Enter 提交；
- Ctrl+C 清空当前输入行（不退出），Ctrl+D 空行时退出。
若 prompt_toolkit 不可用或非 TTY 环境，自动降级为内置 input()（仅单行）。
"""

import asyncio
import sys
import time

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


class ModelSpinner:
    """模型等待指示器：转圈动画 + 实时读秒（仅 TTY 下启用）。

    呈现形式（全程读秒）：
      - 模型开始响应（ModelStartEvent）后，在**同一行**原地刷新
        `⠋ 思考中 3.2s`，转圈字符每 80ms 变一次，秒数实时递增；
      - **全程读秒**：模型吐字（ModelChunkEvent）时**不停止**转圈，
        继续计时，直到模型阶段结束（ModelStopEvent）才停止，
        并把该行定格为 `✓ 用时 3.2s`（保留一行历史）；
      - 非 TTY 环境（管道/重定向）完全静默，不输出任何字符（Q3(a)）。

    计时起点为 ModelStartEvent（Q4），即请求发出时刻。

    实现：用一个后台 asyncio 任务周期性重绘；所有输出走 sys.stdout.write
    配合 `\r` 覆盖，避免与 rich 的 Markdown 渲染互相干扰。
    """

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    _INTERVAL = 0.08  # 重绘周期（秒）

    def __init__(self):
        self._task = None
        self._start = 0.0
        self._active = False
        self._paused = False  # 暂停期间不重绘（供打印其它内容时让出当前行）
        self._enabled = sys.stdout.isatty()

    # -- 生命周期 ------------------------------------------------------
    def start(self) -> None:
        """模型开始响应：启动转圈任务。"""
        if not self._enabled or self._active:
            return
        self._active = True
        self._paused = False
        self._start = time.monotonic()
        self._task = asyncio.ensure_future(self._spin())

    def stop(self) -> None:
        """模型阶段结束：停止转圈并定格用时行（全程读秒，吐字时不停止）。"""
        if not self._active:
            return
        self._active = False
        self._paused = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        elapsed = time.monotonic() - self._start
        if self._enabled:
            # 清除转圈行，改写为定格的用时行
            sys.stdout.write("\r\033[K")
            sys.stdout.write(f"✓ 用时 {elapsed:.1f}s\n")
            sys.stdout.flush()

    def pause(self) -> None:
        """暂停重绘并清除当前转圈行，让出终端当前行给其它输出。

        用于流式过程中需要打印内容（如工具调用提示）的场景：
        先清除转圈行，打印完再 resume() 继续计时（计时不重置）。
        """
        if not self._active or self._paused:
            return
        self._paused = True
        if self._enabled:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

    def resume(self) -> None:
        """恢复重绘（计时延续，不重置起点）。"""
        if not self._active or not self._paused:
            return
        self._paused = False

    # -- 内部 ----------------------------------------------------------
    async def _spin(self) -> None:
        """后台任务：周期性原地重绘 `⠋ 思考中 X.Xs`（暂停期间跳过重绘）。"""
        i = 0
        try:
            while True:
                if not self._paused:
                    elapsed = time.monotonic() - self._start
                    frame = self._FRAMES[i % len(self._FRAMES)]
                    sys.stdout.write(f"\r\033[K{frame} 思考中 {elapsed:.1f}s")
                    sys.stdout.flush()
                    i += 1
                await asyncio.sleep(self._INTERVAL)
        except asyncio.CancelledError:
            pass


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


def register_event_subscribers(assistant) -> None:
    """把 CLI 的呈现逻辑注册为事件订阅者。

    与旧的"回调工厂"相比，订阅方式让内核不再需要知道 CLI 的存在：
    内核只 emit 事件，CLI 决定怎么呈现。

    订阅内容：
      - ToolCallEvent   -> 打印工具名 / 参数（复用 make_tool_call_printer 的格式）
      - ToolResultEvent -> 折叠输出（复用 make_tool_output_handler 的格式）

    阶段 2 起内核已移除 on_tool_call / on_tool_output 回调参数，
    所有呈现统一走事件总线，因此这里注册的订阅者是唯一的呈现路径。
    """
    from core.events import (
        ModelChunkEvent,
        ModelStartEvent,
        ModelStopEvent,
        ToolCallEvent,
        ToolResultEvent,
    )

    printer = make_tool_call_printer()
    folder = make_tool_output_handler()
    spinner = ModelSpinner()

    def _on_tool_call(event) -> None:
        # 打印前让出转圈行，打印后恢复（计时延续），避免与转圈行混排
        spinner.pause()
        try:
            printer(event.name, event.args)
        finally:
            spinner.resume()

    def _on_tool_result(event) -> None:
        spinner.pause()
        try:
            folder(event.command, event.result)
        finally:
            spinner.resume()

    def _on_model_start(event) -> None:
        spinner.start()

    def _on_model_chunk(event) -> None:
        # 全程读秒：模型吐字时不停止转圈，继续计时到模型阶段结束。
        # （保留订阅以便将来需要"首 token 到达"信号时使用，此处不动作。）
        pass

    def _on_model_stop(event) -> None:
        # 唯一停止点：无论成功/中断/超时/异常都会收到，定格用时行。
        spinner.stop()

    assistant.bus.subscribe(ToolCallEvent, _on_tool_call)
    assistant.bus.subscribe(ToolResultEvent, _on_tool_result)
    assistant.bus.subscribe(ModelStartEvent, _on_model_start)
    assistant.bus.subscribe(ModelChunkEvent, _on_model_chunk)
    assistant.bus.subscribe(ModelStopEvent, _on_model_stop)


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

⏱️  等待与超时：
   模型响应期间会显示转圈动画与实时读秒（如 ⠋ 思考中 3.2s），
   首个 token 到达后定格为"✓ 用时 X.Xs"；
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


async def _read_input(prompt: str, assistant=None) -> str:
    """异步读取用户输入（支持多行），优先使用 prompt_toolkit。

    阶段 2 起为 async：使用 `prompt_async` 以免阻塞事件循环
    （阻塞式 prompt 会让 asyncio 任务无法推进）。
    降级路径（无 prompt_toolkit / 非 TTY）用 `asyncio.to_thread(input, ...)`。
    """
    global _SESSION
    if _SESSION is None:
        _SESSION = _build_session(assistant)
    if _SESSION is not None:
        try:
            return await _SESSION.prompt_async(prompt, prompt_continuation="... ")
        except EOFError:
            raise
        except KeyboardInterrupt:
            return ""
    # 降级路径：仅单行（放到线程池，避免阻塞事件循环）
    return await asyncio.to_thread(input, prompt)


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


async def arun_repl(assistant) -> None:
    """启动交互式 REPL（异步），直到用户退出。

    阶段 2 起为 async：主循环 await 内核的 `achat()`，
    输入用 `prompt_async`，从而让事件循环在等待模型/工具时保持可响应。
    """
    # 注册事件订阅者：内核 emit 事件，CLI 负责呈现
    register_event_subscribers(assistant)
    # 把事件循环绑定到事件总线：供 emit_threadsafe 从工作线程投递事件
    # （阶段 5 起 bash 为原生异步，工具事件已在循环线程中产生；
    #  绑定仍保留，以兼容技能库等可能的工作线程场景）
    try:
        assistant.bus.bind_loop(asyncio.get_running_loop())
    except Exception:
        pass
    print_banner(assistant)
    try:
        while True:
            try:
                user_prompt = (await _read_input("\n👤 You > ", assistant)).strip()
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
            response = await assistant.achat(user_prompt)
            render_markdown(response)

            if getattr(assistant, "_pending_reload", False):
                print("\n" + assistant.reload_code())
    finally:
        await assistant.bash.close()
