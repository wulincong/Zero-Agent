"""命令准入安全策略（第一层）。

设计目标：
1. 把 shell 命令分成三档：ALLOW（放行）/ CONFIRM（需用户确认）/ DENY（拒绝）。
2. 检查逻辑与执行逻辑解耦，便于单测与后续扩展。
3. 用 shlex 做词法切分，降低 "rm  -rf"（多空格）等简单绕过。

注意：这是"第一层"防线，基于模式匹配，无法覆盖所有绕过手法。
真正的根治是后续把文件操作拆成受控工具（第四层）。
"""

import re
import shlex
from dataclasses import dataclass
from enum import Enum


class Decision(Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass
class Verdict:
    decision: Decision
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.decision is Decision.DENY


# ---------------------------------------------------------------------------
# 策略定义
# ---------------------------------------------------------------------------

# 直接拒绝：灾难性 / 不可逆 / 提权 / 远程代码执行
DENY_RULES = [
    (r"\brm\s+(-[a-zA-Z]+\s+)*-[a-zA-Z]*[rf]", "递归/强制删除（rm -rf）"),
    (r"\bmkfs(\.\w+)?\b", "格式化文件系统"),
    (r"\bdd\b.*\bof=/dev/", "直接写块设备"),
    (r":\(\)\s*\{.*\}\s*;\s*:", "fork 炸弹"),
    (r"\bchmod\s+(-R\s+)?[0-7]*777\s+/(\s|$)", "对根目录放开权限"),
    (r">\s*/dev/(sd|nvme|hd)", "重定向写入块设备"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "关机/重启"),
    (r"\bsudo\b", "提权操作"),
    (r"\b(su|doas)\b\s", "切换用户/提权"),
    (r"\b(curl|wget)\b[^|;]*\|\s*(ba|z|d)?sh\b", "管道执行远程脚本"),
    (r"\b(curl|wget)\b[^|;]*\|\s*(python|perl|ruby)\b", "管道执行远程脚本"),
    (r"\bhistory\s+-c\b", "清除命令历史（反审计）"),
    (r"\b(shred|wipe)\b", "不可逆擦除"),
    (r">\s*/etc/", "写入系统配置目录"),
    (r"\bchown\b.*\s/(\s|$)", "修改根目录属主"),
]

# 需用户确认：有副作用但可能合理
CONFIRM_RULES = [
    (r"\brm\b", "删除文件"),
    (r"\bmv\b", "移动/重命名文件"),
    (r"\bgit\s+push\b", "推送到远程仓库"),
    (r"\bgit\s+reset\s+--hard\b", "丢弃本地改动"),
    (r"\bgit\s+clean\b", "清理未跟踪文件"),
    (r"\b(pip|conda|npm|apt|yum)\s+(install|remove|uninstall|update|upgrade)\b", "安装/卸载软件包"),
    (r"\bkill(all)?\b", "终止进程"),
    (r"\btruncate\b", "截断文件"),
    (r">\s*\S+", "重定向覆盖写入文件"),
    (r"\btee\b", "写入文件"),
    (r"\bcrontab\b", "修改定时任务"),
    (r"\bsystemctl\b", "修改系统服务"),
]

# 敏感文件：读取时屏蔽（防止密钥泄露）
# 路径边界：行首、空白、/、=、引号、重定向符等
_B = r"(?:^|[\s/=\"'<>|;])"
_E = r"(?:$|[\s\"'<>|;])"

SENSITIVE_PATH_PATTERNS = [
    _B + r"\.env(?:\.|" + _E + r")",
    _B + r"\.ssh/",
    r"id_(rsa|dsa|ecdsa|ed25519)",
    r"\.pem" + _E,
    r"\.key" + _E,
    _B + r"\.aws/",
    _B + r"\.config/gcloud/",
    _B + r"\.netrc" + _E,
    _B + r"\.git-credentials" + _E,
    r"credentials(?:\.json)?" + _E,
]

_DENY_RE = [(re.compile(p, re.IGNORECASE), r) for p, r in DENY_RULES]
_CONFIRM_RE = [(re.compile(p, re.IGNORECASE), r) for p, r in CONFIRM_RULES]
_SENSITIVE_RE = [re.compile(p, re.IGNORECASE) for p in SENSITIVE_PATH_PATTERNS]


def _normalize(cmd: str) -> str:
    """把命令做轻量归一化，降低简单绕过。

    - 去掉反斜杠续行
    - 折叠多余空白
    - 去掉引号（r''m -> rm）
    """
    s = cmd.replace("\\\n", " ")
    s = re.sub(r"\s+", " ", s)
    s = s.replace("'", "").replace('"', "")
    return s.strip()


def check_command(cmd: str) -> Verdict:
    """对单条命令做准入判定。

    返回 Verdict(decision, reason)。多行命令逐行判定，取最严结果。
    """
    if not cmd or not cmd.strip():
        return Verdict(Decision.ALLOW)

    worst = Verdict(Decision.ALLOW)

    for line in cmd.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        norm = _normalize(line)

        # 1) 拒绝档
        for rx, reason in _DENY_RE:
            if rx.search(norm):
                return Verdict(Decision.DENY, f"命中拒绝规则：{reason}")

        # 2) 敏感文件读取
        for rx in _SENSITIVE_RE:
            if rx.search(norm):
                return Verdict(Decision.DENY, "尝试访问敏感文件（密钥/凭据）")

        # 3) 确认档
        if worst.decision is Decision.ALLOW:
            for rx, reason in _CONFIRM_RE:
                if rx.search(norm):
                    worst = Verdict(Decision.CONFIRM, f"命中确认规则：{reason}")
                    break

    return worst


def is_sensitive_path(path: str) -> bool:
    """判断路径是否属于敏感文件（供后续文件工具复用）。"""
    return any(rx.search(path) for rx in _SENSITIVE_RE)
