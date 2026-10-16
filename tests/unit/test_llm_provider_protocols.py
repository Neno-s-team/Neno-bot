from types import SimpleNamespace

from app.llm import openrouter_client


def test_anthropic_messages_payload_extracts_system_and_converts_blocks(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(ok=True, headers={}, status_code=200, json=lambda: {
            "content": [{"type": "text", "text": "回复成功"}],
        })

    monkeypatch.setattr(openrouter_client.requests, "post", fake_post)

    result = openrouter_client.chat_with_openrouter(
        api_key="secret",
        url="https://api.example.test/v1/messages",
        model_name="claude-fable-5",
        messages=[
            {"role": "system", "content": [{"type": "text", "text": "系统提示"}]},
            {"role": "user", "content": [{"type": "text", "text": "你好"}]},
        ],
        max_tokens=100,
    )

    assert result == "回复成功"
    assert captured["headers"]["x-api-key"] == "secret"
    assert captured["json"]["system"] == "系统提示"
    assert captured["json"]["messages"] == [{"role": "user", "content": [{"type": "text", "text": "你好"}]}]
    assert captured["json"]["max_tokens"] == 100


def test_anthropic_response_concatenates_text_blocks(monkeypatch):
    response = SimpleNamespace(
        ok=True,
        headers={},
        status_code=200,
        json=lambda: {"content": [
            {"type": "text", "text": "第一段"},
            {"type": "text", "text": "第二段"},
        ]},
    )
    monkeypatch.setattr(openrouter_client.requests, "post", lambda *args, **kwargs: response)

    result = openrouter_client.chat_with_openrouter(
        api_key="secret",
        url="https://api.example.test/v1/messages",
        model_name="claude-fable-5",
        messages=[{"role": "user", "content": "你好"}],
    )

    assert result == "第一段第二段"
