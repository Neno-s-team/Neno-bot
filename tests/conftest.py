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

# 测试进程绝不启动线上世界。.env 里 WORLD_ENABLED=true 时，集成测试起整个 app 会在真实的 data/bot.db 上
# 再跑一个世界引擎：推进线上世界、取天气、调 NPC 模型、做现实同步（app.main 启动时拿的是真实库路径，
# isolated_db 罩不住）。需要世界的测试自己在临时库上建引擎。
for _name in ("WORLD_ENABLED", "WORLD_NPC_ENABLED", "WORLD_REAL_WEATHER", "WORLD_REAL_FEED", "WORLD_COGNITION_AUTOSTART"):
    os.environ[_name] = "false"


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


@pytest.fixture(autouse=True)
def no_local_style_corpus(monkeypatch: pytest.MonkeyPatch):
    """本地真人语料（data/corpus）不进测试；需要的测试自己指定临时语料。"""
    monkeypatch.setattr(config, "MIND_STYLE_EXAMPLES", 0)
