"""W5 扰动与调节器：日常里冒出来的小事、偶尔的中事、罕见的大事。

- 每隔一段随机时间（interval_hours 区间内抽）出现一次"机会"；三档各自按概率抽签，命中的最高档发生一件。
- 选哪件按权重抽；权重随当前状态放大（熬过头更容易感冒、下雨更容易扭脚），不写死因果。
- 调节器只看近期"平静度"：近 window 小时内发生的事（按档位加权）少于目标 → 概率调高；
  太密 → 概率调低、冷却拉长。它不决定发生什么，只调频率与尺度。
- 每次机会（发没发生）都记 perturb.opportunity 事件，带抽签值，可重放可追溯。
- 关系类事件不在池里；关系锁在 W4 做（NPC 意图过滤）。
"""

from __future__ import annotations

import json

from app.services.world import actions, rhythms
from app.storage.world_store import decode_entity, emit, entity, job, save_entity

_TIERS = ("large", "medium", "small")


def config(w: dict) -> dict | None:
    return (rhythms.config(w) or {}).get("perturbations")


def setup(c, w) -> None:
    cfg = config(w)
    if not cfg:
        return
    _schedule_next(c, w, w["sim_time"], 0)


def _schedule_next(c, w, after: float, index: int) -> None:
    hours = rhythms._draw(w, f"perturb:{index}", "interval", *config(w)["interval_hours"])
    job(c, w["world_id"], f"perturb:{index + 1}", after + hours * 3600, "perturb", {"index": index + 1})


def _local_hour(w, t) -> float:
    return ((t + w["settings"]["utc_offset_minutes"] * 60) % 86400) / 3600


def regulator(c, w) -> float:
    cfg = config(w)["regulator"]
    since = w["sim_time"] - cfg["window_hours"] * 3600
    tiers = config(w)["tiers"]
    weight = 0.0
    for row in c.execute("SELECT payload FROM world_events WHERE world_id=? AND kind='perturb.happened' AND sim_time>=?",
                         (w["world_id"], since)):
        weight += tiers.get(json.loads(row[0]).get("tier"), {}).get("weight", 1)
    target = float(cfg["calm_target"])
    mult = target / max(weight, target / cfg["max_mult"])
    return max(cfg["min_mult"], min(cfg["max_mult"], mult))


# ── 候选与权重 ──────────────────────────────────────────────────────────────

def _actors(c, w):
    return [decode_entity(r) for r in c.execute(
        "SELECT * FROM world_entities WHERE world_id=? AND kind='actor' ORDER BY entity_id", (w["world_id"],))
        if json.loads(r["state"]).get("active", True)]


def _weather_near(c, w, actor):
    here = entity(c, w["world_id"], actions.room(c, w["world_id"], actor))
    if "weather" in here["state"]:
        return here["state"]["weather"]
    row = c.execute("SELECT state FROM world_entities WHERE world_id=? AND kind='place' AND state LIKE '%\"weather\"%' LIMIT 1",
                    (w["world_id"],)).fetchone()
    return json.loads(row[0]).get("weather") if row else None


def _boost(c, w, actor, rules) -> float:
    if actor is None:
        return 1.0
    mult = 1.0
    needs = rhythms._rows(c, w["world_id"], actor["entity_id"])
    conditions = [x["label"] for x in actor["state"].get("conditions", [])]
    for rule in rules or []:
        hit = False
        if "need" in rule and rule["need"] in needs:
            hit = rhythms._value_at(needs[rule["need"]], w["sim_time"]) >= rule["above"]
        elif "mod" in rule:
            hit = any(m["label"] == rule["mod"] and m["until"] > w["sim_time"]
                      for row in needs.values() for m in row["data"].get("mods", []))
        elif "weather" in rule:
            hit = _weather_near(c, w, actor) == rule["weather"]
        elif "doing" in rule:
            hit = (actor["state"].get("doing") or {}).get("activity") == rule["doing"]
        elif "condition" in rule:
            hit = any(rule["condition"] in label for label in conditions)
        if hit:
            mult *= rule["mult"]
    return mult


def _candidates(c, w, tier, cooldown_scale):
    cfg, state = config(w), w["settings"].setdefault("perturb_state", {"last": {}})
    result = []
    for entry in cfg["pool"]:
        if entry["tier"] != tier:
            continue
        if entry.get("local_hours"):
            low, high = entry["local_hours"]
            if not low <= _local_hour(w, w["sim_time"]) < high:
                continue
        if entry["target"].startswith("place:"):
            targets = [(entity(c, w["world_id"], entry["target"][6:]), None)]
        else:
            targets = [(a, a) for a in _actors(c, w)]
        for target, actor in targets:
            key = f"{entry['id']}:{target['entity_id']}"
            last = state["last"].get(key)
            if last is not None and w["sim_time"] - last < entry.get("cooldown_hours", 0) * 3600 * cooldown_scale:
                continue
            require = entry.get("require") or {}
            if "weather_not" in require and target["state"].get("weather") == require["weather_not"]:
                continue
            if "weather_is" in require and target["state"].get("weather") != require["weather_is"]:
                continue
            if entry.get("awake") and actor and (actor["state"].get("doing") or {}).get("activity") == "sleep":
                continue
            weight = entry.get("weight", 1) * _boost(c, w, actor, entry.get("boost"))
            if weight > 0:
                result.append((entry, target, weight))
    return result


# ── 一次机会 ────────────────────────────────────────────────────────────────

def opportunity(c, w, task) -> None:
    cfg = config(w)
    key = task["job_id"]
    mult = regulator(c, w)
    # 太密时冷却拉长（最多 2 倍），太平静时冷却不缩短：调节器只放慢不催促因果
    cooldown_scale = max(1.0, 1.0 / mult) if mult < 1 else 1.0
    draws = {tier: rhythms._draw(w, key, "tier:" + tier) for tier in _TIERS}
    payload = {"regulator": round(mult, 3), "draws": {t: round(v, 6) for t, v in draws.items()}, "happened": None}
    try:
        for tier in _TIERS:
            probability = min(0.95, cfg["tiers"][tier]["probability"] * mult)
            if draws[tier] >= probability:
                continue
            candidates = _candidates(c, w, tier, cooldown_scale)
            if not candidates:
                continue
            cursor = rhythms._draw(w, key, "pick:" + tier) * sum(x[2] for x in candidates)
            entry, target, _ = candidates[-1]
            for item in candidates:
                cursor -= item[2]
                if cursor < 0:
                    entry, target, _ = item
                    break
            c.execute("SAVEPOINT perturb")
            try:
                payload["happened"] = _happen(c, w, key, entry, target)
                c.execute("RELEASE perturb")
            except _Nothing:
                c.execute("ROLLBACK TO perturb")
                c.execute("RELEASE perturb")
                payload["fizzled"] = entry["id"]
            break
    finally:
        emit(c, w, key, "perturb.opportunity", payload)
        _schedule_next(c, w, task["run_at"], int(task["payload"]["index"]))


def _happen(c, w, key, entry, target) -> dict:
    wid = w["world_id"]
    changes, applied = [], []
    actor = target if target["kind"] == "actor" else None
    for n, effect in enumerate(entry["effects"]):
        draw = rhythms._draw(w, key, f"effect:{n}")
        kind = effect["type"]
        if kind == "weather":
            target["state"]["weather"] = effect["value"]
            save_entity(c, target)
            changes.append(target)
            applied.append({"weather": effect["value"]})
        elif kind == "credits":
            low, high = effect["range"]
            amount = int(round(low + (high - low) * draw))
            actor = entity(c, wid, actor["entity_id"])
            actor["state"]["credits"] = max(0, actor["state"].get("credits", 0) + amount)
            save_entity(c, actor)
            changes.append(actor)
            applied.append({"credits": amount})
        elif kind == "need_add":
            low, high = effect["range"]
            amount = low + (high - low) * draw
            rows = rhythms.settle(c, w, actor["entity_id"])
            row = rows.get(effect["need"])
            if row:
                row["value"] = max(0.0, min(1.0, row["value"] + amount))
                rhythms._reschedule(c, w, row)
                applied.append({"need": effect["need"], "add": round(amount, 3)})
        elif kind == "need_mod":
            low, high = effect["mult"]
            hours = _span(w, key, n, effect["hours"])
            rows = rhythms.settle(c, w, actor["entity_id"])
            row = rows.get(effect["need"])
            if row:
                row["data"].setdefault("mods", []).append({"mult": low + (high - low) * draw, "label": entry["label"],
                                                           "until": w["sim_time"] + hours * 3600})
                rhythms._reschedule(c, w, row)
                applied.append({"need": effect["need"], "mult": round(low + (high - low) * draw, 3), "hours": round(hours, 1)})
        elif kind == "condition":
            hours = _span(w, key, n, effect["hours"])
            actor = entity(c, wid, actor["entity_id"])
            conditions = [x for x in actor["state"].get("conditions", []) if x["until"] > w["sim_time"]]
            conditions.append({"label": effect["label"], "until": w["sim_time"] + hours * 3600})
            actor["state"]["conditions"] = conditions
            save_entity(c, actor)
            changes.append(actor)
            applied.append({"condition": effect["label"], "hours": round(hours, 1)})
            job(c, wid, f"{key}:recover:{n}", w["sim_time"] + hours * 3600, "perturb_recover",
                {"actor_id": actor["entity_id"], "label": effect["label"]})
        elif kind == "break_item":
            owned = [decode_entity(r) for r in c.execute(
                "SELECT * FROM world_entities WHERE world_id=? AND owner_id=? AND kind='item' ORDER BY entity_id",
                (wid, actor["entity_id"]))]
            # 按"多容易坏"加权抽：小家电常坏，电脑难得坏一次，手机不坏（权重 0）
            weights = effect.get("fragility", {})
            fragile = [(x, weights.get(x["entity_id"], effect.get("default_fragility", 1.0))) for x in owned
                       if all(cap in x["capabilities"] for cap in effect["capabilities"]) and not x["state"].get("broken")]
            fragile = [(x, f) for x, f in fragile if f > 0]
            if not fragile:
                raise _Nothing()
            cursor = draw * sum(f for _, f in fragile)
            item = fragile[-1][0]
            for x, f in fragile:
                cursor -= f
                if cursor < 0:
                    item = x
                    break
            item["state"].update(broken=True, on=False, broken_at=w["sim_time"])
            save_entity(c, item)
            changes.append(item)
            applied.append({"broke": item["entity_id"], "label": item.get("label") or item["entity_id"]})
    state = w["settings"].setdefault("perturb_state", {"last": {}})
    state["last"][f"{entry['id']}:{target['entity_id']}"] = w["sim_time"]
    happened = {"id": entry["id"], "tier": entry["tier"], "label": entry["label"], "target": target["entity_id"],
                "applied": applied}
    event = emit(c, w, key + ":happened", "perturb.happened", {**happened, "changes": changes},
                 actor["entity_id"] if actor else None)
    _witness(c, w, changes, event, actor["entity_id"] if actor else None)
    if actor:
        rhythms._nudge(c, w, actor["entity_id"])
    return happened


def _witness(c, w, changes, event_id, actor_id) -> None:
    """在场的人看见；当事人（如果是人）记住自己身上发生了什么。"""
    places = {actions.room(c, w["world_id"], value) for value in changes}
    seen = {actor_id} if actor_id else set()
    for place in places:
        seen.update(r[0] for r in c.execute(
            "SELECT entity_id FROM world_entities WHERE world_id=? AND kind='actor' AND parent_id=?", (w["world_id"], place)))
    for key in sorted(seen):
        actions.observe(c, w, key, event_id, "perturbation")
    if actor_id and changes:
        actions.remember(c, w, actor_id, changes, event_id, "happened_to_me")


class _Nothing(Exception):
    """候选事件在落地时发现无从发生（例如没东西可坏）。"""


def _span(w, key, n, hours_range) -> float:
    low, high = hours_range
    return low + (high - low) * rhythms._draw(w, key, f"hours:{n}")


def recover(c, w, task) -> None:
    p = task["payload"]
    actor = entity(c, w["world_id"], p["actor_id"])
    before = actor["state"].get("conditions", [])
    actor["state"]["conditions"] = [x for x in before if x["until"] > w["sim_time"] + 1]
    if not actor["state"]["conditions"]:
        actor["state"].pop("conditions")
    save_entity(c, actor)
    emit(c, w, task["job_id"], "perturb.recovered", {"label": p["label"], "changes": [actor]}, actor["entity_id"])


def handle(c, w, task) -> bool:
    if task["kind"] == "perturb":
        if config(w):
            opportunity(c, w, task)
        return True
    if task["kind"] == "perturb_recover":
        recover(c, w, task)
        return True
    return False
