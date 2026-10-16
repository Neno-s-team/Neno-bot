from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.security as security


def test_admin_token_rejects_request_when_not_configured(monkeypatch):
    monkeypatch.setattr(security, "ADMIN_TOKEN", "")

    with pytest.raises(HTTPException) as exc:
        security.require_admin_token(x_admin_token=None)

    assert exc.value.status_code == 403
    assert exc.value.detail == "ADMIN_TOKEN not configured"


def test_admin_token_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(security, "ADMIN_TOKEN", "secret-token")

    with pytest.raises(HTTPException) as exc:
        security.require_admin_token(x_admin_token="wrong")

    assert exc.value.status_code == 403
    assert exc.value.detail == "invalid admin token"


def test_admin_token_accepts_matching_token(monkeypatch):
    monkeypatch.setattr(security, "ADMIN_TOKEN", "secret-token")

    security.require_admin_token(x_admin_token="secret-token")


def test_internal_token_is_fail_closed(monkeypatch):
    monkeypatch.setattr(security, "INTERNAL_TOKEN", "")
    with pytest.raises(HTTPException) as exc:
        security.require_internal_token(x_internal_token=None)
    assert exc.value.status_code == 403
    assert exc.value.detail == "NENO_INTERNAL_TOKEN not configured"


def test_internal_token_rejects_wrong_and_accepts_matching(monkeypatch):
    monkeypatch.setattr(security, "INTERNAL_TOKEN", "internal-secret")
    with pytest.raises(HTTPException) as exc:
        security.require_internal_token(x_internal_token="wrong")
    assert exc.value.status_code == 403
    security.require_internal_token(x_internal_token="internal-secret")
