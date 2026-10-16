from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import app.services.chat_service as chat_service
import app.security as security
from app import config
from app.storage import db
from app.storage.operations import operations
from app.schemas import MediaAttachment
from app.services.chat import multimodal_input_service


def _mock_reply(monkeypatch, reply: str = "测试回复") -> None:
    monkeypatch.setattr(
        chat_service, "generate_chat_reply", lambda messages, trace_id=None, session_id=None: reply
    )


def test_root_returns_200(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_returns_200_and_db_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["db"] == "ok"
    assert "recent_errors_24h" in payload


def test_chat_request_response_contract(client, fast_aggregation, monkeypatch):
    _mock_reply(monkeypatch, "你好呀")

    response = client.post("/chat", json={"message": "你好", "session_id": "s-contract"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["reply"] == "你好呀"
    assert payload["session_id"] == "s-contract"
    assert payload["trace_id"]
    assert isinstance(payload["user_message_id"], int)
    assert isinstance(payload["assistant_message_id"], int)
    assert payload["user_message_ids"] == [payload["user_message_id"]]
    assert payload["message_type"] == "text"
    assert isinstance(payload["latency_ms"], int)
    assert payload["latency_ms"] >= 0


def test_chat_requires_internal_token(isolated_db, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main_module

    calls = []
    monkeypatch.setattr(security, "INTERNAL_TOKEN", "required-secret")
    monkeypatch.setattr(chat_service, "generate_chat_reply", lambda *a, **kw: calls.append(True) or "no")
    with TestClient(main_module.app, raise_server_exceptions=False) as unauthenticated:
        missing = unauthenticated.post("/chat", json={"message": "x", "session_id": "auth"})
        wrong = unauthenticated.post("/chat", headers={"X-Internal-Token": "wrong"},
                                     json={"message": "x", "session_id": "auth"})
    assert missing.status_code == wrong.status_code == 403
    assert not calls and not db.get_session_messages("auth")


def test_chat_does_not_reflect_unconfigured_cross_origin(client):
    response = client.options("/chat", headers={
        "Origin": "https://audit-attacker.invalid",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-internal-token,content-type",
    })
    assert response.headers.get("access-control-allow-origin") is None


def test_chat_persists_history(client, fast_aggregation, monkeypatch, admin_headers):
    _mock_reply(monkeypatch, "记住了")

    client.post("/chat", json={"message": "记一下这句话", "session_id": "s-history"})

    response = client.get(
        "/session/messages", params={"session_id": "s-history"}, headers=admin_headers
    )
    assert response.status_code == 200
    messages = response.json()["messages"]
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "记一下这句话"),
        ("assistant", "记住了"),
    ]


def test_chat_rejects_blank_message(client):
    response = client.post("/chat", json={"message": "   ", "session_id": "s-blank"})
    assert response.status_code == 400


def test_chat_rejects_blank_session_id(client):
    response = client.post("/chat", json={"message": "你好", "session_id": "  "})
    assert response.status_code == 400


def test_chat_rejects_noncanonical_telegram_sessions(client):
    for session_id in ("tg:invalid", "tg:", "tg:+123", "tg:00123", "tg: 123", "tg:9223372036854775808"):
        response = client.post("/chat", json={"message": "x", "session_id": session_id})
        assert response.status_code == 400, session_id


def test_historical_invalid_telegram_session_can_be_cleared_without_peer_cancellation(
    client, admin_headers, isolated_db
):
    store = operations()
    db.add_message("tg:+123", "user", "legacy alias")
    store.write("INSERT INTO inbox(key,chat_id,message_id,data,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                ("legacy-canonical", 123, 1, "{}", time.time(), time.time()))
    store.write("INSERT INTO outbox(key,inbox_key,seq,chat_id,request,updated_at) VALUES (?,?,?,?,?,?)",
                ("legacy-canonical", "legacy-canonical", 0, 123, "{}", time.time()))
    response = client.post("/session/clear", headers=admin_headers, json={"session_id": "tg:+123"})
    assert response.status_code == 200
    assert not db.get_session_messages("tg:+123")
    assert store.one("SELECT state FROM outbox WHERE key=?", ("legacy-canonical",))["state"] == "pending"


def test_idempotency_uses_canonical_session_before_fingerprint(client, admin_headers, fast_aggregation, monkeypatch):
    _mock_reply(monkeypatch, "canonical")
    request = {"request_id": "canonical-request", "message": "x", "session_id": " canonical-session "}
    first = client.post("/chat", json=request)
    assert first.status_code == 200 and first.json()["session_id"] == "canonical-session"
    assert client.post("/session/clear", headers=admin_headers,
                       json={"session_id": "canonical-session"}).status_code == 200
    replay = client.post("/chat", json=request)
    assert replay.status_code == 410


def test_chat_returns_500_when_provider_fails(client, fast_aggregation, monkeypatch):
    def broken(messages, trace_id=None, session_id=None):
        raise RuntimeError("LLM provider error")

    monkeypatch.setattr(chat_service, "generate_chat_reply", broken)

    response = client.post("/chat", json={"message": "你好", "session_id": "s-err"})
    assert response.status_code == 500


def test_chat_merges_burst_messages_into_one_turn(client, monkeypatch):
    import app.routers.chat as chat_router
    from app.services.session_aggregation_controller import SessionAggregationController

    controller = SessionAggregationController(window_seconds=0.8)
    monkeypatch.setattr(chat_router, "session_aggregation_controller", controller)
    _mock_reply(monkeypatch, "一起回你们")

    def post(index: int):
        return client.post(
            "/chat", json={"message": f"第{index}句", "session_id": "s-burst"}
        ).json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(post, 1)
        time.sleep(0.1)
        second = pool.submit(post, 2)
        first_payload = first.result()
        second_payload = second.result()

    # 两条突发消息合并成一个 turn：同一个 assistant 回复，两条 user 消息都落库
    assert first_payload["assistant_message_id"] == second_payload["assistant_message_id"]
    assert sorted(first_payload["user_message_ids"]) == sorted(second_payload["user_message_ids"])
    assert len(first_payload["user_message_ids"]) == 2


def test_chat_serializes_same_session_turns(client, fast_aggregation, monkeypatch):
    lock = threading.Lock()
    state = {"active": 0, "max_active": 0}

    def slow_generate(messages, trace_id=None, session_id=None):
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        try:
            time.sleep(0.05)
            return f"回复{len(messages)}"
        finally:
            with lock:
                state["active"] -= 1

    monkeypatch.setattr(chat_service, "generate_chat_reply", slow_generate)

    def post(index: int):
        response = client.post(
            "/chat", json={"message": f"消息{index}", "session_id": "s-serial"}
        )
        return response.status_code

    with ThreadPoolExecutor(max_workers=3) as pool:
        statuses = list(pool.map(post, range(6)))

    assert statuses == [200] * 6
    assert state["max_active"] == 1


def test_chat_sends_image_directly_to_main_model(client, fast_aggregation, monkeypatch, tmp_path):
    from PIL import Image
    image = tmp_path / "photo.png"
    Image.new("RGB", (32, 32), "red").save(image)

    def unexpected_vision(**kwargs):
        raise AssertionError("Separate vision pre-call must not run")

    monkeypatch.setattr(
        multimodal_input_service,
        "generate_multimodal_chat_reply",
        unexpected_vision,
    )
    captured = []

    def direct_reply(messages, trace_id=None, session_id=None):
        captured.extend(messages[-1]["content"])
        return "一张红色图片"

    monkeypatch.setattr(chat_service, "generate_chat_reply", direct_reply)

    response = client.post(
        "/chat",
        json={
            "message": "看看这张图",
            "session_id": "s-image",
            "attachments": [
                {"kind": "image", "media_path": str(image), "mime_type": "image/png"}
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["reply"] == "一张红色图片"
    assert payload["message_type"] == "image"
    assert any(block["type"] == "image_url" for block in captured)


def test_chat_invalid_image_returns_400(client, fast_aggregation, monkeypatch, tmp_path):
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    def broken_multimodal(*, text_prompt, attachments, trace_id=None):
        raise RuntimeError("vision provider down")

    monkeypatch.setattr(
        multimodal_input_service, "generate_multimodal_chat_reply", broken_multimodal
    )

    response = client.post(
        "/chat",
        json={
            "message": "看看这张图",
            "session_id": "s-image-err",
            "attachments": [
                {"kind": "image", "media_path": str(image), "mime_type": "image/png"}
            ],
        },
    )

    assert response.status_code == 400


def test_chat_rejects_local_image_outside_trusted_spool(client, fast_aggregation, monkeypatch, tmp_path):
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = tmp_path / "outside.png"
    from PIL import Image
    Image.new("RGB", (4, 4), "red").save(outside)
    monkeypatch.setattr(config, "IMAGE_LOCAL_SPOOL_ROOTS", (trusted.resolve(),))
    called = []
    monkeypatch.setattr(chat_service, "generate_chat_reply", lambda *a, **kw: called.append(True) or "no")

    response = client.post(
        "/chat",
        json={
            "message": "看看这张图",
            "session_id": "s-image-path-err",
            "attachments": [{"kind": "image", "media_path": str(outside)}],
        },
    )

    assert response.status_code == 400
    assert not called


def test_attachment_schema_rejects_unknown_kind_length(client):
    response = client.post(
        "/chat",
        json={
            "message": "你好",
            "attachments": [{"kind": "x" * 100}],
        },
    )
    assert response.status_code == 422


def test_attachment_schema_rejects_more_than_eight_attachments(client):
    response = client.post(
        "/chat",
        json={"message": "too many", "attachments": [{"kind": "note"}] * 9},
    )
    assert response.status_code == 422


def test_session_endpoints_require_admin_token(client):
    assert client.get("/session/list").status_code == 403
    assert (
        client.get("/session/messages", params={"session_id": "s"}).status_code == 403
    )
