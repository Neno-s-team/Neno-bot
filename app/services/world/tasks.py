"""手上的事（T1）：有期限、要花功夫的事——课前阅读、组会汇报、导师布置的活、期末论文、开题。不调模型，可重放。

- 来源在作息配置的 `tasks.sources`：每周的（weekly）、每隔几周的（every）、随机冒出来的（random）、定死日子的（dates）。
- 每件事：要花多少小时（effort）、已经做了多少、什么时候截止。她"看文献写论文"时功夫才会涨：
  开始学的时候挑一件来做（多半是最急的，也可能是她想做的），学完按时长 × 当时的效率记进度。
- 压力：按"剩下的活 ÷ 剩下的时间"算——时间还多就不急（会拖），快到期才急；急了更想学、没心思玩。
  每天还有个"今天就是不想碰"的抽签。这是加减分，不替她决定：拖到最后赶不完是会发生的。
- 截止前夜活还没干完，可能熬夜赶（推迟睡觉）。
- 到期：做完了没事；没做完——有的事就这么过去了（没读完去上课、汇报讲得磕巴），有的拖着成了欠账，导师会来问。
"""
from __future__ import annotations

from datetime import date, timedelta

from app.services.world import almanac, rhythms
from app.storage.world_store import emit, entity, job, save_entity

HORIZON = 2  # 提前两天把"哪天布置什么"定下来


def config(w) -> dict | None:
    return (rhythms.config(w) or {}).get("tasks")


def enabled(w, aid: str | None = None) -> bool:
    cfg = config(w)
    return bool(cfg) and (aid is None or aid == cfg.get("actor", "neno"))


def state(w) -> dict:
    return w["settings"].setdefault("tasks", {"items": {}})


def _draw(w, key: str, purpose: str, low: float = 0.0, high: float = 1.0) -> float:
    return rhythms._draw(w, "task:" + key, purpose, low, high)


def _midnight(w, d: date) -> float:
    return (d - date(1970, 1, 1)).days * 86400 - w["settings"].get("utc_offset_minutes", 0) * 60


def _clock(text: str) -> float:
    hours, minutes = text.split(":")
    return int(hours) + int(minutes) / 60


# ── 布置 ─────────────────────────────────────────────────────────────────────

def setup(c, w) -> None:
    if not enabled(w) or state(w).get("ready"):
        return
    today = almanac.local_date(w, w["sim_time"])
    for i in range(-21, HORIZON + 1):
        _plan_day(c, w, today + timedelta(days=i))
    for source in config(w)["sources"]:
        if source["kind"] == "dates":   # 早就布置下来的长线（期末论文、开题）
            for due in source["due"]:
                _maybe_dated(c, w, source, date.fromisoformat(due), today)
    job(c, w["world_id"], f"task_plan:{(today + timedelta(days=1)).isoformat()}",
        _midnight(w, today + timedelta(days=1)) + 5 * 3600, "task_plan", {"date": (today + timedelta(days=1)).isoformat()})
    state(w)["ready"] = True


def _daily(c, w, task) -> None:
    today = date.fromisoformat(task["payload"]["date"])
    _plan_day(c, w, today + timedelta(days=HORIZON))
    for source in config(w)["sources"]:
        if source["kind"] == "dates":
            for due in source["due"]:
                _maybe_dated(c, w, source, date.fromisoformat(due), today)
    items = state(w)["items"]
    for tid in [t for t, item in items.items() if item["status"] != "open" and item["closed_at"] < w["sim_time"] - 14 * 86400]:
        items.pop(tid)
    nxt = today + timedelta(days=1)
    job(c, w["world_id"], f"task_plan:{nxt.isoformat()}", _midnight(w, nxt) + 5 * 3600, "task_plan", {"date": nxt.isoformat()})


def _maybe_dated(c, w, source: dict, due: date, today: date) -> None:
    tid = f"{source['id']}:{due.isoformat()}"
    lead = round(_draw(w, tid, "lead", *source["assign_days_before"]))
    if due - timedelta(days=lead) <= today <= due:
        _assign(c, w, source, tid, max(_midnight(w, due - timedelta(days=lead)) + 10 * 3600, 0),
                _midnight(w, due) + _clock(source.get("due_time", "23:59")) * 3600)


def _plan_day(c, w, d: date) -> None:
    """到 d 这天为止该布置下来的事（按来源各自的规律；重复调用不会重复布置）。"""
    info = almanac.on(w, d)
    for source in config(w)["sources"]:
        if source["kind"] == "random":
            if source.get("when") and not info["tags"] & set(source["when"]):
                continue
            tid = f"{source['id']}:{d.isoformat()}"
            if _draw(w, tid, "occur") >= source["per_week"] / 7:
                continue
            due_day = d + timedelta(days=round(_draw(w, tid, "due", *source["due_days"])))
            _assign(c, w, source, tid, _midnight(w, d) + _draw(w, tid, "hour", *source.get("assign_hours", [9, 21])) * 3600,
                    _midnight(w, due_day) + _clock(source.get("due_time", "18:00")) * 3600)
        elif source["kind"] in ("weekly", "every"):
            for ahead in range(max(source["assign_days_before"]) + 1):
                due_day = d + timedelta(days=ahead)
                if due_day.isoweekday() != source["weekday"]:
                    continue
                if source["kind"] == "every" and (due_day - date(2026, 1, 5)).days // 7 % source["weeks"] != source.get("phase", 0):
                    continue
                if source.get("when") and not almanac.on(w, due_day)["tags"] & set(source["when"]):
                    continue
                tid = f"{source['id']}:{due_day.isoformat()}"
                given = due_day - timedelta(days=round(_draw(w, tid, "lead", *source["assign_days_before"])))
                if given <= d:
                    _assign(c, w, source, tid,
                            _midnight(w, given) + _draw(w, tid, "hour", *source.get("assign_hours", [9, 21])) * 3600,
                            _midnight(w, due_day) + _clock(source.get("due_time", "18:00")) * 3600)


def _assign(c, w, source: dict, tid: str, assigned: float, due: float) -> None:
    items = state(w)["items"]
    if tid in items or due <= w["sim_time"]:
        return
    slots = {name: options[int(_draw(w, tid, "slot:" + name) * len(options))] for name, options in source.get("slots", {}).items()}
    title = source["title"].format(**slots)
    if any(i["title"] == title and i["status"] == "open" for i in items.values()):
        return  # 同一件事手上已经有了
    item = {"id": tid, "src": source["id"], "title": title, "from": source.get("from", ""), "assigned": assigned,
            "due": due, "effort": round(_draw(w, tid, "effort", *source["effort"]), 1), "done": 0.0, "status": "open",
            "after_due": source.get("after_due", "close"), "npc": source.get("npc")}
    items[tid] = item
    if assigned > w["sim_time"]:
        job(c, w["world_id"], f"task_assign:{tid}", assigned, "task_assign", {"tid": tid})
    else:
        item["known"] = True   # 建世界时已经在手上的：早就知道
        # 已经过去的那些天她也不是一点没动：按过去的时间比例补一点进度（不超过七成）
        span = max(1.0, due - assigned)
        item["done"] = round(item["effort"] * min(0.7, _draw(w, tid, "head", 0.0, 1.0) * (w["sim_time"] - assigned) / span), 1)
    job(c, w["world_id"], f"task_due:{tid}", due, "task_due", {"tid": tid})


def _announce(c, w, task) -> None:
    item = state(w)["items"].get(task["payload"]["tid"])
    if not item or item.get("known"):
        return
    item["known"] = True
    aid = config(w).get("actor", "neno")
    emit(c, w, task["job_id"], "task.assigned", {"tid": item["id"], "title": item["title"], "from": item["from"],
                                                 "due": item["due"], "effort": item["effort"]}, aid)
    if item.get("npc"):
        _npc_says(c, w, item["npc"], f"assign:{item['id']}", w["sim_time"] + 60,
                  f"给她布置：{item['title']}，{_due_words(w, item['due'])}前要")


def _due_words(w, ts: float) -> str:
    d = almanac.local_date(w, ts)
    return f"{d.month}月{d.day}日（周{'一二三四五六日'[d.isoweekday() - 1]}）"


def _npc_says(c, w, npc_id: str, key: str, when: float, about: str) -> None:
    """让这个人（导师）来说这件事：排一次 NPC 联系，话由 NPC 的模型按 about 写。没开 NPC 就只有事件。"""
    if npc_id not in (w["settings"].get("npcs") or {}).get("npcs", {}):
        return
    job(c, w["world_id"], f"npc_contact:{npc_id}:task:{key}", when, "npc_contact",
        {"npc": npc_id, "channel": "message", "about": about[:80]})


# ── 做事 ─────────────────────────────────────────────────────────────────────

def open_items(w, aid: str | None = None) -> list[dict]:
    """她知道的、还没了结的事，急的在前。"""
    if not enabled(w, aid):
        return []
    return sorted((i for i in state(w)["items"].values() if i["status"] == "open" and i.get("known")),
                  key=lambda i: -pressure(w, i))


def remaining(item: dict) -> float:
    return max(0.0, item["effort"] - item["done"])


def pressure(w, item: dict) -> float:
    """这件事有多急（0–1.4）：剩下的活 ÷ 剩下能用的时间。时间还多就不急；已经过期的欠账最急。"""
    left = remaining(item)
    if left <= 0:
        return 0.0
    cfg = config(w)["pressure"]
    hours_left = (item["due"] - w["sim_time"]) / 3600
    if hours_left <= 0:
        return 1.2
    ratio = left / max(0.5, hours_left / 24 * cfg["hours_per_day"])
    value = max(0.0, min(1.0, (ratio - cfg["relaxed"]) / (1.0 - cfg["relaxed"])))
    if hours_left < 24:
        value = max(value, 0.5) + 0.4   # 明天就要了
    return value


def drive(w, aid: str) -> dict:
    """此刻手上的事给她的推力：多想学（study）、多没心思玩（leisure）。今天想不想碰是抽的。"""
    items = open_items(w, aid)
    if not items:
        return {"study": 0.0, "leisure": 0.0, "top": 0.0}
    values = [pressure(w, i) for i in items]
    top = values[0]
    total = min(1.5, top + 0.3 * sum(values[1:]))
    cfg = config(w)["pressure"]
    day = almanac.local_date(w, w["sim_time"]).isoformat()
    mood = _draw(w, f"mood:{aid}:{day}", "today", *cfg["day_mood"])   # 有的日子就是不想碰
    if top >= 0.9:
        mood = max(mood, 0.9)                                         # 火烧眉毛了，再不想也得弄
    return {"study": cfg["study_weight"] * total * mood, "leisure": cfg["leisure_weight"] * total * mood, "top": top}


def begin(c, w, aid: str, key: str) -> None:
    """坐下来学：挑一件来做（多半是最急的，也可能就想弄另一件），记在她正在做的事上。"""
    items = open_items(w, aid)
    if not items:
        return
    # 不急的时候，坐下来多半是在看自己的东西；越急越会去碰要交的那件
    if _draw(w, key, "task?") >= config(w)["pressure"]["idle_task_chance"] + pressure(w, items[0]):
        return
    pick = items[0]
    if len(items) > 1 and _draw(w, key, "which") >= config(w)["pressure"]["follow_urgent"]:
        pick = items[1 + int(_draw(w, key, "other") * (len(items) - 1))]
    actor = entity(c, w["world_id"], aid)
    doing = actor["state"].get("doing")
    if not doing:
        return
    doing["task"] = pick["id"]
    doing["what"] = "在弄：" + pick["title"]
    save_entity(c, actor)


def finish(c, w, aid: str, doing: dict) -> None:
    """学完一段：按时长 × 当时的效率记进度（累的时候效率低，也有就是不在状态的时候）。"""
    item = state(w)["items"].get(doing.get("task") or "") if enabled(w, aid) else None
    if not item or item["status"] != "open":
        return
    key = f"{doing['job']}:work"
    hours = max(0.0, (w["sim_time"] - doing["since"]) / 3600)
    fatigue = rhythms._value_at(rhythms._rows(c, w["world_id"], aid)["fatigue"], w["sim_time"]) if "fatigue" in rhythms._rows(
        c, w["world_id"], aid) else 0.0
    efficiency = _draw(w, key, "efficiency", *config(w)["pressure"]["efficiency"]) * (0.7 if fatigue > 0.75 else 1.0)
    gained = round(min(remaining(item), hours * efficiency), 2)
    item["done"] = round(item["done"] + gained, 2)
    emit(c, w, key, "task.progress", {"tid": item["id"], "title": item["title"], "hours": round(hours, 2), "gained": gained,
                                      "done": item["done"], "effort": item["effort"]}, aid)
    if remaining(item) <= 0.05:
        _close(c, w, aid, item, key, "done")


def _close(c, w, aid: str, item: dict, key: str, status: str) -> None:
    late = w["sim_time"] > item["due"]
    item.update(status=status, closed_at=w["sim_time"])
    if status == "done":
        rushed = not late and item["due"] - w["sim_time"] < 3 * 3600
        emit(c, w, key + ":done", "task.done", {"tid": item["id"], "title": item["title"], "late": late, "rushed": rushed,
                                                "late_days": round(max(0.0, w["sim_time"] - item["due"]) / 86400, 1)}, aid)
        if item.get("npc") and _draw(w, item["id"], "feedback") < config(w)["pressure"]["feedback_chance"]:
            about = (f"她的{item['title']}拖了几天才交，收到了，说一句下次早点" if late
                     else f"看了她交的{item['title']}，简单回一下（可以夸一句，也可以提一点意见）")
            _npc_says(c, w, item["npc"], f"done:{item['id']}", w["sim_time"] + _draw(w, item["id"], "reply", 1, 30) * 3600, about)


def _due(c, w, task) -> None:
    item = state(w)["items"].get(task["payload"]["tid"])
    aid = config(w).get("actor", "neno")
    if not item or item["status"] != "open" or remaining(item) <= 0.05:
        return
    share = item["done"] / item["effort"]
    if item["after_due"] == "overdue":
        item["overdue"] = True      # 欠着：还得做，导师会来问
        emit(c, w, task["job_id"], "task.missed", {"tid": item["id"], "title": item["title"], "share": round(share, 2),
                                                   "outcome": "overdue"}, aid)
        if item.get("npc"):
            _npc_says(c, w, item["npc"], f"chase:{item['id']}", w["sim_time"] + _draw(w, item["id"], "chase", 2, 20) * 3600,
                      f"问她{item['title']}怎么还没交")
        return
    outcome = "scraped" if share >= config(w)["pressure"]["scrape_share"] else "missed"   # 勉强应付过去 / 没弄完
    emit(c, w, task["job_id"], "task.missed", {"tid": item["id"], "title": item["title"], "share": round(share, 2),
                                               "outcome": outcome}, aid)
    _close(c, w, aid, item, task["job_id"], outcome)


def crunch(c, w, aid: str, key: str) -> float:
    """该睡了，可明天要交的还没弄完：可能熬夜赶（返回推迟睡觉几小时；不熬返回 0）。"""
    if not enabled(w, aid):
        return 0.0
    cfg = config(w)["pressure"]
    urgent = [i for i in open_items(w, aid) if 0 < i["due"] - w["sim_time"] < cfg["crunch_within_hours"] * 3600
              and remaining(i) >= 1.0]
    if not urgent or _draw(w, key, "crunch") >= cfg["crunch_chance"]:
        return 0.0
    hours = min(cfg["crunch_max_hours"], max(1.0, remaining(urgent[0]) * 0.8))
    emit(c, w, key + ":crunch", "task.crunch", {"tid": urgent[0]["id"], "title": urgent[0]["title"], "hours": round(hours, 1)}, aid)
    return hours


# ── 给她的脑子 ────────────────────────────────────────────────────────────────

def lines(w, aid: str, limit: int = 5) -> list[str]:
    """手上的事，急的在前："周五 18:00 前：改开题报告第二章（导师；做了一半）"。时间说法由调用方给（when_text）。"""
    result = []
    for item in open_items(w, aid)[:limit]:
        result.append({"title": item["title"], "from": item["from"], "due": item["due"], "word": progress_word(item),
                       "overdue": bool(item.get("overdue")) or item["due"] < w["sim_time"]})
    return result


def progress_word(item: dict) -> str:
    share = item["done"] / item["effort"] if item["effort"] else 1.0
    return "还没动" if share < 0.08 else "开了个头" if share < 0.35 else "做了一半" if share < 0.7 else "快好了"


def handle(c, w, task) -> bool:
    kind = task["kind"]
    if kind not in ("task_plan", "task_assign", "task_due"):
        return False
    if enabled(w):
        {"task_plan": _daily, "task_assign": _announce, "task_due": _due}[kind](c, w, task)
    return True
