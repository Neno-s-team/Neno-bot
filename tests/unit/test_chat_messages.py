from __future__ import annotations

import pytest

import app.services.chat_service as chat_service
from app.services.chat_service import build_chat_messages, run_chat_turn
from app.storage.db import get_session_messages


def _history_fixture() -> list[dict]:
    return [
        {"id": 1, "role": "user", "content": "早上好"},
        {"id": 2, "role": "assistant", "content": "早呀"},
    ]


def test_build_chat_messages_puts_system_prompt_first(isolated_db, monkeypatch):
    monkeypatch.setattr(chat_service, "SYSTEM_PROMPT", "测试人设")
    messages = build_chat_messages([], "在吗")

    assert messages[0]["role"] == "system"
    system_content = messages[0]["content"]
    assert isinstance(system_content, list)
    assert system_content[0]["text"] == chat_service.SYSTEM_PROMPT
    assert system_content[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_build_chat_messages_keeps_history_then_current_message(isolated_db, monkeypatch):
    monkeypatch.setattr(chat_service, "SYSTEM_PROMPT", "测试人设")
    history = _history_fixture()
    messages = build_chat_messages(history, "在吗")

    assert [item["role"] for item in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "早上好"
    assert messages[2]["content"] == "早呀"
    last = messages[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list)
    assert last["content"][0]["text"] == "在吗"


def test_run_chat_turn_persists_user_and_assistant_messages(isolated_db, monkeypatch):
    monkeypatch.setattr(chat_service, "SYSTEM_PROMPT", "测试人设")
    captured: dict = {}

    def fake_generate(messages, trace_id=None, session_id=None):
        captured["messages"] = messages
        captured["trace_id"] = trace_id
        return "收到啦"

    monkeypatch.setattr(chat_service, "generate_chat_reply", fake_generate)

    result = run_chat_turn("s1", "你好", trace_id="t-run")

    assert result["reply"] == "收到啦"
    assert result["trace_id"] == "t-run"
    assert result["user_message_ids"] == [result["user_message_id"]]
    assert result["assistant_message_id"] is not None
    assert result["message_type"] == "text"

    # prompt: system + (空历史) + 当前消息
    roles = [item["role"] for item in captured["messages"]]
    assert roles == ["system", "user"]

    stored = [(item["role"], item["content"]) for item in get_session_messages("s1")]
    assert stored == [("user", "你好"), ("assistant", "收到啦")]


def test_run_chat_turn_excludes_current_message_from_history(isolated_db, monkeypatch):
    seen_histories: list[list[dict]] = []

    def fake_generate(messages, trace_id=None, session_id=None):
        seen_histories.append([item for item in messages if item["role"] != "system"])
        return "嗯嗯"

    monkeypatch.setattr(chat_service, "generate_chat_reply", fake_generate)

    run_chat_turn("s1", "第一句", trace_id="t1")
    run_chat_turn("s1", "第二句", trace_id="t2")

    # 第二轮历史应包含第一轮的 user+assistant，但不含第二轮当前消息
    second_turn = seen_histories[1]
    texts = [item["content"] for item in second_turn]
    assert "第一句" in texts
    assert "嗯嗯" in texts
    assert "第二句" not in texts


def test_run_chat_turn_persists_burst_records_individually(isolated_db, monkeypatch):
    monkeypatch.setattr(chat_service, "generate_chat_reply", lambda messages, trace_id=None, session_id=None: "一起回")

    records = [
        {"content": "第一句", "message_type": "text", "source": "local", "metadata": {"arrival_seq": 1}},
        {"content": "第二句", "message_type": "text", "source": "local", "metadata": {"arrival_seq": 2}},
    ]
    result = run_chat_turn("s1", "第一句\n第二句", trace_id="t-burst", user_records=records)

    assert len(result["user_message_ids"]) == 2
    stored = [(item["role"], item["content"]) for item in get_session_messages("s1")]
    assert stored == [
        ("user", "第一句"),
        ("user", "第二句"),
        ("assistant", "一起回"),
    ]


def test_run_chat_turn_failure_keeps_user_message(isolated_db, monkeypatch):
    def broken_generate(messages, trace_id=None, session_id=None):
        raise RuntimeError("LLM provider error")

    monkeypatch.setattr(chat_service, "generate_chat_reply", broken_generate)

    with pytest.raises(RuntimeError):
        run_chat_turn("s1", "你好", trace_id="t-err")

    stored = [(item["role"], item["content"]) for item in get_session_messages("s1")]
    assert stored == [("user", "你好")]
