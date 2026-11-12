"""core/config.py 的单元测试。

全部使用 monkeypatch 修改环境变量，不依赖真实 .env 或真实 API Key。
"""

import pytest

from core import config


def test_load_settings_all_variables(monkeypatch):
    """三个环境变量齐全时应原样读取。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("MODEL_NAME", "my-model")

    settings = config.load_settings()

    assert settings.openai_api_key == "sk-test-key"
    assert settings.openai_base_url == "https://example.com/v1"
    assert settings.model_name == "my-model"


def test_load_settings_missing_api_key(monkeypatch):
    """缺少 OPENAI_API_KEY 时应报错，且消息包含模块前缀与变量名。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ValueError) as exc_info:
        config.load_settings()

    message = str(exc_info.value)
    assert message.startswith("[config]")
    assert "OPENAI_API_KEY" in message


def test_load_settings_defaults(monkeypatch):
    """缺少可选变量时应使用默认值，而不是空字符串。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)

    settings = config.load_settings()

    assert settings.openai_base_url == config.DEFAULT_BASE_URL
    assert settings.model_name == config.DEFAULT_MODEL_NAME
