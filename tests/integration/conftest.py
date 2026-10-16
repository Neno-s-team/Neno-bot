from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.security as security
from app.services.session_aggregation_controller import SessionAggregationController

TEST_ADMIN_TOKEN = "integration-test-admin-token"
TEST_INTERNAL_TOKEN = "integration-test-internal-token"


@pytest.fixture()
def admin_headers(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(security, "ADMIN_TOKEN", TEST_ADMIN_TOKEN)
    return {"X-Admin-Token": TEST_ADMIN_TOKEN}


@pytest.fixture()
def fast_aggregation(monkeypatch: pytest.MonkeyPatch) -> SessionAggregationController:
    """Replace the router's aggregation controller with a short-window one so
    integration tests do not wait on the production burst window."""
    import app.routers.chat as chat_router

    controller = SessionAggregationController(window_seconds=0.05)
    monkeypatch.setattr(chat_router, "session_aggregation_controller", controller)
    return controller


@pytest.fixture()
def client(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    del isolated_db
    monkeypatch.setattr(security, "INTERNAL_TOKEN", TEST_INTERNAL_TOKEN)
    with TestClient(main_module.app, headers={"X-Internal-Token": TEST_INTERNAL_TOKEN}) as test_client:
        yield test_client
