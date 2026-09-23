"""对话历史管理。

把"消息列表"的维护逻辑从主类中抽离，保证系统提示词始终位于首位，
并在热重载后能自愈（首条被替换/丢失时自动补回）。
"""

from langchain_core.messages import HumanMessage, SystemMessage

# 上下文预算兜底默认值（字符数，粗略按 1 token ≈ 2 字符估算）。
# 注意：实际生效值由 models/llm.py 从 config.toml 的 [context] 段读取后传入，
# 这里的常量仅在调用方未显式传参时兜底，数值应与 config.toml 保持一致。
DEFAULT_MAX_CHARS = 400_000
DEFAULT_KEEP_RECENT = 12


class ConversationMemory:
    """常驻对话历史：首条固定为系统提示词，后续为对话消息。

    内置上下文预算管理：消息总量超过 max_chars 时，自动从最旧处成组丢弃
    （system 提示词永远保留），防止长会话把请求撑爆模型上下文窗口。
    """

    def __init__(self, system_prompt: str, max_chars: int = DEFAULT_MAX_CHARS,
                 keep_recent: int = DEFAULT_KEEP_RECENT):
        self.system_prompt = system_prompt
        self.max_chars = max_chars
        self.keep_recent = keep_recent
        self.messages = [SystemMessage(content=system_prompt)]
        # 累计被丢弃的消息条数（供 /context 展示）
        self.dropped = 0

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

    def clear(self) -> None:
        """清空对话上下文，仅保留系统提示词（首条 system 消息）。"""
        self.messages = [SystemMessage(content=self.system_prompt)]

    def add_user(self, content: str) -> None:
        self.messages.append(HumanMessage(content=content))
        self.enforce_budget()

    def add(self, message) -> None:
        self.messages.append(message)
        self.enforce_budget()

    # ------------------------------------------------------------------
    # 上下文预算管理
    # ------------------------------------------------------------------
    @staticmethod
    def _msg_chars(msg) -> int:
        """粗略估算单条消息占用的字符数（含工具调用参数）。"""
        content = getattr(msg, "content", "") or ""
        if not isinstance(content, str):
            content = str(content)
        total = len(content)
        for tc in (getattr(msg, "tool_calls", None) or []):
            total += len(str(tc.get("name", ""))) + len(str(tc.get("args", "")))
        return total

    def total_chars(self) -> int:
        """当前全部消息的字符总量（含系统提示词）。"""
        return sum(self._msg_chars(m) for m in self.messages)

    def enforce_budget(self) -> int:
        """超预算时从最旧处成组丢弃消息，返回本次丢弃的条数。

        分组规则：一条带 tool_calls 的 AIMessage 与其后续的 ToolMessage
        视为同一组，整组一起丢弃，避免出现"孤儿 tool 结果"导致 API 报错。
        系统提示词（首条）永不丢弃；最近 keep_recent 条消息受保护。
        """
        if self.max_chars <= 0:
            return 0
        dropped = 0
        while len(self.messages) > 1 + self.keep_recent and self.total_chars() > self.max_chars:
            # 从索引 1（跳过 system）开始，切出第一组
            end = 2
            first = self.messages[1]
            if getattr(first, "tool_calls", None):
                while end < len(self.messages) and self.messages[end].__class__.__name__ == "ToolMessage":
                    end += 1
            del self.messages[1:end]
            dropped += end - 1
        self.dropped += dropped
        return dropped

    def stats(self) -> dict:
        """返回上下文占用统计，供 /context 指令展示。"""
        return {
            "messages": len(self.messages),
            "chars": self.total_chars(),
            "max_chars": self.max_chars,
            "dropped": self.dropped,
        }

    def __len__(self) -> int:
        return len(self.messages)

    def __iter__(self):
        return iter(self.messages)
