"""模型层：LLM 客户端构造。

集中管理模型名、base_url、温度等参数，便于后续切换供应商或模型。
"""

from langchain_openai import ChatOpenAI

DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_TEMPERATURE = 0


def build_model(api_key: str, tools, base_url: str = DEFAULT_BASE_URL,
                model: str = DEFAULT_MODEL, temperature: float = DEFAULT_TEMPERATURE):
    """构造绑定了工具的对话模型。"""
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
    ).bind_tools(list(tools))
