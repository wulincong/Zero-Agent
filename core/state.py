"""会话状态持久化：把"上次退出时的配置"保存到磁盘，下次启动自动恢复。

设计目标：让用户不必每次启动都重新 /model 切换。
当前持久化的字段：
  - model_profile: 上次激活的模型档案名

存储位置：项目根目录下的 .agent_state.json（已加入 .gitignore，不入库）。
读写策略：任何异常（文件损坏 / 无权限 / 磁盘满）都静默降级为默认值，
          绝不因为状态文件问题阻断启动。
"""

import json
import os

# 状态文件路径：项目根目录（core/ 的上一级）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(_ROOT, ".agent_state.json")

# 允许持久化的字段白名单（防止把任意键写进文件）
_ALLOWED_KEYS = {"model_profile"}


def load_state() -> dict:
    """读取持久化状态；文件不存在或损坏时返回空字典。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k in _ALLOWED_KEYS}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}


def save_state(**updates) -> bool:
    """合并写入状态字段（只保留白名单键）。成功返回 True。

    采用"读-改-写"合并语义，避免不同调用点互相覆盖。
    写入使用临时文件 + 原子替换，防止中途崩溃留下半截文件。
    """
    try:
        data = load_state()
        for k, v in updates.items():
            if k in _ALLOWED_KEYS:
                data[k] = v
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
        return True
    except OSError:
        return False


def get_saved_model_profile() -> str | None:
    """读取上次保存的模型档案名；无记录时返回 None。"""
    value = load_state().get("model_profile")
    return value if isinstance(value, str) and value else None


def save_model_profile(name: str) -> bool:
    """保存当前模型档案名。"""
    return save_state(model_profile=name)
