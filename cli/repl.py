"""交互层：启动横幅与 REPL 主循环。"""

BANNER = "=" * 60
HELP_TEXT = """\
📖 可用指令：
   /reload  重新加载内核与安全模块（保留对话上下文与 shell 会话）
   /help    显示本帮助
   exit/q   退出"""


def print_banner(assistant) -> None:
    print(BANNER)
    print("🚀 个人专属智能助手已启动！")
    print("   指令: /reload 热重载代码(保留上下文) | /help 帮助 | exit/q 退出")
    print(f"📦 已激活技能库: {list(assistant.tools_registry.keys())}")
    print(BANNER)


def run_repl(assistant) -> None:
    """启动交互式 REPL，直到用户退出。"""
    print_banner(assistant)
    try:
        while True:
            user_prompt = input("\n👤 You > ").strip()
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
