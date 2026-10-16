from __future__ import annotations

from pathlib import Path

import pytest

from app import config
import app.storage.db as db_storage
import app.storage.operations as operations_storage
from app.storage.db import init_db


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the SQLite storage at a throwaway directory for this test."""
    data_dir = tmp_path / "data"
    monkeypatch.setattr(db_storage, "DB_DIR", data_dir)
    monkeypatch.setattr(db_storage, "DB_PATH", data_dir / "bot.db")
    monkeypatch.setenv("NENO_OPERATIONS_DB", str(data_dir / "operations.db"))
    monkeypatch.setattr(operations_storage, "_instance", None)
    init_db()
    return data_dir


@pytest.fixture(autouse=True)
def trusted_test_media_spool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "IMAGE_LOCAL_SPOOL_ROOTS", (tmp_path.resolve(),))
