"""运行时层：常驻 shell 会话等执行基础设施。"""

from runtime.bash import DEFAULT_TIMEOUT, PersistentBash

__all__ = ["PersistentBash", "DEFAULT_TIMEOUT"]
