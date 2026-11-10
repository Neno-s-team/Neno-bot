"""W3 需求与节律：需求随时间累积、越阈只产生"感觉"并催角色考虑；作息给出一天的骨架。

原则：规则只产生感觉、代价与机会，不替角色做决定。阈值、作息时刻、是否跳过、
习惯性反应、后果是否发生、吃东西缓解多少都按 seed 抽样，可重放但不可预测。
需求值按"上次结算值 + 速率 × 经过时间"惰性计算，只在越阈/作息/活动起止时排到期任务，
不每秒写库。参数来自 content/world/rhythms/*.json（占位，人设确定后由 W1 重填）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services.world import actions
from app.services.world.randomness import sample
from app.storage.world_store import WorldError, decode_entity, emit, encode, entity, job, save_entity

CONTENT = Path(__file__).resolve().parents[3] / "content/world/rhythms"
EPS = 1e-9


def load(name: str) -> dict:
    path = CONTENT / (name + ".json")
    if not path.is_file():
        raise WorldError("Rhythm profile not found", 404)
    return json.loads(path.read_text(encoding="utf-8"))


def config(w: dict) -> dict | None:
    return w["settings"].get("rhythms")


def _draw(w, key, purpose, low=0.0, high=1.0):
    return low + (high - low) * sample(w["seed"], key, purpose)


def _local_day(w, t):
    offset = w["settings"]["utc_offset_minutes"] * 60
    day = int((t + offset) // 86400)
    return day, day * 86400 - offset


# ── 需求存取与结算 ──────────────────────────────────────────────────────────

def _rows(c, wid, aid):
    rows = c.execute("SELECT * FROM world_need WHERE world_id=? AND actor_id=?", (wid, aid)).fetchall()
    return {r["need"]: {**dict(r), "data": json.loads(r["data"])} for r in rows}


def _save(c, row):
    c.execute("UPDATE world_need SET value=?,at=?,base=?,data=? WHERE world_id=? AND actor_id=? AND need=?",
              (row["value"], row["at"], row["base"], encode(row["data"]), row["world_id"], row["actor_id"], row["need"]))


def _rate(row):
    mult = 1.0
    for mod in row["data"].get("mods", []):
        if mod["until"] > row["at"] + EPS:
            mult *= mod["mult"]
    return row["base"] * mult + row["data"].get("activity", 0.0)


def _value_at(row, t):
    return min(1.0, max(0.0, row["value"] + _rate(row) * (t - row["at"]) / 3600))


def settle(c, w, aid, now=None):
    """把该角色所有需求结算到 now，并重排越阈/后果/到期任务。"""
    now = w["sim_time"] if now is None else now
    rows = _rows(c, w["world_id"], aid)
    for row in rows.values():
        row["value"], row["at"] = _value_at(row, now), now
        row["data"]["mods"] = [m for m in row["data"].get("mods", []) if m["until"] > now + EPS]
    for row in rows.values():
        _reschedule(c, w, row)
    return rows


def _reschedule(c, w, row):
    cfg = config(w)["needs"][row["need"]]
    data = row["data"]
    data["gen"] = data.get("gen", 0) + 1
    if row["value"] < data["urge"] - EPS:
        data["urged"] = False
    data["strained"] = [i for i in data.get("strained", [])
                        if row["value"] >= config(w)["strain"][i]["at"] - EPS]
    _save(c, row)
    rate, base = _rate(row), f"need:{row['actor_id']}:{row['need']}:{data['gen']}"

    def at(target):
        return row["at"] + (target - row["value"]) / rate * 3600

    payload = {"actor_id": row["actor_id"], "need": row["need"], "gen": data["gen"]}
    if row["value"] >= data["urge"] - EPS:
        hours = _draw(w, base, "nag", *cfg["nag_hours"])
        job(c, w["world_id"], base + ":nag", row["at"] + hours * 3600, "need", {**payload, "kind": "urge"})
    elif rate > 0:
        job(c, w["world_id"], base + ":urge", at(data["urge"]), "need", {**payload, "kind": "urge"})
    if rate > 0:
        for i, rule in enumerate(config(w)["strain"]):
            if rule["need"] == row["need"] and i not in data["strained"] and row["value"] < rule["at"]:
                job(c, w["world_id"], f"{base}:strain{i}", at(rule["at"]), "need", {**payload, "kind": "strain", "rule": i})
    ends = [m["until"] for m in data.get("mods", [])]
    if ends:
        job(c, w["world_id"], base + ":mod", min(ends), "need", {**payload, "kind": "settle"})


def felt(c, w, aid) -> dict:
    """给角色看的身体感觉（文字，不给数值）；没感觉的需求不列；身上的病痛一并列出。"""
    if not config(w):
        return {}
    result = {}
    for condition in entity(c, w["world_id"], aid)["state"].get("conditions", []):
        if condition["until"] > w["sim_time"]:
            result.setdefault("身体", condition["label"])
    for need, row in _rows(c, w["world_id"], aid).items():
        cfg, value = config(w)["needs"][need], _value_at(row, w["sim_time"])
        level = 0 if value < row["data"]["urge"] - EPS else 1 if value < 0.75 else 2 if value < 0.9 else 3
        if level:
            result[cfg["label"]] = cfg["felt"][level]
    return result


def status(c, w, aid) -> dict:
    rows = _rows(c, w["world_id"], aid)
    return {"actor_id": aid, "sim_time": w["sim_time"], "felt": felt(c, w, aid), "needs": {
        need: {"value": round(_value_at(r, w["sim_time"]), 4), "rate_per_hour": round(_rate(r), 4),
               "urge_at": r["data"]["urge"], "mods": r["data"].get("mods", [])} for need, r in rows.items()}}


# ── 初始化 ──────────────────────────────────────────────────────────────────

def setup(c, w, profile: str, actor_ids: list[str]) -> None:
    cfg = load(profile)
    w["settings"]["rhythms"] = cfg
    day, _ = _local_day(w, w["sim_time"])
    for aid in actor_ids:
        initial = cfg.get("actors", {}).get(aid, {}).get("initial", {})
        for need, spec in cfg["needs"].items():
            data = {"urge": _draw(w, f"urge:{day}:{aid}:{need}", "urge", *spec["urge"]), "gen": 0, "mods": [], "activity": 0.0}
            c.execute("INSERT INTO world_need VALUES (?,?,?,?,?,?,?)", (
                w["world_id"], aid, need, float(initial.get(need, spec["initial"])), w["sim_time"],
                _day_rate(w, spec, day, aid, need), encode(data)))
        settle(c, w, aid)
        for entry in cfg.get("actors", {}).get(aid, {}).get("routine", []):
            _schedule_routine(c, w, aid, entry, w["sim_time"], include_running=True)
    _, start = _local_day(w, w["sim_time"])
    job(c, w["world_id"], f"rhythm_day:{day + 1}", start + 86400, "rhythm_day", {"day": day + 1})


def _day_rate(w, spec, day, aid, need):
    spread = spec.get("day_variation", 0)
    return spec["rate_per_hour"] * (1 + spread * (2 * _draw(w, f"day:{day}:{aid}:{need}", "rate") - 1))


def _new_day(c, w, task):
    """每天重抽一次各需求的速率与阈值：今天容易饿、明天熬得住，都不是写死的。"""
    cfg, day = config(w), task["payload"]["day"]
    actors = {r["actor_id"] for r in c.execute("SELECT DISTINCT actor_id FROM world_need WHERE world_id=?", (w["world_id"],))}
    for aid in sorted(actors):
        rows = settle(c, w, aid)
        for need, row in rows.items():
            spec = cfg["needs"][need]
            row["base"] = _day_rate(w, spec, day, aid, need)
            row["data"]["urge"] = _draw(w, f"urge:{day}:{aid}:{need}", "urge", *spec["urge"])
            _reschedule(c, w, row)
    job(c, w["world_id"], f"rhythm_day:{day + 1}", task["run_at"] + 86400, "rhythm_day", {"day": day + 1})


# ── 作息 ────────────────────────────────────────────────────────────────────

def _clock(text):
    hours, minutes = text.split(":")
    return int(hours) * 3600 + int(minutes) * 60


def _occurrence(w, aid, entry, day):
    """某天这条作息的实际开始/结束（带抖动）；当天不适用返回 None。"""
    _, start = _local_day(w, day * 86400 - w["settings"]["utc_offset_minutes"] * 60)
    weekday = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(days=day)).isoweekday()
    if entry.get("days") and weekday not in entry["days"]:
        return None
    jitter = entry.get("jitter_min", 0) * 60
    key = f"routine:{aid}:{entry['id']}:{day}"
    begin = start + _clock(entry["start"]) + _draw(w, key, "start", -jitter, jitter)
    if entry.get("end"):
        finish = start + _clock(entry["end"]) + _draw(w, key, "end", -jitter, jitter)
        if finish <= begin:
            finish += 86400
    else:
        finish = None
    return begin, finish


def _schedule_routine(c, w, aid, entry, after, include_running=False):
    day, _ = _local_day(w, after)
    for d in range(day - 1, day + 8):
        occ = _occurrence(w, aid, entry, d)
        if occ is None:
            continue
        begin, finish = occ
        lead = 0.0
        if entry.get("commute"):
            from app.services.world import life
            here = entity(c, w["world_id"], aid)["parent_id"]
            lead = life.travel_minutes(w, here, entry["place"]) * 60
        if begin - lead > after or (include_running and finish and begin <= after < finish - 600):
            job(c, w["world_id"], f"routine:{aid}:{entry['id']}:{d}", max(begin - lead, after), "routine",
                {"actor_id": aid, "entry": entry["id"], "day": d, "until": finish, "begin": begin})
            return


def _routine(c, w, task):
    p = task["payload"]
    aid, cfg = p["actor_id"], config(w)
    entry = next(e for e in cfg["actors"][aid]["routine"] if e["id"] == p["entry"])
    key = task["job_id"]
    actor = entity(c, w["world_id"], aid)
    try:
        if not actor["state"].get("active", True):
            raise WorldError("角色不在场")
        if _draw(w, key, "skip") < entry.get("skip", 0):
            emit(c, w, key + ":skip", "routine.skipped", {"entry": entry["id"], "activity": entry["activity"]}, aid)
            return
        if actor["state"].get("doing"):
            end_activity(c, w, aid, reason="作息到点")
            actor = entity(c, w["world_id"], aid)
        place = entity(c, w["world_id"], entry["place"])
        if entry.get("commute") and p.get("begin") and p["begin"] - w["sim_time"] > 60 and actor["parent_id"] != place["entity_id"]:
            after = {"activity": entry["activity"], "place": entry["place"], "source": "routine:" + entry["id"],
                     "hours": (p["until"] - p["begin"]) / 3600 if p.get("until") else None}
            start_activity(c, w, aid, "commute", source="routine:" + entry["id"], key=key + ":go",
                           hours=(p["begin"] - w["sim_time"]) / 3600, after=after)
            return
        if actor["parent_id"] != place["entity_id"]:
            actor["parent_id"] = place["entity_id"]
            save_entity(c, actor)
            event = emit(c, w, key + ":go", "routine.moved", {"to": place["entity_id"], "changes": [actor]}, aid)
            actions.observe(c, w, aid, event, "arrival")
        hours = (p["until"] - w["sim_time"]) / 3600 if p.get("until") else None
        start_activity(c, w, aid, entry["activity"], source="routine:" + entry["id"], hours=hours,
                       opens=entry.get("opens"), then=entry.get("then"), key=key)
    except WorldError as error:
        emit(c, w, key + ":blocked", "routine.blocked", {"entry": entry["id"], "reason": str(error)}, aid)
    finally:
        _schedule_routine(c, w, aid, entry, task["run_at"] + 600)


# ── 活动（睡觉/歇会儿/上班/聊天/散步）──────────────────────────────────────

def activity_feasible(c, w, actor, name) -> None:
    spec = (config(w) or {}).get("activities", {}).get(name)
    if not spec:
        raise WorldError("Unknown activity")
    if actor["state"].get("doing", {}).get("activity") == name:
        raise WorldError("Already doing this", 409)
    here = actions.visible(c, w["world_id"], actor)
    if spec.get("requires_type") and not any(x["type_id"] == spec["requires_type"] for x in here):
        raise WorldError("Nothing here to do that with", 409)
    if spec.get("requires_company") and not _company(c, w, actor):
        raise WorldError("Nobody here to talk to", 409)


def _company(c, w, actor):
    return [decode_entity(r) for r in c.execute(
        "SELECT * FROM world_entities WHERE world_id=? AND kind='actor' AND parent_id=? AND entity_id!=?",
        (w["world_id"], actor["parent_id"], actor["entity_id"])) if json.loads(r["state"]).get("active", True)]


def start_activity(c, w, aid, name, source, hours=None, opens=None, then=None, key=None, after=None):
    actor = entity(c, w["world_id"], aid)
    if actor["state"].get("doing"):
        end_activity(c, w, aid, reason="换去做别的")
        actor = entity(c, w["world_id"], aid)
    activity_feasible(c, w, actor, name)
    spec = config(w)["activities"][name]
    key = key or f"activity:{aid}:{name}:{int(w['sim_time'])}"
    if hours is None:
        hours = _draw(w, key, "hours", *spec["hours"])
    until = w["sim_time"] + max(60, hours * 3600)
    rows = settle(c, w, aid)
    for need, rate in spec.get("rates", {}).items():
        if need in rows:
            rows[need]["data"]["activity"] = rate
            _reschedule(c, w, rows[need])
    changes = []
    if opens:
        place = entity(c, w["world_id"], opens)
        place["state"]["open"] = True
        save_entity(c, place)
        changes.append(place)
    actor["state"]["doing"] = {"activity": name, "label": spec["label"], "since": w["sim_time"], "until": until,
                               "source": source, "opens": opens, "then": then, "job": key + ":end"}
    if after:
        actor["state"]["doing"]["after"] = after
    save_entity(c, actor)
    changes.append(actor)
    job(c, w["world_id"], key + ":end", until, "activity_end", {"actor_id": aid, "job": key + ":end"})
    event = emit(c, w, key + ":start", "activity.started",
                 {"activity": name, "until": until, "source": source, "changes": changes}, aid)
    return {"activity": name, "until": until, "event_id": event}


def end_activity(c, w, aid, reason="到时间了"):
    actor = entity(c, w["world_id"], aid)
    doing = actor["state"].get("doing")
    if not doing:
        return None
    rows = settle(c, w, aid)
    for row in rows.values():
        row["data"]["activity"] = 0.0
        _reschedule(c, w, row)
    spec = config(w)["activities"][doing["activity"]]
    changes = []
    worked = max(0.0, (w["sim_time"] - doing["since"]) / 3600)
    if spec.get("wage_per_hour"):
        actor["state"]["credits"] = actor["state"].get("credits", 0) + int(worked * spec["wage_per_hour"])
    if doing.get("opens"):
        place = entity(c, w["world_id"], doing["opens"])
        place["state"]["open"] = False
        save_entity(c, place)
        changes.append(place)
    if doing.get("then"):
        actor["parent_id"] = doing["then"]
    actor["state"].pop("doing")
    save_entity(c, actor)
    changes.append(actor)
    c.execute("UPDATE world_jobs SET status='cancelled' WHERE world_id=? AND job_id=? AND status='pending'",
              (w["world_id"], doing["job"]))
    event = emit(c, w, f"{doing['job']}:ended:{int(w['sim_time'])}", "activity.ended",
                 {"activity": doing["activity"], "reason": reason, "hours": round(worked, 2), "changes": changes}, aid)
    actions.observe(c, w, aid, event, "activity_end")
    _nudge(c, w, aid)
    from app.services.world import life
    if doing.get("after") and reason == "到时间了":
        follow = doing["after"]
        if follow.get("hours") is None:
            follow = {**follow, "hours": None}
        try:
            life.arrive(c, w, aid, follow, doing["job"] + ":arrive")
        except WorldError as error:
            emit(c, w, doing["job"] + ":arrive-blocked", "life.blocked", {"reason": str(error)}, aid)
            life.schedule_choice(c, w, aid)
    elif reason == "到时间了":
        life.schedule_choice(c, w, aid)
    return event


def _nudge(c, w, aid):
    """让角色尽快重新考虑（不替它决定做什么）。"""
    c.execute("UPDATE world_minds SET next_at=MIN(next_at,?) WHERE world_id=? AND actor_id=?",
              (w["sim_time"], w["world_id"], aid))


# ── 越阈 / 后果 / 吃东西 ────────────────────────────────────────────────────

def _need_event(c, w, task):
    p = task["payload"]
    aid, need = p["actor_id"], p["need"]
    if p["kind"] == "settle":
        settle(c, w, aid)
        return
    rows = _rows(c, w["world_id"], aid)
    row = rows.get(need)
    if row is None or row["data"].get("gen") != p["gen"]:
        return
    row["value"], row["at"] = _value_at(row, w["sim_time"]), w["sim_time"]
    key = task["job_id"]
    if p["kind"] == "strain":
        rule = config(w)["strain"][p["rule"]]
        row["data"].setdefault("strained", []).append(p["rule"])
        draw = _draw(w, key, "strain")
        # 和随机机会一样，抽签本身也留痕：没出事也可追溯。
        emit(c, w, key + ":draw", "need.strain", {"need": need, "label": rule["label"], "draw": draw,
                                                  "probability": rule["probability"], "happened": draw < rule["probability"]}, aid)
        if draw < rule["probability"]:
            target = row if rule["effect"]["need"] == need else rows[rule["effect"]["need"]]
            if target is not row:
                target["value"], target["at"] = _value_at(target, w["sim_time"]), w["sim_time"]
            target["data"].setdefault("mods", []).append({"mult": rule["effect"]["rate_mult"], "label": rule["label"],
                                                          "until": w["sim_time"] + rule["effect"]["hours"] * 3600})
            emit(c, w, key, "need.consequence", {"need": need, "label": rule["label"], "effect": rule["effect"]}, aid)
            if target is not row:
                _reschedule(c, w, target)
        _reschedule(c, w, row)
        return
    row["data"]["urged"] = True
    if entity(c, w["world_id"], aid)["state"].get("doing", {}).get("activity") == "sleep":
        # 睡着时感觉不到；醒来结算时会重新排催促。
        _save(c, row)
        return
    _reschedule(c, w, row)
    habit = config(w)["needs"][need].get("habit") or {}
    emit(c, w, key, "need.urge", {"need": need, "felt": felt(c, w, aid)}, aid)
    _nudge(c, w, aid)
    if _draw(w, key, "habit") < habit.get("probability", 0):
        option = habit["options"][int(_draw(w, key, "habit_option") * len(habit["options"]))]
        _habit(c, w, aid, option, key)


def _habit(c, w, aid, option, key):
    """习惯性的顺手反应：条件不满足就算了，不强求。"""
    actor = entity(c, w["world_id"], aid)
    if not actor["state"].get("active", True) or actor["state"].get("doing"):
        return
    if c.execute("SELECT 1 FROM world_actions WHERE world_id=? AND actor_id=? AND status IN ('running','paused')",
                 (w["world_id"], aid)).fetchone():
        return
    c.execute("SAVEPOINT habit")
    try:
        if option == "eat":
            food = [x for x in actions.visible(c, w["world_id"], actor) if "edible" in x["capabilities"]
                    and x.get("owner_id") in (None, aid) and x["state"].get("quantity", 1) > 0
                    and not x["state"].get("price_credits")]
            if not food:
                c.execute("RELEASE habit")
                return
            pick = food[int(_draw(w, key, "food") * len(food))]
            changes = actions.execute(c, w, key + ":eat", {"actor_id": aid, "operation": "consume",
                                      "target_id": pick["entity_id"], "params": {"quantity": 1}}, None)
            event = emit(c, w, key + ":habit", "need.habit",
                         {"habit": "eat", "item": pick["entity_id"], "changes": changes}, aid)
            on_consume(c, w, aid, pick["type_id"], 1, key)
            actions.remember(c, w, aid, changes, event, "own_action_result")
        else:
            start_activity(c, w, aid, option, source="habit", key=key + ":habit")
        c.execute("RELEASE habit")
    except WorldError:
        c.execute("ROLLBACK TO habit")
        c.execute("RELEASE habit")


def on_consume(c, w, aid, type_id, quantity, key):
    food = (config(w) or {}).get("food")
    if not food:
        return
    rows = settle(c, w, aid)
    row = rows.get(food["need"])
    if row is None:
        return
    relief = food["relief"].get(type_id, food["relief"]["default"]) * quantity
    relief *= 1 + food.get("variation", 0) * (2 * _draw(w, key, "relief") - 1)
    row["value"] = max(0.0, row["value"] - relief)
    _reschedule(c, w, row)


def handle(c, w, task) -> bool:
    kind = task["kind"]
    if kind == "need":
        _need_event(c, w, task)
    elif kind == "routine":
        _routine(c, w, task)
    elif kind == "rhythm_day":
        _new_day(c, w, task)
    elif kind == "activity_end":
        actor = entity(c, w["world_id"], task["payload"]["actor_id"])
        if actor["state"].get("doing", {}).get("job") == task["payload"]["job"]:
            end_activity(c, w, actor["entity_id"])
    else:
        return False
    return True
