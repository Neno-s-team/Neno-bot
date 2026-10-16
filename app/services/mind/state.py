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


def _clamp_unit(value: float) -> float:
    return max(-1.0, min(1.0, value))


def _compute_next(
    raw: dict[str, Any] | None, valence_delta: float, arousal_delta: float
) -> tuple[float, float]:
    """共享计算：衰减后的值 + delta + clamp。读写在库外与事务内都走这条。"""
    if raw is None:
        return _clamp_unit(valence_delta), _clamp_unit(arousal_delta)
    decayed_valence, decayed_arousal = _decay_raw(raw)
    return (
        _clamp_unit(decayed_valence + valence_delta),
        _clamp_unit(decayed_arousal + arousal_delta),
    )


def decayed_from_raw(raw: dict[str, Any] | None) -> tuple[float, float]:
    """给定原始行，返回衰减后的 (valence, arousal)；供事务内预读复用。"""
    return _compute_next(raw, 0.0, 0.0)


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
        new_valence, new_arousal = _compute_next(raw, valence_delta, arousal_delta)
        if raw is None:
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


def apply_affect_delta_in_conn(
    conn,
    session_id: str,
    valence_delta: float,
    arousal_delta: float,
) -> dict[str, Any] | None:
    """事务内写入：同库同一个 conn 上做带版本号的 UPDATE（首行 INSERT）。

    供 commit_memory 在同一事务里提交状态用；版本冲突返回 None，由调用方回滚整个事务。
    对外唯一入口仍是 apply_affect_delta。
    """
    row = conn.execute(
        "SELECT session_id, valence, arousal, updated_at, version"
        " FROM mind_state WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    raw = dict(row) if row is not None else None
    new_valence, new_arousal = _compute_next(raw, valence_delta, arousal_delta)
    now = time.time()
    if raw is None:
        cursor = conn.execute(
            """INSERT INTO mind_state(session_id, valence, arousal, updated_at, version)
               VALUES (?, ?, ?, ?, 1)
               ON CONFLICT(session_id) DO NOTHING""",
            (session_id, new_valence, new_arousal, now),
        )
        if cursor.rowcount == 0:
            return None  # 首写撞已存在行 → 调用方回滚重试
        return {
            "session_id": session_id,
            "valence": new_valence,
            "arousal": new_arousal,
            "updated_at": now,
            "version": 1,
        }
    cursor = conn.execute(
        """UPDATE mind_state
           SET valence=?, arousal=?, updated_at=?, version=version+1
           WHERE session_id=? AND version=?""",
        (new_valence, new_arousal, now, session_id, raw["version"]),
    )
    if cursor.rowcount == 0:
        return None
    return {
        "session_id": session_id,
        "valence": new_valence,
        "arousal": new_arousal,
        "updated_at": now,
        "version": raw["version"] + 1,
    }

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
