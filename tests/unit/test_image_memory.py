import json

import pytest
from PIL import Image

from app import config
from app.llm import openrouter_client as provider
from app.schemas import MediaAttachment
from app.services import chat_service
from app.services.chat import image_memory as memory, llm_gateway, maintenance
from app.storage import db, image_store as store


def make_attachment(tmp_path, session="images"):
    source = tmp_path / "input.png"
    Image.new("RGB", (48, 32), "red").save(source)
    attachment = memory.prepare_attachments(session, [MediaAttachment(
        kind="image", media_path=str(source), mime_type="image/jpeg"
    )], "红色测试图")[0]
    return source, attachment


def image_count(messages):
    return sum(block.get("type") == "image_url" for message in messages
               if isinstance(message.get("content"), list) for block in message["content"])


def send_image(attachment, session="images"):
    return chat_service.run_chat_turn(session, "看这张图", user_records=[{
        "content": "看这张图", "message_type": "image", "metadata": {
            "attachments": [attachment.model_dump()]
        }
    }])


def test_five_turn_window_archives_once_and_never_stores_base64(isolated_db, tmp_path, monkeypatch):
    seen, archives = [], []
    monkeypatch.setattr(config, "IMAGE_HISTORY_TURNS", 5)
    monkeypatch.setattr(config, "IMAGE_ARCHIVE_MODEL_NAME", "archive-fixture")
    monkeypatch.setattr(chat_service, "generate_chat_reply", lambda messages, **kw: seen.append(messages) or "收到")
    source, attachment = make_attachment(tmp_path)
    monkeypatch.setattr(llm_gateway, "request_model_response", lambda **kw:
                        archives.append(kw) or json.dumps({attachment.image_id: "纯红色矩形，没有文字。"}))
    send_image(attachment)
    source.unlink()
    for number in range(2, 6):
        chat_service.run_chat_turn("images", f"第{number}轮")
    assert [image_count(messages) for messages in seen] == [1] * 5
    assert archives == []
    chat_service.run_chat_turn("images", "第六轮")
    for _ in range(4):
        maintenance.run_one()
        if archives:
            break
    chat_service.run_chat_turn("images", "第七轮")
    assert image_count(seen[-2]) == image_count(seen[-1]) == 0
    assert len(archives) == 1
    assert archives[0]["model_name"] == "archive-fixture"
    assert archives[0]["extra_body"] == config.IMAGE_ARCHIVE_EXTRA_BODY
    assert image_count(archives[0]["messages"]) == 1
    assert "纯红色矩形" in json.dumps(seen[-1], ensure_ascii=False)
    assert attachment.mime_type == "image/png"
    stored = db.get_session_messages("images")
    assert len(stored) == 14
    assert "base64," not in json.dumps(stored)
    assert store.next_turn("images") == 8


def test_recall_and_search_survive_compaction_and_restart(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(chat_service, "generate_chat_reply", lambda *a, **kw: "收到")
    _, attachment = make_attachment(tmp_path)
    send_image(attachment)
    store.save_description("images", attachment.image_id, "红色矩形测试图")
    store.complete_turn("images", 8)
    # Simulate a future compactor replacing the message rows, not user deletion.
    db.execute_write("DELETE FROM messages WHERE session_id=?", ("images",))
    db.init_db()
    assert store.next_turn("images") == 9
    assert store.search_images("images", "红色")[0]["image_id"] == attachment.image_id
    assert store.search_images("images", attachment.image_id)[0]["image_id"] == attachment.image_id
    assert store.search_images("images", "%_") == []
    result = memory.execute_image_tool("images", "view_image", {"image_id": attachment.image_id})
    assert result[-1]["image_url"]["url"].startswith("data:image/png;base64,")
    _, blocks, index = memory.decorate_context([], "images", 9, [])
    assert blocks == [] and attachment.image_id in index
    with pytest.raises(ValueError):
        memory.execute_image_tool("another-session", "view_image", {"image_id": attachment.image_id})
    db.clear_session_messages("images")
    assert not store.search_images("images")


def test_recent_image_survives_text_token_trimming(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(chat_service, "generate_chat_reply", lambda *a, **kw: "收到")
    _, attachment = make_attachment(tmp_path)
    send_image(attachment)
    history, _, _ = memory.decorate_context([], "images", 2, [])
    assert image_count(history) == 1
    assert attachment.image_id in json.dumps(history)


def test_archive_failure_does_not_block_chat_or_destroy_pixels(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(chat_service, "generate_chat_reply", lambda *a, **kw: "收到")
    _, attachment = make_attachment(tmp_path)
    send_image(attachment)
    store.complete_turn("images", 5)
    calls = []

    def unavailable(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("temporary provider error")

    monkeypatch.setattr(llm_gateway, "request_model_response", unavailable)
    assert chat_service.run_chat_turn("images", "继续聊")["reply"] == "收到"
    for _ in range(4):
        maintenance.run_one()
        if calls:
            break
    assert chat_service.run_chat_turn("images", "再聊")["reply"] == "收到"
    assert len(calls) == 1
    assert memory.execute_image_tool("images", "view_image", {"image_id": attachment.image_id})[-1]["type"] == "image_url"


def test_image_tools_produce_one_final_database_reply(isolated_db, tmp_path, monkeypatch):
    _, attachment = make_attachment(tmp_path)
    recorded = []

    def model(**kwargs):
        recorded.append(kwargs)
        if len(recorded) == 1:
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": "lookup", "type": "function", "function": {
                    "name": "search_images", "arguments": '{"query":""}'}}]}
        if len(recorded) == 2:
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": "revisit", "type": "function", "function": {
                    "name": "view_image", "arguments": json.dumps({"image_id": attachment.image_id})}}]}
        return {"role": "assistant", "content": "确实是红色。"}

    monkeypatch.setattr(llm_gateway, "request_model_response", model)
    assert send_image(attachment)["reply"] == "确实是红色。"
    assert len(db.get_session_messages("images")) == 2
    tool_results = [message for message in recorded[-1]["messages"] if message["role"] == "tool"]
    assert len(tool_results) == 2 and tool_results[-1]["content"][-1]["type"] == "image_url"
    assert store.next_turn("images") == 2
    assert all(request["model_name"] == config.CHAT_MODEL_NAME for request in recorded)


def test_native_anthropic_preserves_cache_signature_and_image_tool_results():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    native = [{"type": "thinking", "thinking": "test", "signature": "signed-test"},
              {"type": "tool_use", "id": "look", "name": "view_image", "input": {"image_id": "img_test"}}]
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "tool instructions",
                                         "cache_control": {"type": "ephemeral", "ttl": "1h"}}]},
        {"role": "user", "content": "看看旧图"},
        {"role": "assistant", "content": "", "_anthropic_content": native},
        {"role": "tool", "tool_call_id": "look", "content": [image]},
    ]
    system, converted = provider._anthropic_messages(messages)
    assert system[0]["cache_control"]["ttl"] == "1h"
    assert converted[1]["content"] == native
    result = converted[2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "look"
    assert result["content"][0]["source"]["type"] == "base64"


def test_openai_image_results_follow_all_tool_responses():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    payload = provider._openai_messages([
        {"role": "tool", "tool_call_id": "a", "content": [{"type": "text", "text": "img_a"}, image]},
        {"role": "tool", "tool_call_id": "b", "content": "done", "is_error": False},
    ])
    assert [item["role"] for item in payload] == ["tool", "tool", "user"]
    assert payload[0]["content"] == "img_a"
    assert payload[-1]["content"][-1] == image
    assert "is_error" not in payload[1]


def test_persona_can_be_disabled_without_disabling_image_tools(isolated_db, monkeypatch):
    monkeypatch.setattr(chat_service, "SYSTEM_PROMPT", "")
    assert chat_service.build_chat_messages([], "你好") == [
        {"role": "user", "content": [{"type": "text", "text": "你好"}]}]
    messages = chat_service.build_chat_messages([], "看图", image_tools=True)
    assert messages[0]["content"][0]["text"] == memory.IMAGE_INSTRUCTIONS
    assert len(messages[0]["content"]) == 1


def test_aggregated_image_messages_share_one_turn(isolated_db, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(chat_service, "generate_chat_reply", lambda messages, **kw: seen.append(messages) or "收到两张")
    _, first = make_attachment(tmp_path)
    _, second = make_attachment(tmp_path)
    result = chat_service.run_chat_turn("images", "连续两张图", user_records=[{
        "content": "看图", "message_type": "image",
        "metadata": {"attachments": [attachment.model_dump()]},
    } for attachment in (first, second)])
    assert len(result["user_message_ids"]) == 2
    assert image_count(seen[0]) == 2
    assert store.next_turn("images") == 2
    assert {asset["turn_index"] for asset in store.search_images("images")} == {1}
