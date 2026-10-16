from __future__ import annotations

from app.storage.db import (
    add_message,
    clear_session_messages,
    delete_message_turn,
    get_message_by_id,
    get_recent_messages_by_tokens,
    get_session_messages,
    get_sessions,
)


def test_add_and_list_messages_roundtrip(isolated_db):
    user_id = add_message("s1", "user", "你好", trace_id="t1")
    assistant_id = add_message("s1", "assistant", "嗨", trace_id="t1")

    messages = get_session_messages("s1")
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "你好"
    assert messages[0]["id"] == user_id
    assert messages[1]["id"] == assistant_id


def test_messages_from_other_sessions_are_isolated(isolated_db):
    add_message("s1", "user", "只在 s1")
    add_message("s2", "user", "只在 s2")

    assert [item["content"] for item in get_session_messages("s1")] == ["只在 s1"]
    assert [item["content"] for item in get_session_messages("s2")] == ["只在 s2"]


def test_metadata_snapshot_is_persisted_and_readable(isolated_db):
    add_message("s1", "user", "带元数据", trace_id="t-meta", metadata={"source": "local", "trace_id": "t-meta"})

    item = get_message_by_id(1)
    assert item["metadata"]["source"] == "local"
    assert item["metadata"]["trace_id"] == "t-meta"
    assert item["trace_id"] == "t-meta"


def test_recent_messages_by_tokens_keeps_most_recent_order(isolated_db):
    for index in range(6):
        add_message("s1", "user", f"消息{index}")
        add_message("s1", "assistant", f"回复{index}")

    recent = get_recent_messages_by_tokens("s1", token_limit=20)
    assert len(recent) >= 2
    assert recent[-1]["content"] == "回复5"
    # 越新的消息越靠后（时间顺序）
    contents = [item["content"] for item in recent]
    assert contents == sorted(
        contents, key=lambda c: int("".join(ch for ch in c if ch.isdigit()))
    )


def test_clear_session_messages(isolated_db):
    add_message("s1", "user", "a")
    add_message("s1", "assistant", "b")
    add_message("s2", "user", "keep me")

    deleted = clear_session_messages("s1")
    assert deleted == 2
    assert get_session_messages("s1") == []
    assert len(get_session_messages("s2")) == 1


def test_get_sessions_lists_session_summaries(isolated_db):
    add_message("s1", "user", "a")
    add_message("s1", "assistant", "b")
    add_message("s2", "user", "c")

    sessions = get_sessions()
    assert {item["session_id"] for item in sessions} == {"s1", "s2"}
    counts = {item["session_id"]: item["message_count"] for item in sessions}
    assert counts == {"s1": 2, "s2": 1}


def test_delete_message_turn_removes_whole_trace(isolated_db):
    add_message("s1", "user", "问", trace_id="turn-1")
    add_message("s1", "assistant", "答", trace_id="turn-1")
    add_message("s1", "user", "下一轮", trace_id="turn-2")

    result = delete_message_turn(1)
    assert result["deleted_count"] == 2
    remaining = [item["trace_id"] for item in get_session_messages("s1")]
    assert remaining == ["turn-2"]
