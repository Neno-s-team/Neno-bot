from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from app import config
from app.services.mind import store
from app.utils.logging_utils import log_event

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


def _decay(value: float, elapsed_minutes: float, half_life_minutes: float) -> float:
    """指数衰减：V = V0 * 2^(-t / half_life)。"""
    if half_life_minutes <= 0:
        return value
    return value * (2 ** (-elapsed_minutes / half_life_minutes))


def _decay_raw(raw: dict[str, Any]) -> tuple[float, float]:
    """按 raw.updated_at 衰减，供读和写共用同一条逻辑。"""
    elapsed_minutes = (time.time() - raw["updated_at"]) / 60.0
    half_life = config.MIND_AFFECT_HALF_LIFE_MINUTES
    return (
        _decay(raw["valence"], elapsed_minutes, half_life),
        _decay(raw["arousal"], elapsed_minutes, half_life),
    )


def read_state(session_id: str) -> dict[str, Any]:
    """读取当前情绪状态，按 updated_at 衰减。"""
    raw = store.get_mind_state(session_id)
    if raw is None:
        return {
            "session_id": session_id,
            "valence": 0.0,
            "arousal": 0.0,
            "updated_at": time.time(),
            "version": 0,
        }
    valence, arousal = _decay_raw(raw)
    return {
        "session_id": raw["session_id"],
        "valence": valence,
        "arousal": arousal,
        "updated_at": raw["updated_at"],
        "version": raw["version"],
    }


def apply_affect_delta(
    session_id: str,
    valence_delta: float,
    arousal_delta: float,
    *,
    max_retries: int = 2,
) -> dict[str, Any] | None:
    """全仓库唯一的情绪状态写入入口：衰减 + delta + clamp + 乐观锁。"""
    for _ in range(max_retries + 1):
        raw = store.get_mind_state(session_id)
        if raw is None:
            new_valence = max(-1.0, min(1.0, valence_delta))
            new_arousal = max(-1.0, min(1.0, arousal_delta))
            inserted = store.insert_mind_state(session_id, new_valence, new_arousal)
            if inserted > 0:
                return {
                    "session_id": session_id,
                    "valence": new_valence,
                    "arousal": new_arousal,
                    "updated_at": time.time(),
                    "version": 1,
                }
            continue  # 首写撞已存在行，下一轮走 UPDATE 分支

        decayed_valence, decayed_arousal = _decay_raw(raw)
        new_valence = max(-1.0, min(1.0, decayed_valence + valence_delta))
        new_arousal = max(-1.0, min(1.0, decayed_arousal + arousal_delta))
        changed = store.update_mind_state_versioned(
            session_id, new_valence, new_arousal, raw["version"]
        )
        if changed > 0:
            return {
                "session_id": session_id,
                "valence": new_valence,
                "arousal": new_arousal,
                "updated_at": time.time(),
                "version": raw["version"] + 1,
            }

    log_event(
        "mind",
        "mind_state_write_conflict",
        session_id=session_id,
        retries=max_retries,
    )
    return None

def compute_inhibition(arousal: float = 0.0, base: float | None = None) -> float:
    """抑制力 = clamp(base − night_factor − max(arousal, 0) * 0.2)。"""
    resolved_base = 0.7 if base is None else base
    if ZoneInfo is not None:
        tz = ZoneInfo(config.MIND_TIMEZONE)
    else:
        tz = timezone.utc
    hour = datetime.now(tz).hour
    night_factor = 0.15 if hour >= 23 or hour < 5 else 0.0
    arousal_factor = max(arousal, 0.0) * 0.2
    return max(0.0, min(1.0, resolved_base - night_factor - arousal_factor))


def compute_leak(arousal: float = 0.0, base_inhibition: float | None = None) -> float:
    """leak 上限 = 1 − inhibition。"""
    return 1.0 - compute_inhibition(arousal, base_inhibition)
