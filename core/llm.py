"""LLM 调用模块。

职责：用 OpenAI SDK 把 prompt 发给模型，返回原始字符串。

约束：
- 不做重试，不做 JSON 解析，不打日志；
- SDK 级超时固定 60 秒；
- SDK 抛出的异常原样上抛，不包装。
"""

import openai

from core.config import load_settings


# SDK 级超时（秒），避免用户长时间等待
SDK_TIMEOUT_SECONDS = 60


def call_llm(prompt: str) -> str:
    """调用 OpenAI 兼容接口，返回模型输出的原始文本。"""
    settings = load_settings()
    client = openai.OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        timeout=SDK_TIMEOUT_SECONDS,
    )
    response = client.chat.completions.create(
        model=settings.model_name,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or ""
