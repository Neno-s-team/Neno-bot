"""她的一天：空档里自己挑事做（D2）。不调模型，按 seed 抽样，可重放。

每当她空下来（一件事做完、醒来、义务结束），过几分钟在候选里挑一件：
    分 = 基础 + 喜好 + 身体（越饿越想吃、越累越想歇）+ 习惯（这个时段常在这儿）+ 噪声
按温度做 softmax 抽，不取最大值。挑中的事不会跨过下一个义务（时长截到义务前）；
要换片区（家 / 楼下 / 学校）就先"在路上"一段。参数在 content/world/rhythms/life.json 的 choices / chooser。
义务（上课、组会、饭点、睡觉）仍走 rhythms 的作息表，到点会打断正在做的事。
"""

from __future__ import annotations

import math

from app.services.world import city, rhythms
from app.storage.world_store import WorldError, emit, entity, job, save_entity

_AREAS = {
    "home": {"home_living", "home_kitchen", "my_room", "home_bath"},
    "campus": {"campus", "library", "seminar_room", "advisor_office", "canteen", "metro"},
}


def _area(place: str, w=None) -> str:
    if w is not None and city.enabled(w):
        return city.area(w, place)
    for name, places in _AREAS.items():
        if place in places:
            return name
    return "out"


def enabled(w, aid) -> bool:
    cfg = rhythms.config(w) or {}
    return bool(cfg.get("choices")) and bool(cfg.get("actors", {}).get(aid, {}).get("life"))


def travel_minutes(w, here: str, there: str) -> float:
    if city.enabled(w):
        return city.route(w, here, there)["minutes"]
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


def _candidates(c, w, aid, actor, key: str = "", room: float | None = None):
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
        if choice.get("group") and cooling.get("group:" + choice["group"], 0) > w["sim_time"]:
            continue  # 刚在外面吃过，就不会回家再吃一顿
        low, high = choice["hours"]
        if not low <= hour < high:
            continue
        if choice.get("weather_not") and weather == choice["weather_not"]:
            continue
        if choice.get("campus_only") and _area(here, w) != "campus":
            continue
        if choice.get("fixes") and not broken:
            continue
        bonus = 0.0
        if choice.get("venue"):
            if not city.enabled(w):
                continue
            picked = _pick_venue(w, aid, choice, here, hour, key or f"t{int(w['sim_time'])}")
            if picked is None:
                continue
            place, bonus = picked
        else:
            place = here if choice["place"] == "here" else choice["place"]
            if not _has(c, w, place) or not entity(c, w["world_id"], place)["state"].get("open", True):
                continue
        if room is not None and city.enabled(w):
            # 去一趟、做一会儿、再赶回下一件事：时间不够就不起这个念头（不会为了 20 分钟跑去 10 公里外看电影）
            way = city.route(w, here, place)["minutes"] / 60
            low = cfg["activities"][choice.get("activity") or "eat"]["hours"][0]
            if 2 * way + low > room:
                continue
        score = choice.get("base", 0.5) + prefs.get(choice.get("pref", ""), 0.0) + plans.get(choice["id"], 0.0) + bonus
        for need, weight in choice.get("relieves", {}).items():
            score += weight * max(0.0, needs.get(need, 0.0) - 0.3)
        score += cfg["chooser"]["habit_weight"] * min(1.0, habits.get(_habit_key(w, place), 0.0) / 5)
        if choice["id"] == w["settings"].get("life_last", {}).get(aid):
            score -= 0.5  # 刚做过的事，接着再做的兴致低一点
        spec = cfg["activities"].get(choice.get("activity") or "eat", {})
        if spec.get("requires_company") and not _someone_at(c, w, aid, place):
            continue
        if sick and _area(place, w) != "home":
            score -= 1.0  # 不舒服时不太想出门，但不是不能
        if choice.get("fixes"):
            # 坏得越久越烦：一天内把"想修"的劲涨满
            age = max((w["sim_time"] - x["broken_at"]) / 3600 for x in broken_list)
            score += cfg["chooser"].get("fix_urgency", 0.8) * min(1.0, max(0.0, age) / 24)
        if broken & set(choice.get("uses", [])):
            score -= cfg["chooser"].get("broken_penalty", 0.8)  # 电脑坏了也能翻纸质的，只是没那么想
        result.append((choice, place, score))
    return result


_EVERYDAY = ("noodle", "snack", "breakfast", "fast_food", "mala", "restaurant", "milk_tea", "cafe", "bakery",
             "fruit", "market", "supermarket", "convenience", "express", "pharmacy", "canteen")


def _venue_memory(w, aid) -> dict:
    """她在这一片住了好几年：第一次用到城市时，家和学校附近的店她多半已经知道——
    越近越可能去过；好吃的成了常去的，难吃的记得难吃。之后才是真的"第一次去"。"""
    memory = w["settings"].setdefault("life_venues", {})
    if aid in memory:
        return memory[aid]
    from app.services.world.randomness import sample
    known: dict = {}
    cfg = city.config(w)
    for anchor, reach in (("my_room", 1500), ("campus", 1000)):
        if anchor not in cfg.get("places", {}):
            continue
        for v in city.nearby(w, anchor, list(_EVERYDAY), reach, limit=200):
            d = city.dist(city.point(w, anchor), (v["lat"], v["lon"]))
            if v["id"] in known or sample(w["seed"], v["id"], "known") > 0.85 - 0.5 * d / reach:
                continue
            quality = 2 * sample(w["seed"], v["id"], "venue-quality") - 1
            visits = 1 + int(sample(w["seed"], v["id"], "visits") * (12 if quality > 0.2 else 3))
            known[v["id"]] = {"like": round(0.8 * quality + 0.2 * (2 * sample(w["seed"], v["id"], "like") - 1), 3),
                              "visits": visits, "first": None}
    memory[aid] = known
    return known


def _pick_venue(w, aid, choice, here, hour, key):
    """城里挑具体一家：近的、开着的、去过觉得好的、偶尔想试试没去过的，加一点那天的心血来潮。
    返回 (店 id, 对这件事兴致的加减)；附近没开着的就 None。"""
    spec = choice["venue"]
    found = [v for v in city.nearby(w, here, spec["kinds"], spec.get("within_m", 2500), town=spec.get("town", False),
                                    limit=spec.get("consider", 25)) if city.open_at(v, hour)]
    if not found:
        return None
    memory = _venue_memory(w, aid)
    curiosity = rhythms.config(w)["chooser"].get("curiosity", 0.3)
    best = None
    for v in found:
        minutes = city.route(w, here, v["id"])["minutes"]
        seen = memory.get(v["id"])
        # 去过的：印象 + 熟悉（去得多了就顺脚）；没去过的：一点好奇。大多数时候去常去的那几家
        known = seen["like"] + 0.25 * min(seen["visits"], 6) / 6 if seen else curiosity
        if seen and seen.get("last"):
            ago = (w["sim_time"] - seen["last"]) / 3600
            known -= 0.6 if ago < 20 else 0.25 if ago < 60 else 0.0  # 昨天刚吃过，今天换一家
        score = (-minutes / spec.get("minutes_scale", 20) + known
                 + 0.3 * (2 * rhythms._draw(w, key, f"venue:{choice['id']}:{v['id']}") - 1))
        if best is None or score > best[1]:
            best = (v["id"], score, minutes, seen["like"] if seen else 0.0)
    # 想不想出门：只看路远不远、那家有多喜欢；没去过的店不扣兴致
    far = max(0.0, best[2] - 5) / spec.get("minutes_scale", 20)  # 楼下五分钟以内不算远
    return best[0], max(-1.0, min(0.6, 0.5 * max(0.0, best[3]) - 0.4 * far))


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
    options = _candidates(c, w, aid, actor, key, room)
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
        cooling = w["settings"].setdefault("life_cooldown", {}).setdefault(aid, {})
        if choice.get("cooldown_hours"):
            cooling[choice["id"]] = w["sim_time"] + choice["cooldown_hours"] * 3600
        if choice.get("group"):
            cooling["group:" + choice["group"]] = w["sim_time"] + cfg.get("groups", {}).get(choice["group"], 2) * 3600
    except WorldError as error:
        # 这件事现在做不成（没钱、东西没了、店关了）：一段时间内不再想它
        w["settings"].setdefault("life_cooldown", {}).setdefault(aid, {})[choice["id"]] = w["sim_time"] + 3 * 3600
        emit(c, w, key + ":blocked", "life.blocked", {"choice": choice["id"], "reason": str(error)}, aid)
        schedule_choice(c, w, aid)


def _do(c, w, aid, actor, choice, place, room, key) -> None:
    cfg = rhythms.config(w)
    activity = choice.get("activity") or "eat"
    span = cfg["activities"][activity]["hours"]
    way = city.route(w, actor["parent_id"], place) if city.enabled(w) else None
    travel = (way["minutes"] if way else travel_minutes(w, actor["parent_id"], place)) / 60
    hours = min(rhythms._draw(w, key, "hours", *span), max(0.1, room - travel - 0.05))
    after = {"activity": activity, "place": place, "hours": hours, "source": "life:" + choice["id"],
             "eat": choice.get("eat")}
    if choice.get("venue"):
        city.ensure_place(c, w, place)
        after["venue"] = {"id": place, "price": city.price(city.data(w)["by_id"][place]),
                          "meal": bool(choice["venue"].get("meal"))}
    if travel > 0:
        rhythms.start_activity(c, w, aid, "commute", source="life:travel", hours=travel, key=key + ":go", after=after)
        if way and way.get("label"):
            mover = entity(c, w["world_id"], aid)
            mover["state"]["doing"]["route"] = way["label"]  # 她知道自己是怎么去的：走路/骑车/坐几号线
            save_entity(c, mover)
    else:
        arrive(c, w, aid, after, key)


def arrive(c, w, aid, after, key) -> None:
    actor = entity(c, w["world_id"], aid)
    if actor["parent_id"] != after["place"]:
        actor["parent_id"] = after["place"]
        save_entity(c, actor)
    if after.get("eat"):
        _eat(c, w, aid, after["eat"], key)
    if after.get("venue"):
        _visit(c, w, aid, after["venue"], key)
    rhythms.start_activity(c, w, aid, after["activity"], source=after["source"], hours=after.get("hours"), key=key + ":do")
    habits = w["settings"].setdefault("life_habits", {}).setdefault(aid, {})
    for k in list(habits):
        habits[k] *= 0.97  # 不去就慢慢淡
    habits[_habit_key(w, after["place"])] = habits.get(_habit_key(w, after["place"]), 0.0) + 1


def _visit(c, w, aid, venue, key) -> None:
    """到了城里的某家店：记一笔（去过几次、花了多少），在外面吃饭也算吃饱。钱的账（C 之后）再接。"""
    memory = _venue_memory(w, aid)
    seen = memory.setdefault(venue["id"], {"like": 0.0, "visits": 0, "first": w["sim_time"]})
    seen["visits"] += 1
    seen["last"] = w["sim_time"]
    if venue.get("meal"):
        rhythms.on_consume(c, w, aid, "neno:meal", 1, key)
    emit(c, w, key + ":visit", "life.visit", {"venue": venue["id"], "name": city.label(c, w, venue["id"]),
                                              "price": venue.get("price", 0), "visits": seen["visits"]}, aid)


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
    _venue_taste(c, w, aid, reason)
    choice = next((ch for ch in rhythms.config(w)["choices"] if "life:" + ch["id"] == source), None)
    if not choice or not choice.get("pref"):
        return
    key = f"taste:{aid}:{choice['id']}:{int(w['sim_time'])}"
    mood = 2 * rhythms._draw(w, key, "mood") - 1          # 那天就是觉得好/不好
    place = entity(c, w["world_id"], entity(c, w["world_id"], aid)["parent_id"])
    weather = place["state"].get("weather")
    if weather is None and _has(c, w, "street") and _area(place["entity_id"], w) == "out":
        weather = entity(c, w["world_id"], "street")["state"].get("weather")
    if weather == "rain" and _area(place["entity_id"], w) == "out":
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


def _venue_taste(c, w, aid, reason) -> None:
    """在城里某家店待完：印象 = 这家店本身（种子定的隐藏品质，好吃难吃是店的事）+ 那天的心情。
    第一次去印象定得重，之后慢慢修正。"""
    here = entity(c, w["world_id"], aid)["parent_id"]
    seen = w["settings"].get("life_venues", {}).get(aid, {}).get(here)
    if seen is None:
        return
    from app.services.world.randomness import sample
    quality = 2 * sample(w["seed"], here, "venue-quality") - 1
    key = f"venue-taste:{aid}:{here}:{int(w['sim_time'])}"
    mood = 2 * rhythms._draw(w, key, "mood") - 1 - (0.3 if reason != "到时间了" else 0.0)
    felt = 0.7 * quality + 0.3 * mood
    weight = 0.6 if seen["visits"] <= 1 else 0.25
    old = seen.get("like", 0.0)
    seen["like"] = round(max(-1.0, min(1.0, old + weight * (felt - old))), 3)
    emit(c, w, key, "life.venue_taste", {"venue": here, "from": old, "to": seen["like"]}, aid)


def handle(c, w, task) -> bool:
    if task["kind"] != "life_choose":
        return False
    choose(c, w, task)
    return True
