"""内核层：主循环、工具注册、热重载逻辑与系统提示词。"""

from core.assistant import SKILLS_DIR, InteractiveAssistant
from core.prompts import SYSTEM_PROMPT

__all__ = ["InteractiveAssistant", "SYSTEM_PROMPT", "SKILLS_DIR"]
