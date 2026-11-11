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
    "home": {"home_living", "home_kitchen", "my_room", "home_bath"},
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


def schedule_choice(c, w, aid, delay_minutes: float | None = None, tag: str = "", ask: bool = True) -> None:
    if not enabled(w, aid):
        return
    key = f"life:{aid}:{int(w['sim_time'])}{tag}"
    if delay_minutes is None:
        delay_minutes = rhythms._draw(w, key, "delay", 2, 12)
    payload = {"actor_id": aid} if ask else {"actor_id": aid, "no_ask": True}
    job(c, w["world_id"], key, w["sim_time"] + delay_minutes * 60, "life_choose", payload)


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
    plans = _plan_boosts(w)
    broken_list = broken_items(c, w, aid)
    broken = {x["entity_id"] for x in broken_list}
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
        if choice.get("fixes") and not broken:
            continue
        place = here if choice["place"] == "here" else choice["place"]
        if not _has(c, w, place) or not entity(c, w["world_id"], place)["state"].get("open", True):
            continue
        score = choice.get("base", 0.5) + prefs.get(choice.get("pref", ""), 0.0) + plans.get(choice["id"], 0.0)
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
        if choice.get("fixes"):
            # 坏得越久越烦：一天内把"想修"的劲涨满
            age = max((w["sim_time"] - x["broken_at"]) / 3600 for x in broken_list)
            score += cfg["chooser"].get("fix_urgency", 0.8) * min(1.0, max(0.0, age) / 24)
        if broken & set(choice.get("uses", [])):
            score -= cfg["chooser"].get("broken_penalty", 0.8)  # 电脑坏了也能翻纸质的，只是没那么想
        result.append((choice, place, score))
    return result


def broken_items(c, w, aid) -> list[dict]:
    """她名下坏着、能修的东西。"""
    import json
    rows = c.execute("SELECT entity_id, label, state, capabilities FROM world_entities "
                     "WHERE world_id=? AND owner_id=? AND kind='item' ORDER BY entity_id", (w["world_id"], aid))
    items = []
    for r in rows:
        state = json.loads(r[2])
        if state.get("broken") and "repairable" in json.loads(r[3]):
            items.append({"entity_id": r[0], "label": r[1], "broken_at": state.get("broken_at", w["sim_time"])})
    return items


def after(c, w, aid, doing, reason) -> None:
    """自己修东西：做完按概率修好一件（先修平时要用的），修不好就是修不好，过阵子可能再试。"""
    spec = rhythms.config(w)["activities"].get(doing["activity"], {})
    if "fix_chance" not in spec or reason != "到时间了":
        return
    items = broken_items(c, w, aid)
    if not items:
        return
    used = {u for ch in rhythms.config(w)["choices"] for u in ch.get("uses", [])}
    item = sorted(items, key=lambda x: x["entity_id"] not in used)[0]
    key = f"{doing['job']}:fix"
    draw = rhythms._draw(w, key, "fix")
    if draw < spec["fix_chance"]:
        target = entity(c, w["world_id"], item["entity_id"])
        target["state"]["broken"] = False
        target["state"].pop("broken_at", None)
        save_entity(c, target)
        emit(c, w, key, "life.fixed", {"item": item["entity_id"], "label": item["label"], "draw": round(draw, 3),
                                       "changes": [target]}, aid)
    else:
        emit(c, w, key, "life.fix_failed", {"item": item["entity_id"], "label": item["label"], "draw": round(draw, 3)}, aid)


def _plan_boosts(w) -> dict[str, float]:
    """她的打算/心愿（大脑侧）给候选加分；没接大脑（独立世界、测试）时为空。"""
    if not w["settings"].get("mind_linked"):
        return {}
    try:
        from app.services.mind import intents
        return intents.boosts(w["sim_time"])
    except Exception:
        return {}


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
    from app.services.world import self_mind
    # 刚问过她却没想成（断网等）退回来的这一次，按规则挑，别再问一遍
    if not task["payload"].get("no_ask") and self_mind.enabled(w) and self_mind.used_today(c, w) < self_mind.per_day():
        reason = self_mind.conflict(c, w, aid, options)
        if reason:
            # 关键时刻让她自己拿主意：排给她的脑子，回来再落地；等的时候她就先待着
            self_mind.queue_decision(c, w, aid, options, reason, key)
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
        if w["settings"].get("mind_linked"):
            try:
                from app.services.mind import intents
                intents.mark_done(choice["id"], w["sim_time"])
            except Exception:
                pass
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


def taste(c, w, aid, doing, reason) -> None:
    """做完一件自己挑的事，按"过得怎样"小幅调喜好（D5）：不能一次定性，带抽样。

    好不好 = 抽样基调 + 天气（外面下雨减分）+ 身体（做着做着更累/更饿减分）+ 被打断减分。
    每次调 ±0.08 以内，喜好夹在 [-0.6, 0.8]。
    """
    source = doing.get("source", "")
    if not enabled(w, aid) or not source.startswith("life:") or source == "life:travel":
        return
    choice = next((ch for ch in rhythms.config(w)["choices"] if "life:" + ch["id"] == source), None)
    if not choice or not choice.get("pref"):
        return
    key = f"taste:{aid}:{choice['id']}:{int(w['sim_time'])}"
    mood = 2 * rhythms._draw(w, key, "mood") - 1          # 那天就是觉得好/不好
    place = entity(c, w["world_id"], entity(c, w["world_id"], aid)["parent_id"])
    weather = place["state"].get("weather")
    if weather is None and _has(c, w, "street") and _area(place["entity_id"]) == "out":
        weather = entity(c, w["world_id"], "street")["state"].get("weather")
    if weather == "rain" and _area(place["entity_id"]) == "out":
        mood -= 0.6
    needs = {n: rhythms._value_at(r, w["sim_time"]) for n, r in rhythms._rows(c, w["world_id"], aid).items()}
    mood -= 0.5 * max(0.0, needs.get("fatigue", 0) - 0.7) + 0.5 * max(0.0, needs.get("hunger", 0) - 0.7)
    if reason != "到时间了":
        mood -= 0.3
    prefs = w["settings"].setdefault("life_prefs", {}).setdefault(aid, dict(rhythms.config(w).get("preferences", {})))
    old = prefs.get(choice["pref"], 0.0)
    prefs[choice["pref"]] = max(-0.6, min(0.8, old + 0.08 * max(-1.0, min(1.0, mood))))
    emit(c, w, key, "life.taste", {"pref": choice["pref"], "from": round(old, 3), "to": round(prefs[choice["pref"]], 3),
                                   "weather": weather}, aid)


def handle(c, w, task) -> bool:
    if task["kind"] != "life_choose":
        return False
    choose(c, w, task)
    return True
