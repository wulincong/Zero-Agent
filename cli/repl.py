"""交互层：启动横幅与 REPL 主循环。

输入采用 prompt_toolkit（若可用），提供完整行编辑能力：
- 左右方向键移动光标、Home/End、Ctrl+A/E、Ctrl+W 删词、历史上下翻；
- 多行输入：Alt+Enter（或 Esc 后 Enter、Ctrl+J）插入换行，Enter 提交；
- Ctrl+C 清空当前输入行（不退出），Ctrl+D 空行时退出。
若 prompt_toolkit 不可用或非 TTY 环境，自动降级为内置 input()（仅单行）。
"""

import sys

BANNER = "=" * 60
HELP_TEXT = """\
📖 可用指令：
   /reload  重新加载内核与安全模块（保留对话上下文与 shell 会话）
   /help    显示本帮助
   exit/q   退出

⌨️  输入技巧：
   Enter          提交
   Shift+Enter    换行（多行输入，VS Code 终端支持）
   Alt+Enter      换行（备用；部分终端为 Esc 后按 Enter）
   Ctrl+J         换行（备用）
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

    # Shift+Enter：终端需支持扩展键盘协议（VS Code 终端支持）。
    # VS Code 发送 CSI u 格式：ESC [ 1 3 ; 2 u
    # 注意：旧式 modifyOtherKeys 序列 ESC[27;2;13~ 已被 prompt_toolkit
    # 内置映射为 ControlM（等同 Enter），无法在此层区分，故不绑定。
    kb.add("escape", "[", "1", "3", ";", "2", "u")(_newline)

    # 兜底：Alt+Enter / Esc 后 Enter / Ctrl+J 同样换行
    kb.add("escape", "enter")(_newline)
    kb.add("c-j")(_newline)

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
    print("   指令: /reload 热重载代码(保留上下文) | /help 帮助 | exit/q 退出")
    print("   输入: Enter 提交 | Shift+Enter 换行 | Esc 中断操作 | ←→ 移动光标")
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
            if user_prompt.lower() in ["/help", "/h"]:
                print("\n" + HELP_TEXT)
                continue
            response = assistant.chat(user_prompt)
            print(f"\n🤖 Agent > {response}")

            if getattr(assistant, "_pending_reload", False):
                print("\n" + assistant.reload_code())
    finally:
        assistant.bash.close()
