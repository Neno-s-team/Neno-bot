"""世界 → 大脑：她此刻在哪、在干嘛（D3）。替代临时课表 schedule.json 作为 L0 的打断等级来源。

开关 MIND_WORLD_ID（空 = 不接，继续用课表）。世界库与聊天同库（data/bot.db），只读。
- 当前：读世界里 neno 的 doing / 位置 / 身体状况。
- 未来（L0 抽样要往后看几小时）：读世界里已排好的义务（作息表）与当前活动的结束时刻；
  空档里她会做什么还没定，按"在家/在外的空闲"给一个中间等级。
打断等级：活动 → 0..4（见 LEVELS）；身体不舒服、刚醒等再微调。
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from app import config
from app.services.mind import clock
from app.storage.db import fetch_one
from app.utils.logging_utils import log_event

# 活动 → (打断等级, 手机在哪, 静音)
LEVELS: dict[str, tuple[int, str, bool]] = {
    "sleep": (0, "枕边", True),
    "meeting": (1, "包里", True),
    "class": (1, "包里", True),
    "commute": (3, "手里", False),
    "study": (2, "桌上", False),
    "read": (3, "身边", False),
    "eat": (3, "桌上", False),
    "cook": (3, "灶台边", False),
    "call": (2, "耳边", False),
    "rest": (3, "手里", False),
    "scroll": (4, "手里", False),
    "stroll": (3, "兜里", False),
    "chat": (2, "身边", False),
}
_IDLE = (3, "身边", False)
_LABELS = {"sleep": "睡觉", "meeting": "组会", "class": "上课", "commute": "在路上", "study": "看文献写论文",
           "read": "看闲书", "eat": "吃东西", "cook": "做饭", "call": "和爸妈打电话", "rest": "歇会儿", "scroll": "刷手机",
           "stroll": "散步", "chat": "跟人聊天"}


def enabled() -> bool:
    return bool(getattr(config, "MIND_WORLD_ID", ""))


def _world() -> dict[str, Any] | None:
    try:
        row = fetch_one("SELECT * FROM world_worlds WHERE world_id = ?", (config.MIND_WORLD_ID,))
    except Exception:
        return None
    if row is None:
        return None
    value = dict(row)
    value["settings"] = json.loads(value["settings"])
    return value


def _actor() -> dict[str, Any] | None:
    try:
        row = fetch_one("SELECT * FROM world_entities WHERE world_id = ? AND entity_id = ?",
                        (config.MIND_WORLD_ID, config.MIND_WORLD_ACTOR))
    except Exception:
        return None
    if row is None:
        return None
    value = dict(row)
    value["state"] = json.loads(value["state"])
    return value


def _place_label(place_id: str) -> str:
    row = fetch_one("SELECT label FROM world_entities WHERE world_id = ? AND entity_id = ?",
                    (config.MIND_WORLD_ID, place_id))
    return row["label"] if row else place_id


def _segment(activity: str | None, start: float | None, end: float | None, place: str = "") -> dict[str, Any]:
    level, phone, muted = LEVELS.get(activity or "", _IDLE)
    return {"activity": _LABELS.get(activity or "", "空闲"), "level": level, "phone": phone, "muted": muted,
            "start": start, "end": end, "world": "live", "place": place}


def now_segment(ts: float | None = None) -> dict[str, Any] | None:
    """世界里她此刻的状态；世界没开/没找到返回 None（调用方回退课表）。"""
    if not enabled():
        return None
    actor = _actor()
    if actor is None:
        log_event("mind", "world_unavailable", world=config.MIND_WORLD_ID)
        return None
    doing = actor["state"].get("doing") or {}
    seg = _segment(doing.get("activity"), doing.get("since"), doing.get("until"), _place_label(actor["parent_id"]))
    conditions = [c["label"] for c in actor["state"].get("conditions", []) if c.get("until", 0) > (ts or clock.now())]
    if conditions:
        seg["body"] = "，".join(conditions)
    contacts = [x for x in actor["state"].get("recent_contacts", []) if (ts or clock.now()) - x["at"] < 12 * 3600]
    if contacts:
        seg["contacts"] = [x["note"] or f"{x['from']}{'打电话' if x['channel'] == 'call' else '发消息'}来过"
                           for x in contacts[-3:]]
    return seg


def iter_future(t0: float, t1: float) -> Iterator[tuple[float, float, dict[str, Any]]]:
    """[t0, t1) 内的时段流：当前活动到结束 → 已排义务 → 其余按空闲。"""
    current = now_segment(t0)
    if current is None:
        return
    cursor = t0
    if current.get("end") and current["end"] > t0:
        end = min(current["end"], t1)
        yield (t0, end, current)
        cursor = end
    for start, end, seg in _obligations(cursor, t1):
        if start > cursor:
            yield (cursor, start, _segment(None, cursor, start))
        if end > cursor:
            yield (max(start, cursor), end, seg)
            cursor = end
    if cursor < t1:
        yield (cursor, t1, _segment(None, cursor, t1))


def _obligations(t0: float, t1: float) -> list[tuple[float, float, dict[str, Any]]]:
    from app.services.world import rhythms

    w = _world()
    if w is None or not rhythms.config(w):
        return []
    entries = rhythms.config(w).get("actors", {}).get(config.MIND_WORLD_ACTOR, {}).get("routine", [])
    spans = []
    day, _ = rhythms._local_day(w, t0)
    for d in range(day - 1, day + 3):
        for entry in entries:
            occ = rhythms._occurrence(w, config.MIND_WORLD_ACTOR, entry, d)
            if occ is None:
                continue
            begin, finish = occ
            if finish is None:
                hours = rhythms.config(w)["activities"][entry["activity"]]["hours"]
                finish = begin + sum(hours) / 2 * 3600
            if finish > t0 and begin < t1:
                spans.append((max(begin, t0), min(finish, t1), _segment(entry["activity"], begin, finish)))
    spans.sort(key=lambda x: x[0])
    merged = []
    for span in spans:
        if merged and span[0] < merged[-1][1]:
            continue
        merged.append(span)
    return merged


def day_events(start: float, end: float, limit: int = 60) -> list[str]:
    """[start, end) 内她在世界里过的日子，压成几十行"几点 做了什么 / 谁找过她 / 出了什么事"，给夜间反思回想。"""
    if not enabled():
        return []
    from datetime import datetime
    try:
        rows = [dict(r) for r in _fetch_events(start, end)]
    except Exception:
        return []
    tz = clock.local_now().tzinfo
    lines, last = [], None
    for row in rows:
        p = json.loads(row["payload"])
        at = datetime.fromtimestamp(row["sim_time"], tz).strftime("%H:%M")
        kind = row["kind"]
        text = None
        if kind == "activity.started" and p.get("activity") not in (None, "commute"):
            text = _LABELS.get(p["activity"], p["activity"])
        elif kind == "npc.contacted":
            text = p.get("note") or "有人找过她"
        elif kind == "perturb.happened" and (row["actor_id"] == config.MIND_WORLD_ACTOR or p.get("target") == "street"):
            text = p.get("label")
        elif kind == "need.consequence":
            text = p.get("label")
        elif kind == "routine.skipped":
            text = f"没去{_LABELS.get(p.get('activity'), p.get('activity'))}"
        if text and text != last:
            lines.append(f"{at} {text}")
            last = text
    return lines[-limit:]


def _fetch_events(start: float, end: float):
    from app.storage.db import fetch_all
    return fetch_all(
        """SELECT kind, actor_id, payload, sim_time FROM world_events
           WHERE world_id = ? AND sim_time >= ? AND sim_time < ?
             AND (actor_id = ? OR (kind = 'perturb.happened' AND actor_id IS NULL))
           ORDER BY id""",
        (config.MIND_WORLD_ID, start, end, config.MIND_WORLD_ACTOR))
