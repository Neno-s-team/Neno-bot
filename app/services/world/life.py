"""她的一天：空档里自己挑事做（D2）。不调模型，按 seed 抽样，可重放。

每当她空下来（一件事做完、醒来、义务结束），过几分钟在候选里挑一件：
    分 = 基础 + 喜好 + 身体（越饿越想吃、越累越想歇）+ 习惯（这个时段常在这儿）+ 噪声
按温度做 softmax 抽，不取最大值。挑中的事不会跨过下一个义务（时长截到义务前）；
要换片区（家 / 楼下 / 学校）就先"在路上"一段。参数在 content/world/rhythms/life.json 的 choices / chooser。
义务（上课、组会、饭点、睡觉）仍走 rhythms 的作息表，到点会打断正在做的事。
"""

from __future__ import annotations

import math

from app.services.world import rhythms
from app.storage.world_store import WorldError, emit, entity, job, save_entity

_AREAS = {
    "home": {"home_living", "home_kitchen", "my_room", "home_bath", "parents_room"},
    "campus": {"campus", "library", "seminar_room", "advisor_office", "canteen", "metro"},
}


def _area(place: str) -> str:
    for name, places in _AREAS.items():
        if place in places:
            return name
    return "out"


def enabled(w, aid) -> bool:
    cfg = rhythms.config(w) or {}
    return bool(cfg.get("choices")) and bool(cfg.get("actors", {}).get(aid, {}).get("life"))


def travel_minutes(w, here: str, there: str) -> float:
    if _area(here) == _area(there):
        return 0.0
    table = rhythms.config(w).get("travel_minutes", {})
    return float(max(table.get(there, 10), table.get(here, 10)))


def schedule_choice(c, w, aid, delay_minutes: float | None = None) -> None:
    if not enabled(w, aid):
        return
    key = f"life:{aid}:{int(w['sim_time'])}"
    if delay_minutes is None:
        delay_minutes = rhythms._draw(w, key, "delay", 2, 12)
    job(c, w["world_id"], key, w["sim_time"] + delay_minutes * 60, "life_choose", {"actor_id": aid})


def _next_obligation(c, w, aid) -> float | None:
    cfg = rhythms.config(w)
    day, _ = rhythms._local_day(w, w["sim_time"])
    soonest = None
    for entry in cfg["actors"][aid].get("routine", []):
        for d in (day, day + 1):
            occ = rhythms._occurrence(w, aid, entry, d)
            if occ is None:
                continue
            begin = occ[0] - (travel_minutes(w, entity(c, w["world_id"], aid)["parent_id"], entry["place"]) * 60
                              if entry.get("commute") else 0)
            if begin > w["sim_time"] and (soonest is None or begin < soonest):
                soonest = begin
    return soonest


def _habit_key(w, place: str) -> str:
    hour = int(((w["sim_time"] + w["settings"]["utc_offset_minutes"] * 60) % 86400) // 3600)
    return f"{place}:{hour // 3}"


def _candidates(c, w, aid, actor):
    cfg = rhythms.config(w)
    here = actor["parent_id"]
    hour = ((w["sim_time"] + w["settings"]["utc_offset_minutes"] * 60) % 86400) / 3600
    weather = entity(c, w["world_id"], "street")["state"].get("weather") if _has(c, w, "street") else None
    needs = {n: rhythms._value_at(r, w["sim_time"]) for n, r in rhythms._rows(c, w["world_id"], aid).items()}
    prefs = w["settings"].setdefault("life_prefs", {}).setdefault(aid, dict(cfg.get("preferences", {})))
    habits = w["settings"].setdefault("life_habits", {}).setdefault(aid, {})
    sick = bool(actor["state"].get("conditions"))
    result = []
    cooling = w["settings"].setdefault("life_cooldown", {}).setdefault(aid, {})
    for choice in cfg["choices"]:
        if cooling.get(choice["id"], 0) > w["sim_time"]:
            continue
        low, high = choice["hours"]
        if not low <= hour < high:
            continue
        if choice.get("weather_not") and weather == choice["weather_not"]:
            continue
        if choice.get("campus_only") and _area(here) != "campus":
            continue
        place = here if choice["place"] == "here" else choice["place"]
        if not _has(c, w, place) or not entity(c, w["world_id"], place)["state"].get("open", True):
            continue
        score = choice.get("base", 0.5) + prefs.get(choice.get("pref", ""), 0.0)
        for need, weight in choice.get("relieves", {}).items():
            score += weight * max(0.0, needs.get(need, 0.0) - 0.3)
        score += cfg["chooser"]["habit_weight"] * min(1.0, habits.get(_habit_key(w, place), 0.0) / 5)
        if choice["id"] == w["settings"].get("life_last", {}).get(aid):
            score -= 0.5  # 刚做过的事，接着再做的兴致低一点
        spec = cfg["activities"].get(choice.get("activity") or "eat", {})
        if spec.get("requires_company") and not _someone_at(c, w, aid, place):
            continue
        if sick and _area(place) != "home":
            score -= 1.0  # 不舒服时不太想出门，但不是不能
        result.append((choice, place, score))
    return result


def _someone_at(c, w, aid, place) -> bool:
    import json
    return any(json.loads(r[0]).get("active", True) and not (json.loads(r[0]).get("doing") or {}).get("activity") == "sleep"
               for r in c.execute("SELECT state FROM world_entities WHERE world_id=? AND kind='actor' AND parent_id=? AND entity_id!=?",
                                  (w["world_id"], place, aid)))


def _has(c, w, eid) -> bool:
    return c.execute("SELECT 1 FROM world_entities WHERE world_id=? AND entity_id=?", (w["world_id"], eid)).fetchone() is not None


def choose(c, w, task) -> None:
    aid = task["payload"]["actor_id"]
    actor = entity(c, w["world_id"], aid)
    if not enabled(w, aid) or not actor["state"].get("active", True) or actor["state"].get("doing"):
        return
    if c.execute("SELECT 1 FROM world_actions WHERE world_id=? AND actor_id=? AND status IN ('running','paused')",
                 (w["world_id"], aid)).fetchone():
        return
    cfg, key = rhythms.config(w), task["job_id"]
    deadline = _next_obligation(c, w, aid)
    room = (deadline - w["sim_time"]) / 3600 if deadline else 24.0
    options = _candidates(c, w, aid, actor)
    if not options or room < 0.25:
        emit(c, w, key + ":idle", "life.idle", {"room_hours": round(room, 2)}, aid)
        if room >= 0.25:
            schedule_choice(c, w, aid, rhythms._draw(w, key, "idle", *cfg["chooser"]["idle_minutes"]))
        return
    temperature, noise = cfg["chooser"]["temperature"], cfg["chooser"]["noise"]
    scored = [(ch, pl, s + noise * (2 * rhythms._draw(w, key, "noise:" + ch["id"]) - 1)) for ch, pl, s in options]
    top = max(s for _, _, s in scored)
    weights = [math.exp((s - top) / max(0.05, temperature)) for _, _, s in scored]
    cursor = rhythms._draw(w, key, "pick") * sum(weights)
    choice, place, score = scored[-1]
    for item, weight in zip(scored, weights):
        cursor -= weight
        if cursor < 0:
            choice, place, score = item
            break
    w["settings"].setdefault("life_last", {})[aid] = choice["id"]
    emit(c, w, key + ":chose", "life.chose", {"choice": choice["id"], "place": place, "score": round(score, 3),
         "options": sorted(([o[0]["id"], round(o[2], 2)] for o in scored), key=lambda x: -x[1])[:5]}, aid)
    try:
        _do(c, w, aid, actor, choice, place, room, key)
        if choice.get("cooldown_hours"):
            w["settings"].setdefault("life_cooldown", {}).setdefault(aid, {})[choice["id"]] =                 w["sim_time"] + choice["cooldown_hours"] * 3600
    except WorldError as error:
        # 这件事现在做不成（没钱、东西没了、店关了）：一段时间内不再想它
        w["settings"].setdefault("life_cooldown", {}).setdefault(aid, {})[choice["id"]] = w["sim_time"] + 3 * 3600
        emit(c, w, key + ":blocked", "life.blocked", {"choice": choice["id"], "reason": str(error)}, aid)
        schedule_choice(c, w, aid)


def _do(c, w, aid, actor, choice, place, room, key) -> None:
    cfg = rhythms.config(w)
    activity = choice.get("activity") or "eat"
    span = cfg["activities"][activity]["hours"]
    travel = travel_minutes(w, actor["parent_id"], place) / 60
    hours = min(rhythms._draw(w, key, "hours", *span), max(0.1, room - travel - 0.05))
    after = {"activity": activity, "place": place, "hours": hours, "source": "life:" + choice["id"],
             "eat": choice.get("eat")}
    if travel > 0:
        rhythms.start_activity(c, w, aid, "commute", source="life:travel", hours=travel, key=key + ":go", after=after)
    else:
        arrive(c, w, aid, after, key)


def arrive(c, w, aid, after, key) -> None:
    actor = entity(c, w["world_id"], aid)
    if actor["parent_id"] != after["place"]:
        actor["parent_id"] = after["place"]
        save_entity(c, actor)
    if after.get("eat"):
        _eat(c, w, aid, after["eat"], key)
    rhythms.start_activity(c, w, aid, after["activity"], source=after["source"], hours=after.get("hours"), key=key + ":do")
    habits = w["settings"].setdefault("life_habits", {}).setdefault(aid, {})
    for k in list(habits):
        habits[k] *= 0.97  # 不去就慢慢淡
    habits[_habit_key(w, after["place"])] = habits.get(_habit_key(w, after["place"]), 0.0) + 1


def _eat(c, w, aid, item_id, key) -> None:
    item = entity(c, w["world_id"], item_id)
    if item["state"].get("quantity", 1) < 1:
        raise WorldError("Nothing left to eat", 409)
    actor = entity(c, w["world_id"], aid)
    price = item["state"].get("price_credits")
    if price:
        if actor["state"].get("credits", 0) < price:
            raise WorldError("Not enough money", 409)
        actor["state"]["credits"] -= price
        save_entity(c, actor)
    if item["state"].get("quantity", 1) < 90:
        item["state"]["quantity"] = item["state"].get("quantity", 1) - 1
        save_entity(c, item)
    rhythms.on_consume(c, w, aid, item["type_id"], 1, key)


def handle(c, w, task) -> bool:
    if task["kind"] != "life_choose":
        return False
    choose(c, w, task)
    return True
