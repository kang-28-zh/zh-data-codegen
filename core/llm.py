"""LLM 调用模块。

职责：用 OpenAI SDK 把 prompt 发给模型，返回原始字符串。

约束：
- 不做重试，不做 JSON 解析，不打日志；
- SDK 级超时固定 60 秒；
- SDK 抛出的异常原样上抛，不包装；
- 默认读环境变量，但允许网页端用 set_settings_override 临时覆盖。
"""

import contextvars

import openai

from core.config import Settings, load_settings


# SDK 级超时（秒），避免用户长时间等待
SDK_TIMEOUT_SECONDS = 60

# 运行期临时覆盖（例如网页端用户自填的 Key）；用 ContextVar 隔离并发会话
_settings_override: contextvars.ContextVar[Settings | None] = contextvars.ContextVar(
    "llm_settings_override", default=None
)


def set_settings_override(settings: Settings) -> None:
    """临时覆盖 LLM 配置；调用方负责在结束后 clear。"""
    _settings_override.set(settings)


def clear_settings_override() -> None:
    """清除临时覆盖，恢复为读取环境变量。"""
    _settings_override.set(None)


def call_llm(prompt: str) -> str:
    """调用 OpenAI 兼容接口，返回模型输出的原始文本。"""
    settings = _settings_override.get() or load_settings()
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
