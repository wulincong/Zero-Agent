"""模型层：LLM 客户端构造与多模型档案（profile）管理。

设计目标：让"切换模型"变成一次配置查找，而不是改代码。
每个 profile 描述一个可用的模型端点（供应商 / 模型名 / base_url / 密钥环境变量），
`build_model` 只负责按 profile 构造客户端。

新增一个模型只需在 MODEL_PROFILES 里加一条，无需改动其它层。
"""

import os

from langchain_openai import ChatOpenAI

from core import config as _config

# ----------------------------------------------------------------------
# 通用默认参数
# ----------------------------------------------------------------------
DEFAULT_TEMPERATURE = _config.get_float("model.temperature", 0)

# 请求超时（秒）。流式场景下 httpx 的 read 超时是"相邻两个 chunk 之间的最大间隔"，
# 因此这里给一个较宽松的值：既能容忍长思考/长输出，又能在连接被挂起时及时失败，
# 避免"提交后永远没返回"。
DEFAULT_TIMEOUT = _config.get_float("model.timeout", 120.0)
# 失败重试次数。设为 1 表示不自动重试，避免超时场景下等待时间翻倍。
DEFAULT_MAX_RETRIES = _config.get_int("model.max_retries", 1)

# ----------------------------------------------------------------------
# 调用级超时（由内核的线程化调用器强制执行，独立于 httpx 的 socket 超时）
# ----------------------------------------------------------------------
# 为什么需要它：httpx 的 read timeout 只在"连接已建立且 socket 正常"时可靠生效。
# 当连接处于半开状态（服务端不回 FIN/RST）或网关挂起时，socket 可能长时间不返回，
# 表现为"卡死、Esc 无效、无报错"。内核把调用放进工作线程，由主线程按下面的
# 阈值强制放弃等待，从而保证一定能退出。
#
# 首字节超时：连接建立后，多久没收到任何数据就判定为挂起（秒）。
# 可用环境变量 AGENT_FIRST_BYTE_TIMEOUT 覆盖；设为 0 表示不限制。
FIRST_BYTE_TIMEOUT = _config.get_optional_float("timeout.first_byte", 60)
# 整体超时：单次模型调用的最长等待时间（秒），兜底防止无限等待。
# 可用环境变量 AGENT_TOTAL_TIMEOUT 覆盖；设为 0 表示不限制。
TOTAL_TIMEOUT = _config.get_optional_float("timeout.total", 300)

# ----------------------------------------------------------------------
# 上下文预算（字符数，粗略按 1 token ≈ 2 字符估算）
# ----------------------------------------------------------------------
# 超过该预算时，记忆层会从最旧处成组丢弃消息，避免请求超出模型上下文窗口
# （典型报错：maximum context length is N tokens, however you requested M tokens）。
# 可用环境变量 AGENT_CONTEXT_MAX_CHARS 覆盖；设为 0 表示不限制。
CONTEXT_MAX_CHARS = _config.get_int("context.max_chars", 400000)
# 触发压缩后至少保留的最近消息条数。
CONTEXT_KEEP_RECENT = _config.get_int("context.keep_recent", 12)


# ----------------------------------------------------------------------
# 模型档案：每个条目描述一个可切换的模型端点
# ----------------------------------------------------------------------
# 字段说明：
#   model        : 传给供应商的模型名
#   base_url     : OpenAI 兼容端点地址
#   api_key_env  : 从哪个环境变量读取密钥
#   label        : 展示用名称
#   temperature  : 可选，覆盖默认温度
#
# 说明：Gemini 使用官方提供的 OpenAI 兼容端点，因此无需额外依赖
#       （不必安装 langchain-google-genai），直接复用 ChatOpenAI。
MODEL_PROFILES: dict[str, dict] = {
    "deepseek": {
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "label": "DeepSeek Chat",
    },
    "deepseek-reasoner": {
        "model": "deepseek-reasoner",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "label": "DeepSeek Reasoner (R1)",
    },
    "gemini": {
        "model": "gemini-2.5-flash",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GEMINI_API_KEY",
        "label": "Gemini 2.5 Flash",
    },
    "gemini-pro": {
        "model": "gemini-3.1-pro-preview",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GEMINI_API_KEY",
        "label": "Gemini 3.1 Pro (preview)",
    },
}

# 默认档案名（可用环境变量 AGENT_DEFAULT_MODEL 覆盖）
DEFAULT_PROFILE = _config.get_str("model.default_profile", "deepseek")


def get_profile(name: str) -> dict:
    """按名称取档案；不存在时抛出 KeyError（附带可用列表）。"""
    if name not in MODEL_PROFILES:
        raise KeyError(
            f"未知模型档案 '{name}'。可用: {', '.join(MODEL_PROFILES)}"
        )
    return MODEL_PROFILES[name]


def resolve_api_key(profile: dict) -> str | None:
    """从档案指定的环境变量读取密钥；缺失时返回 None。"""
    return os.environ.get(profile["api_key_env"])


def build_model(api_key: str, tools, base_url: str | None = None,
                model: str | None = None, temperature: float = DEFAULT_TEMPERATURE,
                timeout: float = DEFAULT_TIMEOUT,
                max_retries: int = DEFAULT_MAX_RETRIES):
    """构造绑定了工具的对话模型（底层通用构造器）。

    base_url / model 为 None 时，取默认档案（DEFAULT_PROFILE）对应的值。
    """
    profile = MODEL_PROFILES[DEFAULT_PROFILE]
    return ChatOpenAI(
        model=model or profile["model"],
        api_key=api_key,
        base_url=base_url or profile["base_url"],
        temperature=temperature,
        timeout=timeout,
        max_retries=max_retries,
    ).bind_tools(list(tools))


def build_model_from_profile(profile_name: str, tools, api_key: str | None = None,
                             temperature: float | None = None,
                             timeout: float = DEFAULT_TIMEOUT,
                             max_retries: int = DEFAULT_MAX_RETRIES):
    """按档案名构造模型。api_key 为 None 时自动从环境变量读取。

    Raises:
        KeyError: 档案名不存在。
        ValueError: 档案对应的密钥环境变量未设置。
    """
    profile = get_profile(profile_name)
    key = api_key or resolve_api_key(profile)
    if not key:
        raise ValueError(
            f"模型 '{profile_name}' 需要环境变量 {profile['api_key_env']}，但未设置。"
        )
    return build_model(
        api_key=key,
        tools=tools,
        base_url=profile["base_url"],
        model=profile["model"],
        temperature=profile.get("temperature", DEFAULT_TEMPERATURE)
        if temperature is None else temperature,
        timeout=timeout,
        max_retries=max_retries,
    )
