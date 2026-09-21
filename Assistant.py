"""Zero-Agent 入口。

职责：载入环境变量、装配内核、启动 REPL。
各层实现分别位于 core/ runtime/ tools/ memory/ models/ security/ cli/。
"""

import os
import sys

from dotenv import load_dotenv

# 从当前目录的环境变量文件载入配置（不覆盖已存在的系统环境变量）
_ENV_FILE = "." + "env"
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), _ENV_FILE))

from cli import run_repl  # noqa: E402
from core import InteractiveAssistant  # noqa: E402


def main() -> None:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit(
            "❌ 未找到 DEEPSEEK_API_KEY。\n"
            f"   请在当前目录创建 {_ENV_FILE} 文件并写入：DEEPSEEK_API_KEY=sk-xxx\n"
            "   或直接设置环境变量：export DEEPSEEK_API_KEY=sk-xxx"
        )

    assistant = InteractiveAssistant(api_key=api_key)
    run_repl(assistant)


if __name__ == "__main__":
    main()
