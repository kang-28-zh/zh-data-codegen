"""配置读取模块。

职责：从环境变量读取运行期配置，只支持一套配置，不读取 .env 文件。

MVP 阶段不引入 dotenv，缺少必需变量时直接报错，不静默使用空字符串。
"""

import os

from pydantic import BaseModel


# 默认值集中定义，便于测试与文档保持一致
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL_NAME = "deepseek-chat"


class Settings(BaseModel):
    """运行配置，字段与 .env.example 保持一致。"""

    openai_api_key: str
    """OpenAI 兼容接口的 API Key，对应环境变量 OPENAI_API_KEY，必需。"""

    openai_base_url: str = DEFAULT_BASE_URL
    """接口基础地址，对应环境变量 OPENAI_BASE_URL。"""

    model_name: str = DEFAULT_MODEL_NAME
    """模型名称，对应环境变量 MODEL_NAME。"""


def load_settings() -> Settings:
    """从环境变量读取配置并返回 Settings。

    读取规则：
    - OPENAI_API_KEY 必需，缺失或为空时抛出 ValueError；
    - OPENAI_BASE_URL / MODEL_NAME 缺失或为空时使用默认值。
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("[config] 缺少必需的环境变量 OPENAI_API_KEY")

    base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or DEFAULT_BASE_URL
    model_name = os.environ.get("MODEL_NAME", "").strip() or DEFAULT_MODEL_NAME

    return Settings(
        openai_api_key=api_key,
        openai_base_url=base_url,
        model_name=model_name,
    )
