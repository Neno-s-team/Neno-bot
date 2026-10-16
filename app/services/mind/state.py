from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from app.config import MIND_AFFECT_HALF_LIFE_MINUTES, MIND_TIMEZONE
from app.services.mind.store import get_mind_state, upsert_mind_state

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


def _decay(value: float, elapsed_minutes: float, half_life_minutes: float) -> float:
    """指数衰减：V = V0 * 2^(-t / half_life)"""
    if half_life_minutes <= 0:
        return value
    return value * (2 ** (-elapsed_minutes / half_life_minutes))


def read_state(session_id: str) -> dict[str, Any]:
    """读取当前情绪状态，应用衰减。"""
    raw = get_mind_state(session_id)
    if raw is None:
        return {
            "session_id": session_id,
            "valence": 0.0,
            "arousal": 0.0,
            "updated_at": time.time(),
            "version": 0,
        }
    elapsed_minutes = (time.time() - raw["updated_at"]) / 60.0
    return {
        "session_id": raw["session_id"],
        "valence": _decay(raw["valence"], elapsed_minutes, MIND_AFFECT_HALF_LIFE_MINUTES),
        "arousal": _decay(raw["arousal"], elapsed_minutes, MIND_AFFECT_HALF_LIFE_MINUTES),
        "updated_at": raw["updated_at"],
        "version": raw["version"],
    }


def apply_affect_delta(session_id: str, valence_delta: float, arousal_delta: float) -> dict[str, Any]:
    """应用情绪变化并保存。"""
    s = read_state(session_id)
    new_valence = max(-1.0, min(1.0, s["valence"] + valence_delta))
    new_arousal = max(-1.0, min(1.0, s["arousal"] + arousal_delta))
    upsert_mind_state(session_id, new_valence, new_arousal, version=s["version"] + 1)
    return {
        "session_id": session_id,
        "valence": new_valence,
        "arousal": new_arousal,
        "updated_at": time.time(),
        "version": s["version"] + 1,
    }


def compute_inhibition(arousal: float = 0.0, base: float | None = None) -> float:
    """抑制力 = clamp(base - night_factor - arousal_factor)"""
    base = base if base is not None else 0.7
    if ZoneInfo is not None:
        tz = ZoneInfo(MIND_TIMEZONE)
    else:
        tz = timezone.utc
    now = datetime.now(tz)
    hour = now.hour
    night_factor = 0.15 if 23 <= hour or hour < 5 else 0.0
    arousal_factor = arousal * 0.2
    return max(0.0, min(1.0, base - night_factor - arousal_factor))


def compute_leak(arousal: float = 0.0, base_inhibition: float | None = None) -> float:
    """leak 上限 = 1 - inhibition"""
    inhibition = compute_inhibition(arousal, base_inhibition)
    return 1.0 - inhibition
