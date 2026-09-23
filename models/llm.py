"""模型层：LLM 客户端构造。

集中管理模型名、base_url、温度、超时等参数，便于后续切换供应商或模型。
"""

from langchain_openai import ChatOpenAI

DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_TEMPERATURE = 0

# 请求超时（秒）。流式场景下 httpx 的 read 超时是"相邻两个 chunk 之间的最大间隔"，
# 因此这里给一个较宽松的值：既能容忍长思考/长输出，又能在连接被挂起时及时失败，
# 避免"提交后永远没返回"。
DEFAULT_TIMEOUT = 120.0
# 失败重试次数。设为 1 表示不自动重试，避免超时场景下等待时间翻倍。
DEFAULT_MAX_RETRIES = 1


def build_model(api_key: str, tools, base_url: str = DEFAULT_BASE_URL,
                model: str = DEFAULT_MODEL, temperature: float = DEFAULT_TEMPERATURE,
                timeout: float = DEFAULT_TIMEOUT,
                max_retries: int = DEFAULT_MAX_RETRIES):
    """构造绑定了工具的对话模型。"""
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        timeout=timeout,
        max_retries=max_retries,
    ).bind_tools(list(tools))
