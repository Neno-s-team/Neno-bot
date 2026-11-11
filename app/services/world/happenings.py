"""世界事件（E2）：世界里在发生的事——讲座、展览、花期、店新开/关门、停水、地铁故障、双十一……

和扰动（perturbations）的区别：扰动是"落在某个人身上的意外"；这里是"世界自己在发生的事"，
不管她知不知道、去不去，它都按日历和种子发生。不调模型，可重放。

- 内容池：content/world/events/<pool>/*.json；世界作息配置里 `happenings.pool` 指定。
- 每天清晨排一次：把 HORIZON 天后那天会发生的事定下来（提前几天"公布"），突发的事（地铁故障）当天才定。
- 她怎么知道：群通知（group）、刷手机刷到（scroll）、路过看到（pass），都按概率——有的事她就是不知道。
- 知道以后：有具体时间的（讲座）按兴趣抽"打算去"，到点看状态——累了、病了、下雨可能就不去了；
  持续一阵的（展览、银杏）变成空档里多出来的一个选项。
- 对世界的影响（effects）：某处关门、某些事更想/更不想做（楼上装修→不想在家学）、地铁晚点、新店、常去的店关了。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from app.services.world import almanac, city, rhythms
from app.services.world.randomness import sample
from app.storage.world_store import emit, entity, job, save_entity

CONTENT = Path(__file__).resolve().parents[3] / "content" / "world" / "events"
HORIZON = 8          # 提前几天把事定下来
BACKFILL = 60        # 建世界时往回补多少天（已经在办的展览、在放的电影）
KEEP_DAYS = 2        # 结束后留几天再清掉


def config(w) -> dict | None:
    return (rhythms.config(w) or {}).get("happenings")


def enabled(w) -> bool:
    return bool(config(w))


@lru_cache(maxsize=4)
def load(pool: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for path in sorted((CONTENT / pool).glob("*.json")):
        for tpl in json.loads(path.read_text(encoding="utf-8"))["pool"]:
            result[tpl["id"]] = tpl
    return result


def templates(w) -> dict[str, dict]:
    return load(config(w)["pool"]) if enabled(w) else {}


def state(w) -> dict:
    return w["settings"].setdefault("happenings", {"items": {}, "known": {}, "plans": {}, "done": {}, "closed_venues": []})


def her(w) -> str:
    return config(w).get("actor", "neno")


def _draw(w, key: str, purpose: str, low: float = 0.0, high: float = 1.0) -> float:
    return low + (high - low) * sample(w["seed"], "happen:" + key, purpose)


def _midnight(w, d: date) -> float:
    offset = w["settings"].get("utc_offset_minutes", 0) * 60
    return (d - date(1970, 1, 1)).days * 86400 - offset


def _same_day(d: date, spec: str) -> bool:
    """spec 是 'MM-DD'（每年）或 'YYYY-MM-DD'（就那一天）。"""
    return d.isoformat() == spec if len(spec) == 10 else d.strftime("%m-%d") == spec


# ── 排事 ─────────────────────────────────────────────────────────────────────

def setup(c, w) -> None:
    """建世界 / 第一次启用：补上已经在发生的和未来 HORIZON 天内的事，并排好每天清晨的排事任务。"""
    if not enabled(w):
        return
    today = almanac.local_date(w, w["sim_time"])
    for tpl in templates(w).values():
        if tpl["schedule"]["kind"] == "season":
            for year in (today.year - 1, today.year):
                start = date.fromisoformat(f"{year}-{tpl['schedule']['from']}")
                if start <= today + timedelta(days=HORIZON):
                    _create(c, w, tpl, start)
    for i in range(-BACKFILL, HORIZON + 1):
        _plan_day(c, w, today + timedelta(days=i), sudden=(i == 0), normal=True)
    _schedule_daily(c, w, today + timedelta(days=1))
    # 她不是今天才搬来：已经公布的事，该看到的大多早看到了（不记"刚听说"，但有时间的仍会考虑去不去）
    aid = her(w)
    for hid, inst in sorted(state(w)["items"].items()):
        tpl = templates(w)[inst["tpl"]]
        if inst["announce"] >= w["sim_time"]:
            continue
        channels = tpl.get("channels", {})
        seen = tpl["scope"] == "nearby" and inst["start"] < w["sim_time"]
        for channel in ("group", "scroll"):
            if not seen and _draw(w, f"setup:{hid}", "learn:" + channel) < channels.get(channel, 0.0):
                seen = True
        if seen:
            know(c, w, aid, inst, "早就知道", "setup", quiet=True)


def _schedule_daily(c, w, d: date) -> None:
    job(c, w["world_id"], f"happen_plan:{d.isoformat()}", _midnight(w, d) + 4.5 * 3600, "happen_plan", {"date": d.isoformat()})


def _daily(c, w, task) -> None:
    today = date.fromisoformat(task["payload"]["date"])
    _plan_day(c, w, today + timedelta(days=HORIZON), sudden=False, normal=True)
    _plan_day(c, w, today, sudden=True, normal=False)
    st = state(w)
    for hid in [h for h, inst in st["items"].items() if inst["end"] < w["sim_time"] - KEEP_DAYS * 86400]:
        st["items"].pop(hid)
        for bucket in ("known", "plans", "done"):
            for per_actor in st[bucket].values():
                per_actor.pop(hid, None)
    _schedule_daily(c, w, today + timedelta(days=1))


def _plan_day(c, w, d: date, *, sudden: bool, normal: bool) -> None:
    info = almanac.on(w, d)
    for tpl in templates(w).values():
        if (tpl.get("sudden") and not sudden) or (not tpl.get("sudden") and not normal):
            continue
        if tpl.get("when") and not info["tags"] & set(tpl["when"]):
            continue
        sch = tpl["schedule"]
        if sch["kind"] == "random":
            days = sch.get("weekdays")
            if days and info["weekday"] not in days:
                continue
            if _draw(w, f"{tpl['id']}:{d.isoformat()}", "occur") >= sch["per_week"] / (len(days) if days else 7):
                continue
        elif sch["kind"] == "season":
            if not _same_day(d, sch["from"]):
                continue
        elif not any(_same_day(d, spec) for spec in sch["on"]):
            continue
        _create(c, w, tpl, d)


def _range(w, key: str, purpose: str, spec) -> float:
    """spec 是一个数，或 [低, 高]（区间里抽）。"""
    if isinstance(spec, (int, float)):
        return float(spec)
    return _draw(w, key, purpose, float(spec[0]), float(spec[1]))


def _pick(w, key: str, purpose: str, options: list):
    return options[int(_draw(w, key, purpose) * len(options))] if options else None


def _place(c, w, tpl, key: str) -> tuple[str | None, str] | None:
    """这件事在哪：(地点 id, 名字)。模板要地点但找不到合适的返回 None（这件事就不发生）。"""
    spec = tpl.get("place")
    if not spec:
        return (None, "")
    if "entity" in spec:
        row = c.execute("SELECT label FROM world_entities WHERE world_id=? AND entity_id=?", (w["world_id"], spec["entity"])).fetchone()
        return (spec["entity"], row[0]) if row else None
    if not city.enabled(w):
        return None
    data = city.data(w)
    closed = set(state(w)["closed_venues"])
    if "name" in spec:
        pool = [v for v in data["venues"] if v["name"] == spec["name"]]
    elif "kinds" in spec:
        pool = [v for v in data["venues"] if v["kind"] in spec["kinds"] and bool(v.get("town")) == bool(spec.get("town"))]
    else:
        from app.services.world import life
        memory = life._venue_memory(w, her(w))
        if "near_home" in spec:  # 家附近一家她还不知道的店
            pool = [v for v in city.nearby(w, "my_room", spec["near_home"], 1200, limit=200) if v["id"] not in memory]
        else:                    # 她常去的一家（去过 regular 次以上、印象不差）
            pool = [data["by_id"][vid] for vid, m in sorted(memory.items())
                    if m["visits"] >= spec["regular"] and m["like"] > 0 and vid in data["by_id"]]
    pool = [v for v in pool if v["id"] not in closed]
    venue = _pick(w, key, "place", pool)
    return (venue["id"], venue["name"]) if venue else None


def _create(c, w, tpl, d: date) -> dict | None:
    hid = f"{tpl['id']}:{d.isoformat()}"
    st = state(w)
    if hid in st["items"]:
        return st["items"][hid]
    sch = tpl["schedule"]
    begin = _midnight(w, d)
    if "start" in sch:      # 有具体钟点的一场
        hour = _pick(w, hid, "start", sch["start"]) if isinstance(sch["start"], list) else sch["start"]
        start = begin + hour * 3600
        end = start + _range(w, hid, "hours", sch["hours"]) * 3600
        multi = False
    elif sch["kind"] == "season":
        start = begin
        last = date.fromisoformat(f"{d.year}-{sch['to']}")
        if last < d:
            last = date.fromisoformat(f"{d.year + 1}-{sch['to']}")
        end = _midnight(w, last) + 86400
        multi = True
    else:                   # 持续几天
        start = begin
        end = start + round(_range(w, hid, "days", sch["days"])) * 86400
        multi = True
    if end <= w["sim_time"]:
        return None
    where = _place(c, w, tpl, hid)
    if where is None:
        return None
    recent = {i["title"] for i in st["items"].values() if i["tpl"] == tpl["id"] and i["end"] > start - 21 * 86400}
    for attempt in range(6):
        slots = {name: _pick(w, hid, f"slot:{name}:{attempt}", options) for name, options in tpl.get("slots", {}).items()}
        text = {"place": where[1], **slots}
        if tpl["title"].format(**text) not in recent:
            break
    else:
        return None
    lead = _range(w, hid, "lead", tpl.get("lead_days", [0, 0])) * 86400
    inst = {"id": hid, "tpl": tpl["id"], "scope": tpl["scope"], "title": tpl["title"].format(**text),
            "detail": tpl.get("detail", "").format(**text), "start": start, "end": end, "multi": multi,
            "announce": start if tpl.get("sudden") else start - lead, "place": where[0], "place_name": where[1],
            "slots": slots}
    effects = tpl.get("effects", {})
    if "metro_delay" in effects:
        inst["delay"] = round(_range(w, hid, "delay", effects["metro_delay"]))
    if effects.get("closes_venue") and where[0]:
        st["closed_venues"].append(where[0])
    if tpl.get("attend", {}).get("doing"):
        inst["doing"] = tpl["attend"]["doing"].format(**text)
    st["items"][hid] = inst
    emit(c, w, f"happen:{hid}", "world.happening", inst)
    return inst


# ── 此刻在发生什么（对世界的影响）──────────────────────────────────────────────

def _local_hour(w, ts: float) -> float:
    return ((ts + w["settings"].get("utc_offset_minutes", 0) * 60) % 86400) / 3600


def active(w, ts: float | None = None) -> list[tuple[dict, dict]]:
    """此刻正在发生的事：[(实例, 模板)]。"""
    if not enabled(w):
        return []
    ts = w["sim_time"] if ts is None else ts
    pool = templates(w)
    return [(inst, pool[inst["tpl"]]) for inst in state(w)["items"].values()
            if inst["start"] <= ts < inst["end"] and inst["tpl"] in pool]


def _in_effect_hours(w, effects: dict, ts: float) -> bool:
    if effects.get("hours"):
        low, high = effects["hours"]
        if not low <= _local_hour(w, ts) < high:
            return False
    return not effects.get("weekdays") or almanac.day(w, ts)["weekday"] in effects["weekdays"]


def boosts(w, aid: str) -> dict[str, float]:
    """正在发生的事让哪些选项更想/更不想做。身边的事（停水、装修）不管她知不知道都起作用；别的要她知道才算。"""
    result: dict[str, float] = {}
    known = state(w)["known"].get(aid, {}) if enabled(w) else {}
    for inst, tpl in active(w):
        effects = tpl.get("effects", {})
        if not effects.get("boost") or not _in_effect_hours(w, effects, w["sim_time"]):
            continue
        if tpl["scope"] != "nearby" and inst["id"] not in known:
            continue
        for choice, value in effects["boost"].items():
            result[choice] = result.get(choice, 0.0) + value
    return result


def closed(w, place_id: str) -> bool:
    if not enabled(w):
        return False
    if place_id in state(w)["closed_venues"]:
        return True
    return any(tpl.get("effects", {}).get("closes") == place_id for _, tpl in active(w))


def metro_delays(w) -> dict[str, tuple[int, str]]:
    """{线路: (晚点分钟, 事件 id)}。"""
    return {inst["slots"]["line"]: (inst["delay"], inst["id"]) for inst, tpl in active(w) if "delay" in inst}


def fresh_venues(w, aid: str) -> dict[str, float]:
    """她知道的新开的店：{店 id: 想去试试的加成}。"""
    known = state(w)["known"].get(aid, {}) if enabled(w) else {}
    return {inst["place"]: tpl["effects"]["fresh_venue"] for inst, tpl in active(w)
            if tpl.get("effects", {}).get("fresh_venue") and inst["id"] in known}


# ── 她怎么知道 ────────────────────────────────────────────────────────────────

_VIA = {"group": "群里", "scroll": "刷手机", "pass": "路过"}


def _passes(w, tpl, inst, place: str | None) -> bool:
    if place is None:
        return False
    if inst["place"] == place:
        return True
    zone = city.area(w, place) if city.enabled(w) else None
    if tpl.get("pass_area"):
        return zone == tpl["pass_area"]
    if tpl["scope"] == "campus":
        return zone == "campus"
    return inst["place"] is None and tpl["scope"] == "city" and zone in ("out", "campus")


def learn(c, w, aid: str, channel: str, key: str, *, place: str | None = None, scale: float = 1.0) -> list[dict]:
    """她有一次机会知道这些事（刷了会儿手机 / 看了眼群 / 路过）。按概率，有的就是没看到。"""
    if not enabled(w) or aid != her(w):
        return []
    st = state(w)
    known = st["known"].setdefault(aid, {})
    heard = []
    for hid, inst in sorted(st["items"].items()):
        tpl = templates(w).get(inst["tpl"])
        if tpl is None or hid in known or not inst["announce"] <= w["sim_time"] < inst["end"]:
            continue
        chance = tpl.get("channels", {}).get(channel, 0.0) * scale
        if channel == "pass" and not _passes(w, tpl, inst, place):
            continue
        if _draw(w, f"{key}:{hid}", "learn:" + channel) >= chance:
            continue
        know(c, w, aid, inst, tpl.get("via", {}).get(channel, _VIA[channel]), key)
        heard.append(inst)
    return heard


def know(c, w, aid: str, inst: dict, via: str, key: str, quiet: bool = False) -> None:
    known = state(w)["known"].setdefault(aid, {})
    if inst["id"] in known:
        return
    known[inst["id"]] = {"at": max(inst["announce"], w["sim_time"] - 86400) if quiet else w["sim_time"], "via": via}
    if not quiet:
        emit(c, w, f"{key}:heard:{inst['id']}", "life.heard",
             {"hid": inst["id"], "title": inst["title"], "via": via, "start": inst["start"], "multi": inst["multi"]}, aid)
    _consider(c, w, aid, inst, key)


# ── 打算去、到点去不去 ────────────────────────────────────────────────────────

_BUSY = {"sleep": "在睡觉", "class": "有课走不开", "meeting": "在开组会", "attend": "有别的事走不开"}


def _consider(c, w, aid: str, inst: dict, key: str) -> None:
    """知道了一场有具体时间的事：按兴趣抽要不要打算去。"""
    attend = templates(w)[inst["tpl"]].get("attend")
    if not attend or inst["multi"] or "interest" not in attend or inst["start"] < w["sim_time"] + 1200:
        return
    prefs = w["settings"].get("life_prefs", {}).get(aid, {})
    interest = max(0.03, min(0.95, attend["interest"] + 0.5 * prefs.get(attend.get("pref", ""), 0.0)))
    if _draw(w, f"{key}:{inst['id']}", "interest") >= interest:
        return
    state(w)["plans"].setdefault(aid, {})[inst["id"]] = {"at": inst["start"], "since": w["sim_time"]}
    job(c, w["world_id"], f"happen_go:{aid}:{inst['id']}:0", max(w["sim_time"] + 60, inst["start"] - 75 * 60), "happen_go",
        {"actor_id": aid, "hid": inst["id"], "stage": 0})
    emit(c, w, f"{key}:plan:{inst['id']}", "life.planned", {"hid": inst["id"], "title": inst["title"], "start": inst["start"]}, aid)


def _go(c, w, task) -> None:
    """快到点了：算好路程再出门；出门前看状态——在忙、太累、病了、下雨，可能就不去了。"""
    aid, hid, key = task["payload"]["actor_id"], task["payload"]["hid"], task["job_id"]
    st = state(w)
    inst, plan = st["items"].get(hid), st["plans"].get(aid, {}).get(hid)
    if not inst or not plan:
        return
    actor = entity(c, w["world_id"], aid)
    way = city.route(w, actor["parent_id"], inst["place"]) if city.enabled(w) and inst["place"] else {"minutes": 10.0}
    depart = inst["start"] - (way["minutes"] + 8) * 60
    if task["payload"]["stage"] == 0 and depart > w["sim_time"] + 60:
        job(c, w["world_id"], f"happen_go:{aid}:{hid}:1", depart, "happen_go", {"actor_id": aid, "hid": hid, "stage": 1})
        return
    st["plans"][aid].pop(hid, None)
    doing = (actor["state"].get("doing") or {}).get("activity")
    needs = {n: rhythms._value_at(r, w["sim_time"]) for n, r in rhythms._rows(c, w["world_id"], aid).items()}
    sky = entity(c, w["world_id"], "street")["state"] if c.execute(
        "SELECT 1 FROM world_entities WHERE world_id=? AND entity_id='street'", (w["world_id"],)).fetchone() else {}
    reasons = []  # (少多少想去, 说法)
    if needs.get("fatigue", 0) > 0.75:
        reasons.append((0.35, "太累了"))
    if actor["state"].get("conditions"):
        reasons.append((0.45, "不舒服"))
    if sky.get("weather") == "rain" and way["minutes"] > 10:
        reasons.append((0.25, "下雨懒得出门"))
    if sky.get("feels") is not None and sky["feels"] < 3 and way["minutes"] > 10:
        reasons.append((0.1, "太冷了"))
    will = 0.88 - sum(r[0] for r in reasons)
    if doing in _BUSY or not actor["state"].get("active", True):
        why = _BUSY.get(doing, "不在")
    elif _draw(w, key, "go") >= will:
        why = max(reasons)[1] if reasons else "临时不想去了"
    else:
        why = None
    if why:
        emit(c, w, key + ":skip", "life.skipped_plan", {"hid": hid, "title": inst["title"], "reason": why}, aid)
        return
    if doing:
        rhythms.end_activity(c, w, aid, reason="换去做别的")
    from app.services.world import life
    hours = max(0.3, (inst["end"] - (w["sim_time"] + way["minutes"] * 60)) / 3600)  # 到了一直待到结束
    life.go(c, w, aid, choice_for(w, inst), inst["place"], hours, key)


def attended(c, w, aid: str, hid: str, key: str) -> None:
    st = state(w)
    inst = st["items"].get(hid)
    if inst is None:
        return
    done = st["done"].setdefault(aid, {})
    done[hid] = done.get(hid, 0) + 1
    know(c, w, aid, inst, "去了才知道", key)
    emit(c, w, key + ":attended", "life.attended", {"hid": hid, "title": inst["title"], "times": done[hid]}, aid)


# ── 空档里多出来的选项（持续一阵的事）─────────────────────────────────────────

def choice_for(w, inst: dict) -> dict:
    attend = templates(w)[inst["tpl"]]["attend"]
    return {"id": "happen:" + inst["id"], "activity": attend["activity"], "place": inst["place"],
            "base": attend.get("base", 0.15), "pref": attend.get("pref"), "hours": attend.get("open", [0, 24]),
            "when": attend.get("when"), "outdoor": attend.get("outdoor"), "relieves": attend.get("relieves", {}),
            "cooldown_hours": 20, "happening": inst["id"], "label": inst.get("doing") or inst["title"],
            "cost": attend.get("cost"),
            **({"group": "outing"} if attend["activity"] == "outing" else {})}


def choices(w, aid: str) -> list[dict]:
    """她知道、正在进行、还没去够的持续性事件 → 候选。"""
    if not enabled(w) or aid != her(w):
        return []
    st = state(w)
    known, done = st["known"].get(aid, {}), st["done"].get(aid, {})
    result = []
    for inst, tpl in active(w):
        attend = tpl.get("attend")
        if attend and inst["multi"] and inst["place"] and inst["id"] in known and done.get(inst["id"], 0) < attend.get("times", 1):
            result.append(choice_for(w, inst))
    return result


def known_list(w, aid: str) -> list[dict]:
    """她知道的、还没过去的事（给她的脑子）。"""
    if not enabled(w):
        return []
    st = state(w)
    plans, done = st["plans"].get(aid, {}), st["done"].get(aid, {})
    result = []
    for hid, how in st["known"].get(aid, {}).items():
        inst = st["items"].get(hid)
        if inst and inst["end"] > w["sim_time"]:
            result.append({**inst, "heard_at": how["at"], "via": how["via"], "planned": hid in plans, "been": done.get(hid, 0)})
    return sorted(result, key=lambda x: (x["start"] > w["sim_time"], x["start"] if x["start"] > w["sim_time"] else -x["heard_at"]))


def handle(c, w, task) -> bool:
    if task["kind"] == "happen_plan":
        if enabled(w):
            _daily(c, w, task)
        return True
    if task["kind"] == "happen_go":
        if enabled(w):
            _go(c, w, task)
        return True
    return False
