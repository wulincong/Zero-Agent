"""对话历史管理。

把"消息列表"的维护逻辑从主类中抽离，保证系统提示词始终位于首位，
并在热重载后能自愈（首条被替换/丢失时自动补回）。
"""

from langchain_core.messages import HumanMessage, SystemMessage


class ConversationMemory:
    """常驻对话历史：首条固定为系统提示词，后续为对话消息。"""

    def __init__(self, system_prompt: str):
        self.system_prompt = system_prompt
        self.messages = [SystemMessage(content=system_prompt)]

    def ensure_system_prompt(self, system_prompt: str | None = None) -> None:
        """兜底：确保系统提示词位于首位（热重载/异常后自愈）。

        若传入新的 system_prompt，则同步刷新首条消息内容。
        """
        if system_prompt is not None:
            self.system_prompt = system_prompt
        if not self.messages or not isinstance(self.messages[0], SystemMessage):
            self.messages.insert(0, SystemMessage(content=self.system_prompt))
        elif self.messages[0].content != self.system_prompt:
            self.messages[0] = SystemMessage(content=self.system_prompt)

    def add_user(self, content: str) -> None:
        self.messages.append(HumanMessage(content=content))

    def add(self, message) -> None:
        self.messages.append(message)

    def __len__(self) -> int:
        return len(self.messages)

    def __iter__(self):
        return iter(self.messages)
