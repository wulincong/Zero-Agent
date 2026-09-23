"""配置层：从 config.toml 加载可调参数，并支持环境变量覆盖。

设计目标：把散落在各模块里的"魔法数字"集中到一处，用户改配置不必碰代码。

优先级（高 → 低）：
    1. 环境变量（如 AGENT_TOTAL_TIMEOUT）—— 用于临时覆盖，不改文件
    2. config.toml —— 用户持久化配置
    3. 代码内置默认值（本文件 _DEFAULTS）—— 保证配置文件缺失也能跑

用法：
    from core import config
    config.get("timeout.total")          # 300
    config.get_int("context.max_chars")  # 400000

任何异常（文件缺失/格式错误/类型不符）都静默回退到默认值，绝不阻断启动。
"""

import os

# TOML 解析：3.11+ 用内置 tomllib，3.10 用官方 backport tomli
try:
    import tomllib as _toml  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as _toml  # Python 3.10
    except ModuleNotFoundError:
        _toml = None

# 配置文件路径：项目根目录（core/ 的上一级）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(_ROOT, "config.toml")

# ----------------------------------------------------------------------
# 内置默认值：config.toml 缺失或某项未配置时使用
# ----------------------------------------------------------------------
_DEFAULTS: dict = {
    "model": {
        "default_profile": "deepseek",
        "temperature": 0,
        "timeout": 120.0,
        "max_retries": 1,
    },
    "timeout": {
        "first_byte": 60,
        "total": 300,
    },
    "context": {
        "max_chars": 400000,
        "keep_recent": 12,
    },
    "cli": {
        "render_width": 100,
    },
    "shell": {
        "timeout": 180,
    },
}

# 环境变量覆盖表：环境变量名 -> 配置键路径
_ENV_OVERRIDES: dict[str, str] = {
    "AGENT_DEFAULT_MODEL": "model.default_profile",
    "AGENT_FIRST_BYTE_TIMEOUT": "timeout.first_byte",
    "AGENT_TOTAL_TIMEOUT": "timeout.total",
    "AGENT_CONTEXT_MAX_CHARS": "context.max_chars",
    "AGENT_CONTEXT_KEEP_RECENT": "context.keep_recent",
    "AGENT_RENDER_WIDTH": "cli.render_width",
    "AGENT_SHELL_TIMEOUT": "shell.timeout",
}

# 缓存：首次加载后驻留内存；reload() 可强制刷新
_cache: dict | None = None


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并字典，override 覆盖 base（不修改入参）。"""
    result = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _load_file() -> dict:
    """读取 config.toml；失败时返回空字典（由默认值兜底）。"""
    if _toml is None:
        return {}
    try:
        with open(CONFIG_FILE, "rb") as f:
            data = _toml.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _apply_env(data: dict) -> dict:
    """把环境变量覆盖应用到配置字典（就地修改并返回）。"""
    for env_name, path in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        section, _, key = path.partition(".")
        data.setdefault(section, {})[key] = raw  # 先存字符串，取值时再转型
    return data


def _build() -> dict:
    """构建最终配置：默认值 <- 文件 <- 环境变量。"""
    merged = _deep_merge(_DEFAULTS, _load_file())
    return _apply_env(merged)


def reload() -> None:
    """强制重新加载配置（供热重载调用）。"""
    global _cache
    _cache = _build()


def _data() -> dict:
    global _cache
    if _cache is None:
        _cache = _build()
    return _cache


def get(path: str, default=None):
    """按点分路径取值，如 get("timeout.total")。"""
    node = _data()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def get_int(path: str, default: int = 0) -> int:
    """取整数值；无法转换时返回 default。"""
    try:
        return int(get(path, default))
    except (TypeError, ValueError):
        return default


def get_float(path: str, default: float = 0.0) -> float:
    """取浮点值；无法转换时返回 default。"""
    try:
        return float(get(path, default))
    except (TypeError, ValueError):
        return default


def get_str(path: str, default: str = "") -> str:
    """取字符串值。"""
    value = get(path, default)
    return value if isinstance(value, str) else str(value)


def get_optional_float(path: str, default: float | None = None) -> float | None:
    """取浮点值，0 或负值视为"不限制"（返回 None）。"""
    value = get_float(path, default if default is not None else 0.0)
    return value if value > 0 else None


def as_dict() -> dict:
    """返回完整配置字典的副本（供 /config 展示）。"""
    return _deep_merge(_data(), {})
