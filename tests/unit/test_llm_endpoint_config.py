import importlib


def test_openrouter_url_can_be_overridden_by_environment(monkeypatch):
    import app.config as config

    custom_url = "https://api.example.test/v1/chat/completions"
    monkeypatch.setenv("OPENROUTER_URL", custom_url)

    reloaded = importlib.reload(config)

    assert reloaded.OPENROUTER_URL == custom_url
