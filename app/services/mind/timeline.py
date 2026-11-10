"""时间线：给模型看的时间一律带"离现在多久"，并且用具体日期，不让模型自己做日期换算。

- ago(ts)：刚才 / 5 分钟前 / 2 小时前 / 昨天晚上 / 前天下午 / 3 天前 / 上周三 / 10月20日
- stamp(ts)：11月10日 周二 21:30（今天/昨天/前天会替换成相对词）
- gap(seconds)：12 分钟 / 3 小时 / 2 天
- turns_with_time(history)：最近原话之间隔得久就插一行"——隔了 2 天（11月10日 周二 21:30）——"
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app import config
from app.services.mind import clock

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

_WEEK = "一二三四五六日"
_GAP_MARK_SEC = 30 * 60


def _tz():
    return ZoneInfo(config.MIND_TIMEZONE) if ZoneInfo is not None else timezone.utc


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts), _tz())


def _period(hour: int) -> str:
    if hour < 5:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 23:
        return "晚上"
    return "深夜"


def _day_diff(ts: float, now: float) -> int:
    return (_local(now).date() - _local(ts).date()).days


def gap(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return "不到两分钟"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟"
    if seconds < 86400:
        hours = seconds / 3600
        return f"{round(hours)} 小时" if hours >= 3 else f"{round(hours * 2) / 2:g} 小时"
    return f"{int(seconds // 86400)} 天"


def ago(ts: float, now: float | None = None) -> str:
    now = clock.now() if now is None else now
    delta = now - float(ts)
    if delta < 0:
        return day_word(ts, now)
    if delta < 120:
        return "刚才"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    days = _day_diff(ts, now)
    local = _local(ts)
    if days == 0:
        return f"{int(delta // 3600)} 小时前" if delta < 6 * 3600 else f"今天{_period(local.hour)}"
    if days == 1:
        return f"昨天{_period(local.hour)}"
    if days == 2:
        return f"前天{_period(local.hour)}"
    if days < 7:
        return f"{days} 天前"
    if days < 14:
        return f"上周{_WEEK[local.weekday()]}"
    return f"{local.month}月{local.day}日"


def day_word(ts: float, now: float | None = None) -> str:
    """某个时刻是哪天：今天/明天/后天/昨天/前天/周X/具体日期（用于打算、期限）。"""
    now = clock.now() if now is None else now
    days = _day_diff(ts, now)
    local = _local(ts)
    words = {0: "今天", 1: "昨天", 2: "前天", -1: "明天", -2: "后天"}
    if days in words:
        return words[days]
    if -7 < days < 0:
        return f"这周{_WEEK[local.weekday()]}" if local.weekday() > _local(now).weekday() else f"下周{_WEEK[local.weekday()]}"
    return f"{local.month}月{local.day}日（周{_WEEK[local.weekday()]}）"


def stamp(ts: float, now: float | None = None) -> str:
    now = clock.now() if now is None else now
    local = _local(ts)
    days = _day_diff(ts, now)
    head = {0: "今天", 1: "昨天", 2: "前天"}.get(days, f"{local.month}月{local.day}日 周{_WEEK[local.weekday()]}")
    return f"{head} {local.strftime('%H:%M')}"


def today_line(now: float | None = None) -> str:
    now = clock.now() if now is None else now
    local = _local(now)
    return f"{local.year}年{local.month}月{local.day}日 周{_WEEK[local.weekday()]} {_period(local.hour)}{local.strftime('%H:%M')}"


def message_ts(item: dict[str, Any]) -> float | None:
    """messages.created_at 是 UTC 'YYYY-MM-DD HH:MM:SS'。"""
    raw = item.get("created_at")
    if not raw:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "")).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def turns_with_time(turns: list[dict[str, Any]], now: float | None = None, *, me: str = "你", other: str = "对方",
                    limit_chars: int = 300) -> list[str]:
    """最近原话：第一条前标时间；相邻两条隔得久（≥30 分钟）就插一行"——隔了多久（几号几点）——"；
    最后一条到现在也隔得久，再标一行"——到现在过了多久——"。"""
    now = clock.now() if now is None else now
    lines: list[str] = []
    last = None
    for item in turns:
        ts = message_ts(item)
        if ts is not None:
            if last is None:
                lines.append(f"——{stamp(ts, now)}——")
            elif ts - last >= _GAP_MARK_SEC:
                lines.append(f"——隔了 {gap(ts - last)}（{stamp(ts, now)}）——")
            last = ts
        who = other if item.get("role") == "user" else me
        lines.append(f"{who}：{str(item.get('content') or '')[:limit_chars]}")
    if last is not None and now - last >= _GAP_MARK_SEC:
        lines.append(f"——到现在过了 {gap(now - last)}——")
    return lines
