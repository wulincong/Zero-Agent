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
from models import DEFAULT_PROFILE, MODEL_PROFILES, resolve_api_key  # noqa: E402


def main() -> None:
    profile = MODEL_PROFILES.get(DEFAULT_PROFILE)
    if profile is None:
        sys.exit(f"❌ 默认模型档案 '{DEFAULT_PROFILE}' 不存在，请检查 models/llm.py")

    if not resolve_api_key(profile):
        env_name = profile["api_key_env"]
        sys.exit(
            f"❌ 默认模型 '{DEFAULT_PROFILE}' 需要环境变量 {env_name}，但未找到。\n"
            f"   请在当前目录的 {_ENV_FILE} 中写入：{env_name}=xxx\n"
            f"   或直接设置环境变量：export {env_name}=xxx\n"
            f"   也可用 AGENT_DEFAULT_MODEL 指定其它默认档案。"
        )

    assistant = InteractiveAssistant(api_key=None)
    run_repl(assistant)


if __name__ == "__main__":
    main()
