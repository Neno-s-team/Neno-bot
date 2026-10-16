"""mind 模块的统一时钟。测试可以 monkeypatch now()/local_now() 成假时钟。"""

from __future__ import annotations

import time
from datetime import datetime

from app import config

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


def now() -> float:
    return time.time()


def local_now() -> datetime:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo(config.MIND_TIMEZONE))
    return datetime.now()
