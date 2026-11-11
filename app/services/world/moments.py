"""小事（S1）：挂在她正在做的事上的小插曲——排队、没座、路边的猫、老板多给了个煎蛋。不调模型，可重放。

和扰动、世界事件的区别：扰动是隔几小时全局抽一次的意外；世界事件是世界自己在发生的事；
小事是"此时此地"才会有的——在路上（go）、到了一个地方（arrive）、做完一件事（finish）时就地抽，所以总和情境对得上。

- 内容是一张表：content/world/moments/<pool>/*.json，一行一件，加小事只加行、不写代码。
  行 = 触发点 on + 条件 if + 权重 + 一句话 text（可带几种说法 variants）+ 后果 effects + 值不值得说 share。
- 条件词汇（_match）：activity / place_kind / area / hour / weather / feels / mode / tags / months / need / min_hours /
  choice / tight / pressed / first_visit / sick。
- 后果词汇（_apply）：need（更累/更饿/更想找人说话）、money（花掉/捡到）、minutes（路上耽误）、
  venue_like（对这家店的印象）、pref（喜好微调）、mood（只是记一笔，给她的脑子）。
- 三类：notice（看到的，没后果）、hiccup（小插曲）、luck（小运气）。
- 大多数时候什么都不发生（每个触发点一个小概率）；同一件事见多了就不稀奇（近期见过的权重降、有冷却）。
- 每件带 share：越高越是"会想跟人说"的事——她的脑子能看到最近几件，以后主动找人也从这里取话题。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.services.world import almanac, city, money, rhythms, tasks
from app.storage.world_store import emit, entity, save_entity

CONTENT = Path(__file__).resolve().parents[3] / "content" / "world" / "moments"
KEEP_RECENT = 8
_TEMPLATE_KINDS = {"library": "library", "canteen": "canteen", "cafe": "cafe", "convenience": "convenience",
                   "riverside": "river", "seminar_room": "classroom", "advisor_office": "office", "campus": "campus",
                   "my_room": "bedroom", "home_living": "living", "home_kitchen": "kitchen", "home_bath": "bath",
                   "street": "street", "metro": "metro"}


def config(w) -> dict | None:
    return (rhythms.config(w) or {}).get("moments")


def enabled(w, aid: str | None = None) -> bool:
    cfg = config(w)
    return bool(cfg) and (aid is None or aid == cfg.get("actor", "neno"))


@lru_cache(maxsize=4)
def load(pool: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted((CONTENT / pool).glob("*.json")):
        rows.extend(json.loads(path.read_text(encoding="utf-8"))["rows"])
    return rows


def state(w) -> dict:
    return w["settings"].setdefault("moments", {"seen": {}})


def _draw(w, key: str, purpose: str, low: float = 0.0, high: float = 1.0) -> float:
    return rhythms._draw(w, "moment:" + key, purpose, low, high)


# ── 情境 ─────────────────────────────────────────────────────────────────────

def place_kind(w, place: str | None) -> str:
    if not place:
        return ""
    if city.enabled(w) and place in city.data(w)["by_id"]:
        return city.data(w)["by_id"][place]["kind"]
    return _TEMPLATE_KINDS.get(place, "")


def context(c, w, aid: str, extra: dict) -> dict:
    """此时此地：在哪、几点、什么天、什么日子、身体怎么样、手头和手上的事。extra 是触发点自己带的（活动、怎么去的…）。"""
    actor = entity(c, w["world_id"], aid)
    place = extra.get("place") or actor["parent_id"]
    sky = {}
    if c.execute("SELECT 1 FROM world_entities WHERE world_id=? AND entity_id='street'", (w["world_id"],)).fetchone():
        sky = entity(c, w["world_id"], "street")["state"]
    day = almanac.day(w, w["sim_time"])
    wallet = money.feel(c, w, aid)
    seen = w["settings"].get("life_venues", {}).get(aid, {}).get(place)
    return {"place": place, "place_kind": place_kind(w, place), "area": city.area(w, place) if city.enabled(w) else "",
            "place_name": city.label(c, w, place),
            "hour": ((w["sim_time"] + w["settings"].get("utc_offset_minutes", 0) * 60) % 86400) / 3600,
            "weather": sky.get("weather"), "feels": sky.get("feels", sky.get("temp")), "tags": day["tags"],
            "month": int(day["date"][5:7]),
            "needs": {n: rhythms._value_at(r, w["sim_time"]) for n, r in rhythms._rows(c, w["world_id"], aid).items()},
            "tight": bool(wallet and wallet["level"] >= 2), "pressed": tasks.drive(w, aid)["top"] if tasks.enabled(w, aid) else 0.0,
            "first_visit": bool(seen and seen.get("visits") == 1), "sick": bool(actor["state"].get("conditions")),
            **extra}


def _match(cond: dict, ctx: dict) -> bool:
    for name, want in cond.items():
        if name in ("activity", "place_kind", "area", "weather", "mode", "choice"):
            if ctx.get(name) not in want:
                return False
        elif name == "weather_not":
            if ctx.get("weather") in want:
                return False
        elif name == "hour":
            low, high = want
            hour = ctx["hour"]
            if not (low <= hour < high if low <= high else hour >= low or hour < high):
                return False
        elif name == "months":
            if ctx["month"] not in want:
                return False
        elif name == "tags":
            if not ctx["tags"] & set(want):
                return False
        elif name == "tags_not":
            if ctx["tags"] & set(want):
                return False
        elif name in ("feels_below", "feels_above"):
            feels = ctx.get("feels")
            if feels is None or (feels >= want if name == "feels_below" else feels <= want):
                return False
        elif name in ("need_above", "need_below"):
            for need, value in want.items():
                have = ctx["needs"].get(need, 0.0)
                if (have < value) if name == "need_above" else (have > value):
                    return False
        elif name == "min_hours":
            if ctx.get("hours", 0.0) < want:
                return False
        elif name == "min_minutes":
            if ctx.get("minutes", 0.0) < want:
                return False
        elif name == "pressed_above":
            if ctx["pressed"] < want:
                return False
        elif name in ("tight", "first_visit", "sick"):
            if bool(ctx.get(name)) != bool(want):
                return False
        else:
            raise ValueError(f"unknown moment condition: {name}")
    return True


# ── 抽一件 ───────────────────────────────────────────────────────────────────

def _novelty(w, row: dict) -> float:
    """见多了就不稀奇：冷却期内不再发生；30 天内每见过一次，权重减半。"""
    seen = state(w)["seen"].get(row["id"], [])
    if seen and w["sim_time"] - seen[-1] < row.get("cooldown_hours", 20) * 3600:
        return 0.0
    recent = sum(1 for t in seen if w["sim_time"] - t < 30 * 86400)
    return 0.5 ** recent


def trigger(c, w, aid: str, hook: str, extra: dict, key: str) -> dict | None:
    """一个触发点：多半什么都不发生；发生就从对得上情境的行里按权重抽一件，落后果、记一笔。返回这件小事（或 None）。"""
    if not enabled(w, aid):
        return None
    cfg = config(w)
    if _draw(w, key, "any:" + hook) >= cfg["chance"].get(hook, 0.0):
        return None
    ctx = context(c, w, aid, extra)
    options = []
    for row in load(cfg["pool"]):
        if row["on"] != hook or not _match(row.get("if", {}), ctx):
            continue
        weight = row.get("weight", 1.0) * _novelty(w, row)
        if weight > 0:
            options.append((row, weight))
    if not options:
        return None
    cursor = _draw(w, key, "pick:" + hook) * sum(weight for _, weight in options)
    row = options[-1][0]
    for candidate, weight in options:
        cursor -= weight
        if cursor < 0:
            row = candidate
            break
    texts = row.get("variants") or [row["text"]]
    text = texts[int(_draw(w, key, "say") * len(texts))].format(place=ctx["place_name"])
    applied = _apply(c, w, aid, row.get("effects", {}), ctx, key)
    moment = {"id": row["id"], "kind": row.get("kind", "notice"), "text": text, "share": row.get("share", 0.3),
              "mood": row.get("effects", {}).get("mood", 0.0), "hook": hook, **applied}
    seen = state(w)["seen"].setdefault(row["id"], [])
    seen.append(w["sim_time"])
    del seen[:-6]
    actor = entity(c, w["world_id"], aid)
    recent = actor["state"].setdefault("recent_moments", [])
    recent.append({"at": w["sim_time"], "text": text, "share": moment["share"], "mood": moment["mood"]})
    del recent[:-KEEP_RECENT]
    save_entity(c, actor)
    emit(c, w, f"{key}:moment:{hook}", "life.moment", moment, aid)
    return moment


def _apply(c, w, aid: str, effects: dict, ctx: dict, key: str) -> dict:
    applied: dict = {}
    for need, spec in effects.get("need", {}).items():
        rows = rhythms.settle(c, w, aid)
        if need in rows:
            amount = _amount(w, key, "need:" + need, spec)
            rows[need]["value"] = max(0.0, min(1.0, rows[need]["value"] + amount))
            rhythms._reschedule(c, w, rows[need])
    if "money" in effects and money.enabled(w, aid):
        amount = round(_amount(w, key, "money", effects["money"]))
        if amount < 0:
            applied["money"] = -money.spend(c, w, aid, -amount, effects.get("what", "小东西"), "别的", key + ":moment",
                                            force=True, exact=True)
        elif amount > 0:
            money.earn(c, w, aid, amount, effects.get("what", "小运气"), key + ":moment")
            applied["money"] = amount
    if "minutes" in effects:
        applied["minutes"] = round(_amount(w, key, "minutes", effects["minutes"]))
    if "venue_like" in effects:
        seen = w["settings"].get("life_venues", {}).get(aid, {}).get(ctx["place"])
        if seen is not None:
            seen["like"] = round(max(-1.0, min(1.0, seen["like"] + effects["venue_like"])), 3)
    for pref, delta in effects.get("pref", {}).items():
        prefs = w["settings"].setdefault("life_prefs", {}).setdefault(aid, dict(rhythms.config(w).get("preferences", {})))
        prefs[pref] = max(-0.6, min(0.8, prefs.get(pref, 0.0) + delta))
    return applied


def _amount(w, key: str, purpose: str, spec) -> float:
    if isinstance(spec, (int, float)):
        return float(spec)
    return _draw(w, key, purpose, float(spec[0]), float(spec[1]))


def recent(c, w, aid: str, within_hours: float = 10.0, limit: int = 3) -> list[dict]:
    """她最近碰到的小事（给她的脑子）：越值得说的越靠前，最多几件。"""
    if not enabled(w, aid):
        return []
    items = [m for m in entity(c, w["world_id"], aid)["state"].get("recent_moments", [])
             if w["sim_time"] - m["at"] < within_hours * 3600]
    return sorted(sorted(items, key=lambda m: -m["share"])[:limit], key=lambda m: m["at"])
