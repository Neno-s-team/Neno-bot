from __future__ import annotations

import socket
import ssl
import time
from pathlib import Path

import pytest
from PIL import Image

from app import config
from app.schemas import MediaAttachment
from app.services.chat import image_memory
from app.services.session_aggregation_controller import SessionAggregationController
from app.session_identity import SessionIdentityError, canonical_chat_session_id, telegram_chat_id


def test_session_identity_is_canonical_and_signed_64_bit():
    assert canonical_chat_session_id(" ordinary ") == "ordinary"
    assert telegram_chat_id("tg:-100123") == -100123
    for value in ("tg:", "tg:+1", "tg:01", "tg: 1", "tg:9223372036854775808"):
        with pytest.raises(SessionIdentityError):
            canonical_chat_session_id(value)


def test_cors_configuration_rejects_wildcard(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://client.invalid, *")
    with pytest.raises(RuntimeError, match="explicit origins"):
        config._cors_origins()


def test_local_image_must_be_inside_trusted_spool(tmp_path, monkeypatch):
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = tmp_path / "outside.png"
    inside = trusted / "inside.png"
    Image.new("RGB", (4, 4), "red").save(outside)
    Image.new("RGB", (4, 4), "blue").save(inside)
    monkeypatch.setattr(config, "IMAGE_LOCAL_SPOOL_ROOTS", (trusted.resolve(),))
    with pytest.raises(ValueError, match="trusted media spool"):
        image_memory._read_source(MediaAttachment(kind="image", media_path=str(outside)))
    assert image_memory._read_source(MediaAttachment(kind="image", media_path=str(inside)))


def test_https_fetch_connects_to_the_validated_address(monkeypatch):
    connected = []
    wrapped = []

    class RawSocket:
        def settimeout(self, value):
            pass
        def connect(self, address):
            connected.append(address)
        def close(self):
            pass

    class TlsSocket:
        def settimeout(self, value):
            pass

    class Context:
        def wrap_socket(self, raw, server_hostname):
            wrapped.append(server_hostname)
            return TlsSocket()

    class Response:
        status = 200
        chunks = [b"synthetic-image", b""]
        def getheader(self, name):
            return None
        def getheaders(self):
            return []
        def read(self, size):
            return self.chunks.pop(0)

    class Connection:
        def __init__(self, host, port, timeout):
            self.sock = None
        def request(self, method, path, headers):
            assert headers["Host"] == "audit-image.invalid"
        def getresponse(self):
            return Response()
        def close(self):
            pass

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
    ])
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: RawSocket())
    monkeypatch.setattr(ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr(image_memory.http.client, "HTTPConnection", Connection)
    assert image_memory._download_image("https://audit-image.invalid/p.png") == b"synthetic-image"
    assert connected == [("93.184.216.34", 443)]
    assert wrapped == ["audit-image.invalid"]


def test_completed_aggregation_sessions_are_evicted():
    controller = SessionAggregationController(window_seconds=0.01)
    for index in range(24):
        ticket = controller.allocate_ticket(session_id=f"rejected-{index}")
        controller.complete_exception(ticket=ticket, exc=ValueError("synthetic"))
    deadline = time.monotonic() + 2
    while controller._sessions and time.monotonic() < deadline:
        time.sleep(0.01)
    assert controller._sessions == {}
