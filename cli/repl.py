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


def _get_console():
    """惰性构造 rich Console（仅在 TTY 下启用渲染）。"""
    global _CONSOLE
    if _CONSOLE is None:
        _CONSOLE = Console()
    return _CONSOLE


def render_markdown(text: str, prefix: str = "\n🤖 Agent > ") -> None:
    """将模型输出的 Markdown 渲染后打印；非 TTY 或 rich 不可用时降级为纯文本。"""
    if not text:
        return
    if _HAS_RICH and sys.stdout.isatty():
        console = _get_console()
        console.print(prefix, end="", highlight=False)
        console.print(Markdown(text))
    else:
        print(f"{prefix}{text}")


BANNER = "=" * 60
HELP_TEXT = """\
📖 可用指令：
   /reload  重新加载内核与安全模块（保留对话上下文与 shell 会话）
   /clear   清空对话上下文（仅保留系统提示词）
   /help    显示本帮助
   exit/q   退出

⌨️  输入技巧：
   Enter          提交
   Alt+Enter      换行（多行输入；部分终端为 Esc 后按 Enter）
   ← → / Home/End 移动光标
   ↑ ↓            翻阅历史
   Esc            中断当前操作（模型调用/命令执行），回到对话
   Ctrl+C         清空当前输入行"""

# ----------------------------------------------------------------------
# 输入后端：优先 prompt_toolkit，失败则降级 input()
# ----------------------------------------------------------------------
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings

    _HAS_PTK = True
except Exception:  # pragma: no cover - 环境缺失时降级
    _HAS_PTK = False


def _build_session():
    """构造带行编辑与多行能力的输入会话；不可用时返回 None。"""
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

    return PromptSession(
        history=InMemoryHistory(),
        key_bindings=kb,
        multiline=True,  # 允许缓冲区含换行；Enter 仍提交（见下方绑定）
    )


_SESSION = None


def _read_input(prompt: str) -> str:
    """读取用户输入（支持多行），优先使用 prompt_toolkit。"""
    global _SESSION
    if _SESSION is None:
        _SESSION = _build_session()
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
    print("   指令: /reload 热重载代码(保留上下文) | /clear 清空上下文 | /help 帮助 | exit/q 退出")
    print("   输入: Enter 提交 | Alt+Enter 换行 | Esc 中断操作 | ←→ 移动光标")
    print(f"📦 已激活技能库: {list(assistant.tools_registry.keys())}")
    print(BANNER)


def run_repl(assistant) -> None:
    """启动交互式 REPL，直到用户退出。"""
    print_banner(assistant)
    try:
        while True:
            try:
                user_prompt = _read_input("\n👤 You > ").strip()
            except EOFError:
                print("\n👋 再见！环境与技能已保存。")
                break
            if not user_prompt:
                continue
            if user_prompt.lower() in ["exit", "quit", "q"]:
                print("👋 再见！环境与技能已保存。")
                break
            if user_prompt.lower() in ["/reload", "/r"]:
                print("\n" + assistant.reload_code())
                continue
            if user_prompt.lower() in ["/clear", "/c"]:
                assistant.memory.clear()
                print("\n🧹 已清空对话上下文，仅保留系统提示词。")
                continue
            if user_prompt.lower() in ["/help", "/h"]:
                print("\n" + HELP_TEXT)
                continue
            response = assistant.chat(user_prompt)
            render_markdown(response)

            if getattr(assistant, "_pending_reload", False):
                print("\n" + assistant.reload_code())
    finally:
        assistant.bash.close()
