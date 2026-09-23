"""模型层：LLM 客户端与供应商配置。

本模块通过 __getattr__ 动态转发到 models.llm，确保热重载 models.llm 后
（如新增模型档案、修改默认档案），这里暴露的常量/函数始终是最新的，
不会持有陈旧引用。
"""

from models import llm as _llm

__all__ = [
    "build_model",
    "build_model_from_profile",
    "get_profile",
    "resolve_api_key",
    "MODEL_PROFILES",
    "DEFAULT_PROFILE",
    "DEFAULT_MODEL",
    "DEFAULT_BASE_URL",
    "DEFAULT_TEMPERATURE",
]


def __getattr__(name: str):
    """动态转发到 models.llm，保证热重载后引用不陈旧。"""
    if name in __all__:
        return getattr(_llm, name)
    raise AttributeError(f"module 'models' has no attribute '{name}'")
