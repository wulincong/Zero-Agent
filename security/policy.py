"""
命令准入安全策略（实用主义平衡版）

设计原则：
1. 真正灾难性的操作（删根、格盘、Fork炸弹）才进 DENY。
2. 有副作用但常见开发操作（rm -rf 普通目录、sudo、读取敏感配置）进 CONFIRM，由用户决定。
3. 纯文本字符串（如 commit message、echo 内容）做脱敏识别，避免误杀。
"""

from dataclasses import dataclass
from enum import Enum
import re
import shlex


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
# 1. 绝对拒绝档 (DENY): 纯恶意 / 毁盘 / 提权绕过 / 远程无审计脚本
# ---------------------------------------------------------------------------
DENY_RULES = [
    # 只禁止极其危险的目标：删除根目录、通配符全删、家目录、上级目录
    (r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+.*?(/\s*$|/\*\s*$|~\s*$|\.\.\s*$)", "危险的大范围递归删除（/ 或 ~ 或 ..）"),
    (r"\bmkfs(\.\w+)?\b", "格式化文件系统"),
    (r"\bdd\b.*\bof=/dev/", "直接写入物理块设备"),
    (r":\(\)\s*\{.*\}\s*;\s*:", "Fork 炸弹"),
    (r"\bchmod\s+(-R\s+)?[0-7]*777\s+(/\s*$|/\*\s*$)", "将根目录权限设为 777"),
    (r">\s*/dev/(sd|nvme|hd)", "向磁盘设备重定向写入"),
    (r"\b(shutdown|reboot|poweroff)\b", "关机/重启"),
    (r"\b(curl|wget)\b[^|;]*\|\s*(ba|z|d)?sh\b", "未经审计的管道远程脚本执行"),
    (r"\bhistory\s+-c\b", "清除审计历史"),
    (r"\b(shred|wipe)\b", "不可逆擦除数据"),
    (r">\s*/etc/", "重定向篡改系统配置目录 (/etc)"),
    (r"\bchown\b.*\s+/\s*$", "递归修改根目录属主"),
]

# ---------------------------------------------------------------------------
# 2. 人工确认档 (CONFIRM): 有副作用，但开发日常经常用到
# ---------------------------------------------------------------------------
CONFIRM_RULES = [
    # 递归删除普通目录（如 node_modules, build），允许用户按 y 放行
    (r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\b", "递归删除目录（如 rm -rf）"),
    (r"\brm\b", "删除文件"),
    (r"\bsudo\b", "申请提权执行 (sudo)"),
    (r"\bgit\s+push\b", "推送远程仓库 (git push)"),
    (r"\bgit\s+reset\s+--hard\b", "硬回退放弃本地代码 (git reset --hard)"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*f\b", "强制清理未跟踪文件 (git clean -f)"),
    (r"\b(pip|conda|npm|pnpm|yarn|apt|brew)\s+(install|remove|uninstall|update|upgrade)\b", "安装/卸载/升级软件包"),
    (r"\bkill\s+-[a-zA-Z0-9]*9\b|\bkillall\b", "强制终止进程 (kill -9)"),
    (r"\bsystemctl\s+(start|stop|restart|reload|disable|enable)\b", "更改系统服务状态"),
    (r"\bchmod\s+(-R\s+|--recursive\s+).*[0-7]*777\b", "递归放开权限为 777"),
    (r"\bdd\b.*\bof=", "dd 文件覆盖写入"),
]

# ---------------------------------------------------------------------------
# 3. 只读白名单 (READONLY): 优先放行，避免误报
# ---------------------------------------------------------------------------
READONLY_RULES = [
    (r"\bsystemctl\s+(status|is-active|is-enabled|is-failed|list-units|list-unit-files|show|cat)\b", "服务状态查询"),
    (r"\bcrontab\s+-l\b", "定时任务列出"),
    (r"\bkill\s+-0\b", "进程存活性探测"),
    (r"\bgit\s+push\b.*--dry-run\b", "git push 试运行"),
    (r"\bgit\s+clean\b.*(-n\b|--dry-run\b)", "git clean 试运行"),
    (r"\b(pip|npm|conda)\s+(list|show|view|search)\b", "软件包信息查看"),
]

# ---------------------------------------------------------------------------
# 4. 敏感凭据路径（从 DENY 降级为 CONFIRM，且排除 .example 模板）
# ---------------------------------------------------------------------------
# 路径边界
_B = r"(?:^|[\s/=\"'<>|;])"
_E = r"(?:$|[\s\"'<>|;])"

SENSITIVE_PATTERNS = [
    # 匹配 .env / .env.local，但排除 .env.example / .env.sample / .env.template
    _B + r"\.env(?:\.(?!example|sample|template)[a-zA-Z0-9_-]+)?" + _E,
    _B + r"\.ssh/(?!known_hosts)", # 排除 known_hosts
    r"id_(rsa|dsa|ecdsa|ed25519)",
    _B + r"[a-zA-Z0-9_-]+\.pem" + _E,
    _B + r"[a-zA-Z0-9_-]+\.key" + _E, # 必须是类似 private.key 的文件名，避免单字 key 误杀
    _B + r"\.aws/credentials",
    _B + r"\.netrc" + _E,
]

_DENY_RE = [(re.compile(p, re.IGNORECASE), r) for p, r in DENY_RULES]
_CONFIRM_RE = [(re.compile(p, re.IGNORECASE), r) for p, r in CONFIRM_RULES]
_READONLY_RE = [(re.compile(p, re.IGNORECASE), r) for p, r in READONLY_RULES]
_SENSITIVE_RE = [re.compile(p, re.IGNORECASE) for p in SENSITIVE_PATTERNS]


def _mask_quoted_strings(cmd: str) -> str:
    """
    将命令中引号内的内容（如 commit 信息、echo 内容）脱敏遮蔽。
    防止 git commit -m "fix rm -rf bug" 被错误当成 rm -rf 执行。
    """
    # 匹配双引号或单引号内的内容并替换成安全占位符 __STR__
    cmd_no_double = re.sub(r'"[^"]*"', '"__STR__"', cmd)
    cmd_masked = re.sub(r"'[^']*'", "'__STR__'", cmd_no_double)
    return cmd_masked


def _normalize(cmd: str) -> str:
    """去除换行折叠并统一空白符"""
    s = cmd.replace("\\\n", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def check_command(cmd: str) -> Verdict:
    """对待执行的 Shell 命令进行安全三档判定。"""
    if not cmd or not cmd.strip():
        return Verdict(Decision.ALLOW)

    worst = Verdict(Decision.ALLOW)

    for raw_line in cmd.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        norm_line = _normalize(line)
        # 对字符串内容脱敏，只分析命令骨架
        masked_line = _mask_quoted_strings(norm_line)

        # 1. 优先判定绝对拒绝规则 (DENY)
        for rx, reason in _DENY_RE:
            if rx.search(masked_line):
                return Verdict(Decision.DENY, f"高危指令禁止执行：{reason}")

        # 2. 只读白名单放行（避免误触 CONFIRM）
        if any(rx.search(masked_line) for rx, _ in _READONLY_RE):
            continue

        # 3. 敏感文件访问判定（降级为 CONFIRM 提醒）
        if any(rx.search(masked_line) for rx in _SENSITIVE_RE):
            return Verdict(Decision.CONFIRM, "命令试图访问敏感凭据文件（.env / 私钥 / 证书）")

        # 4. 判定确认规则 (CONFIRM)
        if worst.decision is Decision.ALLOW:
            for rx, reason in _CONFIRM_RE:
                if rx.search(masked_line):
                    worst = Verdict(Decision.CONFIRM, f"需确认操作：{reason}")
                    break

    return worst