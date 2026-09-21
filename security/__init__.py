"""安全防线：命令准入策略（ALLOW / CONFIRM / DENY）。

对外暴露 policy 模块的核心接口，保持 `from security import check_command, Decision`
的既有用法不变。
"""

from security.policy import (
    Decision,
    Verdict,
    check_command,
    is_sensitive_path,
)

__all__ = ["Decision", "Verdict", "check_command", "is_sensitive_path"]
