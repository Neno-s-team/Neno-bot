"""提供方和档位：模型名写"提供方:模型名"就走那家；额外参数换成那家认的写法；花费没报就按价格表估。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.llm import presets, providers


def test_model_names_pick_their_provider(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.delenv("DEEPSEEK_URL", raising=False)
    assert providers.split("deepseek:deepseek-v4-pro") == ("deepseek", "deepseek-v4-pro")
    assert providers.split("openrouter:google/gemini-3.8-flash:nitro") == ("openrouter", "google/gemini-3.8-flash:nitro")
    assert providers.split("z-ai/glm-5.3-flash:batch") == ("", "z-ai/glm-5.3-flash:batch")     # OpenRouter 自己的冒号后缀
    assert providers.resolve("deepseek:deepseek-v4-pro", "https://openrouter.ai/x", "or-key") == (
        "deepseek", "https://api.deepseek.com/chat/completions", "ds-key", "deepseek-v4-pro")
    # 没写提供方：调用方给什么就用什么（老配置、本机网关都走这条）
    assert providers.resolve("google/gemini-4-carbon", "https://openrouter.ai/api/v1/chat/completions", "or-key") == (
        "openrouter", "https://openrouter.ai/api/v1/chat/completions", "or-key", "google/gemini-4-carbon")
    assert providers.resolve("auto/x", "http://127.0.0.1:3425/v1/chat/completions", "local")[0] == ""
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    with pytest.raises(providers.ProviderError, match="DEEPSEEK_API_KEY"):
        providers.resolve("deepseek:deepseek-flash")
    with pytest.raises(providers.ProviderError, match="CUSTOM_LLM_URL"):
        providers.resolve("custom:qwen-max")
    monkeypatch.setenv("CUSTOM_LLM_URL", "https://my.host/v1/chat/completions")
    monkeypatch.setenv("CUSTOM_LLM_API_KEY", "k")
    assert providers.resolve("custom:qwen-max")[:2] == ("custom", "https://my.host/v1/chat/completions")


def test_openrouter_style_extras_are_translated_for_deepseek():
    schema = {"type": "object", "required": ["gut"], "properties": {"gut": {"type": "string"}}}
    payload = {
        "model": "deepseek-v4-pro", "max_tokens": 800, "stop": ["\nUser"], "session_id": "neno:abc",
        "reasoning": {"effort": "medium"}, "provider": {"require_parameters": True},
        "response_format": {"type": "json_schema", "json_schema": {"name": "x", "strict": True, "schema": schema}},
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": "自我卡", "cache_control": {"type": "ephemeral"}}]},
            {"role": "assistant", "content": "嗯", "reasoning_details": [{"x": 1}]},
            {"role": "user", "content": "在干嘛"},
        ],
    }
    assert providers.adapt("openrouter", payload) is payload and providers.adapt("", payload) is payload
    body = providers.adapt("deepseek", payload)
    assert body["reasoning_effort"] == "high" and "reasoning" not in body and "provider" not in body and "session_id" not in body
    assert body["response_format"] == {"type": "json_object"} and body["max_tokens"] == 800 and body["stop"] == ["\nUser"]
    system, assistant, user = body["messages"]
    assert system == {"role": "system", "content": "自我卡"} and assistant == {"role": "assistant", "content": "嗯"}
    assert user["content"].startswith("在干嘛\n\n只输出一个 JSON 对象") and '"required":["gut"]' in user["content"]
    assert payload["messages"][2]["content"] == "在干嘛"                              # 不改调用方手里的那份
    # 官方默认开着思考：没要求就明确关掉；要求了什么力度就换成它的说法
    assert providers.adapt("deepseek", {"model": "deepseek-flash", "messages": []})["reasoning_effort"] == "none"
    for said, want in (("none", "none"), ("low", "low"), ("high", "high"), ("xhigh", "high"), ("max", "max")):
        assert providers.adapt("deepseek", {"messages": [], "reasoning": {"effort": said}})["reasoning_effort"] == want
    assert providers.adapt("deepseek", {"messages": [], "reasoning": {"enabled": False}})["reasoning_effort"] == "none"
    assert providers.adapt("deepseek", {"messages": [], "reasoning_effort": "max"})["reasoning_effort"] == "max"   # 已经是它的写法
    picture = {"role": "user", "content": [{"type": "text", "text": "看这张", "cache_control": {}},
                                           {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}]}
    kept = providers.adapt("deepseek", {"messages": [picture]})["messages"][0]["content"]
    assert kept == [{"type": "text", "text": "看这张"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}]


def test_cost_is_estimated_when_the_provider_does_not_report_it():
    usage = {"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 400_000, "completion_tokens": 100_000}
    sunday = datetime(2026, 11, 15, 2, 0, tzinfo=timezone.utc).timestamp()
    monday_peak = datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc).timestamp()
    monday_quiet = datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc).timestamp()
    quiet = 0.4 * 0.022 + 0.6 * 0.66 + 0.1 * 1.98
    assert providers.cost("deepseek", "deepseek-v4-pro", usage, sunday) == pytest.approx(quiet)
    assert providers.cost("deepseek", "deepseek-v4-pro", usage, monday_quiet) == pytest.approx(quiet)
    assert providers.cost("deepseek", "deepseek-v4-pro", usage, monday_peak) == pytest.approx(quiet * 2)   # 高峰翻倍
    assert providers.cost("deepseek", "deepseek-flash", {"prompt_tokens": 1000, "completion_tokens": 1000}, sunday) == pytest.approx(
        (1000 * 0.15 + 1000 * 0.6) / 1e6)
    assert providers.cost("openrouter", "x", usage) is None and providers.cost("deepseek", "没见过的", usage) is None


def test_a_preset_fills_every_layer_but_keeps_what_you_wrote():
    env: dict[str, str] = {}
    assert presets.apply(env) == "" and env == {}                                    # 没设档位：什么都不动
    env = {"MODEL_PRESET": "cn"}
    assert presets.apply(env) == "cn"
    assert env["MIND_L1_MODEL"] == env["WORLD_NPC_MODEL"] == env["OPENROUTER_VISION_MODEL"] == "deepseek:deepseek-flash"
    assert env["MIND_THINK_MODEL"] == env["MIND_EXPR_MODEL"] == env["OPENROUTER_CHAT_MODEL"] == "deepseek:deepseek-v4-pro"
    assert providers.needed_keys(list(presets.models("cn").values())) == ["DEEPSEEK_API_KEY"]
    env = {"MODEL_PRESET": "CN-Gemini", "MIND_L1_MODEL": "openrouter:z-ai/glm-5.3-flash"}
    presets.apply(env)
    assert env["MIND_L1_MODEL"] == "openrouter:z-ai/glm-5.3-flash"                    # 单独写的那层不被档位盖掉
    assert env["MIND_EXPR_MODEL"] == "openrouter:google/gemini-3.8-flash" and env["MIND_THINK_MODEL"] == "deepseek:deepseek-v4-pro"
    assert sorted(providers.needed_keys(list(presets.models("cn-gemini").values()))) == ["DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"]
    assert providers.needed_keys(list(presets.models("intl").values())) == ["OPENROUTER_API_KEY"]
    assert presets.models("high")["MIND_THINK_MODEL"] == "openrouter:anthropic/claude-opus-5.5"
    assert providers.needed_keys(["google/gemini-4-carbon", ""]) == ["OPENROUTER_API_KEY"]    # 老写法算 OpenRouter
    with pytest.raises(ValueError, match="不认识"):
        presets.apply({"MODEL_PRESET": "cheap"})
    assert set(presets.LABELS) == set(presets.PRESETS)


def test_a_call_goes_to_the_named_provider_and_is_billed_to_it(isolated_db, monkeypatch):
    from app.llm.openrouter_client import chat_with_openrouter
    from app.services import usage
    from app.storage.operations import operations

    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    sent = {}

    class Reply:
        ok, status_code, text = True, 200, ""

        def json(self):
            return {"id": "req-1", "choices": [{"message": {"content": "在呢", "reasoning_content": "想了想"}}],
                    "usage": {"prompt_tokens": 2000, "prompt_cache_hit_tokens": 1000, "completion_tokens": 500,
                              "completion_tokens_details": {"reasoning_tokens": 300}}}

    monkeypatch.setattr(usage.requests, "post", lambda url, **kw: sent.update(url=url, **kw) or Reply())
    said = chat_with_openrouter(api_key="or-key", url="https://openrouter.ai/api/v1/chat/completions",
                                model_name="deepseek:deepseek-v4-pro", messages=[{"role": "user", "content": "在吗"}],
                                extra_body={"reasoning": {"effort": "medium"}}, max_tokens=300, session_id="me", purpose="mind_think")
    assert said == "在呢"
    assert sent["url"] == "https://api.deepseek.com/chat/completions" and sent["headers"]["Authorization"] == "Bearer ds-key"
    assert sent["json"]["model"] == "deepseek-v4-pro" and sent["json"]["reasoning_effort"] == "high" and "session_id" not in sent["json"]
    row = operations().one("SELECT * FROM llm_usage")
    assert row["provider"] == "deepseek" and row["model"] == "deepseek-v4-pro" and row["purpose"] == "mind_think"
    assert row["input_tokens"] == 2000 and row["reasoning_tokens"] == 300 and row["cost_usd"] > 0
    assert row["cost_usd"] == pytest.approx(providers.cost("deepseek", "deepseek-v4-pro", Reply().json()["usage"], row["created_at"]))
    with pytest.raises(RuntimeError, match="密钥"):                                   # 没写提供方、也没有 OpenRouter 密钥
        chat_with_openrouter(api_key="", url="https://openrouter.ai/api/v1/chat/completions", model_name="x/y", messages=[])
