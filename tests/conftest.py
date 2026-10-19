from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app import config
import app.storage.db as db_storage
import app.storage.operations as operations_storage
from app.storage.db import init_db

# 测试里用 naive datetime(...).timestamp() 构造时刻，课表按 MIND_TIMEZONE 解读；
# 固定进程时区与之一致，避免在 UTC 机器上跑出不同结果。
os.environ["TZ"] = config.MIND_TIMEZONE or "Asia/Shanghai"
if hasattr(time, "tzset"):
    time.tzset()


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
