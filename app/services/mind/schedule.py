"""简易课表：L0 的打断等级来源（P3a 用课表替代世界）。

文件缺失或解析失败一律当等级 3（空闲），记 schedule_unavailable，
情境标注 world=unavailable。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Iterator

from app import config
from app.services.mind import clock, store
from app.utils.logging_utils import log_event

_DEFAULT_LEVEL = 3


def _minutes(text: str) -> int:
    hour, minute = str(text).split(":")
    return int(hour) * 60 + int(minute)


def _load_raw() -> dict[str, Any] | None:
    path = config.MIND_SCHEDULE_PATH
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("schedule root must be an object")
        return data
    except Exception:
        log_event("mind", "schedule_unavailable", path=path)
        return None


def _day_segments(day: datetime) -> list[dict[str, Any]] | None:
    """某本地日的时段列表（分钟粒度，睡眠优先，weekly 次之，default 填空）。"""
    raw = _load_raw()
    if raw is None:
        return None
    segments: list[dict[str, Any]] = []
    sleep = raw.get("sleep") or {}
    if sleep.get("start") and sleep.get("end"):
        segments.append({
            "start": _minutes(sleep["start"]),
            "end": _minutes(sleep["end"]),
            "activity": "睡觉",
            "level": 0,
            "phone": "枕边",
            "muted": True,
        })
    for entry in raw.get("weekly") or []:
        days = [int(item) for item in (entry.get("days") or [])]
        if day.isoweekday() not in days:
            continue
        start, end = _minutes(entry["start"]), _minutes(entry["end"])
        # 与已占用（睡眠）时段重叠则跳过
        if any(seg["start"] < end and start < seg["end"] for seg in segments):
            continue
        segments.append({
            "start": start,
            "end": end,
            "activity": entry.get("activity", "安排"),
            "level": int(entry.get("level", 1)),
            "phone": entry.get("phone", "身上"),
            "muted": bool(entry.get("muted", False)),
        })
    segments.sort(key=lambda item: item["start"])
    default = raw.get("default") or {}
    fill_default = {
        "activity": default.get("activity", "空闲"),
        "level": int(default.get("level", _DEFAULT_LEVEL)),
        "phone": default.get("phone", "身上"),
        "muted": bool(default.get("muted", False)),
    }
    cursor = 0
    filled: list[dict[str, Any]] = []
    for seg in segments:
        if seg["start"] > cursor:
            filled.append({"start": cursor, "end": seg["start"], **fill_default})
        filled.append(seg)
        cursor = max(cursor, seg["end"])
    if cursor < 24 * 60:
        filled.append({
            "start": cursor,
            "end": 24 * 60,
            "activity": default.get("activity", "空闲"),
            "level": int(default.get("level", _DEFAULT_LEVEL)),
            "phone": default.get("phone", "身上"),
            "muted": bool(default.get("muted", False)),
        })
    return filled


def _segment_on_day(day: datetime, minute: int) -> dict[str, Any] | None:
    segments = _day_segments(day)
    if segments is None:
        return None
    for seg in segments:
        if seg["start"] <= minute < seg["end"]:
            return seg
    return None


def _unavailable() -> dict[str, Any]:
    return {
        "activity": "空闲",
        "level": _DEFAULT_LEVEL,
        "phone": "身上",
        "muted": False,
        "start": None,
        "end": None,
        "world": "unavailable",
    }


def segment_at(t: float | None = None, session_id: str | None = None) -> dict[str, Any]:
    """t（epoch）时的时段；缺失/解析失败 → 等级 3 + world=unavailable。

    传 session_id 且课表等级 >=3 时：若 t < last_self_activity + 专注窗口，
    升到等级 4（她在这个聊天里刚活动过）。不传 session_id 不做升级。
    """
    ts = clock.now() if t is None else t
    local = datetime.fromtimestamp(ts, tz=clock.local_now().tzinfo)
    seg = _segment_on_day(local, local.hour * 60 + local.minute)
    if seg is None:
        return _unavailable()
    result = dict(seg)
    result["world"] = "schedule"
    if session_id is not None and result["level"] >= 3:
        window = max(0, int(config.MIND_FOCUS_WINDOW_MIN)) * 60
        last = store.last_self_activity(session_id, ts)
        if window and last is not None and ts < last + window:
            result["level"] = 4
    return result


def iter_segments(t0: float, t1: float) -> Iterator[tuple[float, float, dict[str, Any]]]:
    """[t0, t1) 内的绝对时段流，供 L0 抽样沿时间推进。"""
    tz = clock.local_now().tzinfo
    day = datetime.fromtimestamp(t0, tz=tz).replace(hour=0, minute=0, second=0, microsecond=0)
    while day.timestamp() < t1:
        segments = _day_segments(day)
        if segments is None:
            yield (t0, t1, _unavailable())
            return
        base = day.timestamp()
        for seg in segments:
            start = base + seg["start"] * 60
            end = base + seg["end"] * 60
            if end > t0 and start < t1:
                yield (max(start, t0), min(end, t1), dict(seg))
        day += timedelta(days=1)


def next_change(t: float | None = None) -> float | None:
    """t 之后当前时段的结束时刻；未知则 None。"""
    ts = clock.now() if t is None else t
    current = segment_at(ts)
    if current.get("end") is None:
        return None
    tz = clock.local_now().tzinfo
    local = datetime.fromtimestamp(ts, tz=tz)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start.timestamp() + current["end"] * 60


def next_open_start(t: float, min_level: int = 2) -> float | None:
    """t 之后第一个等级 >= min_level 的时段开始时刻。"""
    end = t + 24 * 3600
    for start, _seg_end, seg in iter_segments(t, end):
        if seg["level"] >= min_level:
            return start
    return None
