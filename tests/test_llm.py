"""core/llm.py 的单元测试。

只验证 SDK 接线参数，不发起真实网络请求。
"""

from unittest.mock import MagicMock, patch

from core.llm import call_llm


def test_call_llm_passes_timeout_and_settings(monkeypatch):
    """timeout 必须是 60，model 来自 MODEL_NAME，密钥与地址来自环境变量。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("MODEL_NAME", "test-model")

    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = "hello"

    with patch("openai.OpenAI") as fake_openai:
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_response
        fake_openai.return_value = fake_client

        result = call_llm("prompt")

    assert result == "hello"

    init_kwargs = fake_openai.call_args.kwargs
    assert init_kwargs["timeout"] == 60
    assert init_kwargs["api_key"] == "sk-test"
    assert init_kwargs["base_url"] == "https://example.com/v1"

    create_kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert create_kwargs["model"] == "test-model"
