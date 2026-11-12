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
from app.storage.db import fetch_all, fetch_one
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
    "attend": (1, "包里", True),     # 听讲座、开题会：偶尔瞄一眼
    "watch": (3, "手边", False),
    "chores": (3, "兜里", False),
    "exercise": (2, "放在一边", False),
    "shower": (1, "洗手台上", False),
    "outing": (3, "手里", False),
    "shop": (3, "兜里", False),
    "drink": (3, "桌上", False),
    "fix": (3, "身边", False),
}
_IDLE = (3, "身边", False)
_LABELS = {"sleep": "睡觉", "meeting": "组会", "class": "上课", "commute": "在路上", "study": "看文献写论文",
           "read": "看闲书", "eat": "吃东西", "cook": "做饭", "call": "和爸妈打电话", "rest": "歇会儿", "scroll": "刷手机",
           "stroll": "散步", "chat": "跟人聊天", "fix": "折腾坏了的东西",
           "drink": "喝奶茶/咖啡", "shop": "买东西", "outing": "出去逛逛", "attend": "听讲座/参加活动",
           "watch": "看剧", "chores": "收拾屋子", "exercise": "运动", "shower": "洗澡"}


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
    if doing.get("activity") == "commute" and (doing.get("after") or {}).get("place"):
        # 路上：怎么去、去哪（"坐2号线转1号线（三坝→龙翔桥）去湖滨银泰"）
        seg["activity"] = f"在路上，{doing.get('route') or ''}去{_place_label(doing['after']['place'])}"
    elif doing.get("what"):
        seg["activity"] = doing["what"]  # 在参加世界里的某件事："在听讲座《…》""在北山街看银杏"
    conditions = [c["label"] for c in actor["state"].get("conditions", []) if c.get("until", 0) > (ts or clock.now())]
    if conditions:
        seg["body"] = "，".join(conditions)
    broken = _broken_items()
    if broken:
        seg["broken"] = "、".join(broken)
    w = _world()
    if w is not None:
        from app.services.world import city
        try:
            seg["home"] = city.home_line(w)
        except Exception:
            pass
        try:
            seg.update(_around(w))
        except Exception:
            pass
    contacts = [x for x in actor["state"].get("recent_contacts", []) if (ts or clock.now()) - x["at"] < 12 * 3600]
    if contacts:
        from app.services.mind import timeline
        seg["contacts"] = [f"{timeline.ago(x['at'])}，" + (x["note"] or f"{x['from']}{'打电话' if x['channel'] == 'call' else '发消息'}来过")
                           for x in contacts[-3:]]
    thought = actor["state"].get("last_thought")
    if thought and (ts or clock.now()) - thought["at"] < 3 * 3600:
        from app.services.mind import timeline
        seg["last_thought"] = f"{timeline.ago(thought['at'])}心里嘀咕过：{thought['text']}"
    return seg


def _around(w: dict[str, Any]) -> dict[str, Any]:
    """今天是什么日子、外面什么天、她知道最近有什么事。"""
    from app.services.mind import timeline
    from app.services.world import almanac, happenings, weather
    now = w["sim_time"]
    result: dict[str, Any] = {"day_note": almanac.note(w, now)}
    street = fetch_one("SELECT state FROM world_entities WHERE world_id = ? AND entity_id = 'street'", (config.MIND_WORLD_ID,))
    if street:
        sky = weather.line(json.loads(street["state"]))
        if sky:
            result["weather"] = sky
    try:
        from app.services.world import money
        from app.storage.db import get_conn
        with get_conn() as conn:
            wallet = money.line(conn, w, config.MIND_WORLD_ACTOR)
        if wallet:
            result["money"] = wallet
    except Exception:
        pass
    try:
        from app.services.world import tasks
        todo = []
        for item in tasks.lines(w, config.MIND_WORLD_ACTOR):
            if item["overdue"]:
                when = f"已经过了期限（原定{timeline.day_word(item['due'], now)}）"
            else:
                when = f"{timeline.day_word(item['due'], now)} {clock_text(item['due'])} 前"
            todo.append(f"{when}：{item['title']}（{item['from']}；{item['word']}）")
        if todo:
            result["tasks"] = todo
    except Exception:
        pass
    try:
        from app.services.world import moments
        from app.storage.db import get_conn
        with get_conn() as conn:
            small = moments.recent(conn, w, config.MIND_WORLD_ACTOR)
        if small:
            result["moments"] = [f"{timeline.ago(m['at'], now)}：{m['text']}" for m in small]
    except Exception:
        pass
    lines = []
    for item in happenings.known_list(w, config.MIND_WORLD_ACTOR)[:6]:
        if item["start"] > now:
            when = f"{timeline.day_word(item['start'], now)} {clock_text(item['start'])}"
        elif item["multi"]:
            last = item["end"] - 1
            when = "这阵子" + (f"（到{timeline.day_word(last, now)}）" if 86400 < last - now < 30 * 86400 else "")
        else:
            when = "现在"
        tail = "；".join(x for x in (item["detail"], "打算去" if item["planned"] else "", "去过了" if item["been"] else "",
                                    f"讲的是：{item['plot']}" if item["been"] and item.get("plot") else "") if x)
        lines.append(f"{when}：{item['title']}" + (f"（{tail}）" if tail else ""))
    if lines:
        result["happenings"] = lines
    return result


def clock_text(ts: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts, clock.local_now().tzinfo).strftime("%H:%M")


def _broken_items() -> list[str]:
    try:
        rows = fetch_all("SELECT label, state FROM world_entities WHERE world_id = ? AND owner_id = ? AND kind = 'item'",
                         (config.MIND_WORLD_ID, config.MIND_WORLD_ACTOR))
    except Exception:
        return []
    return [r["label"] for r in rows if json.loads(r["state"]).get("broken")]


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


# 世界事件 → "她的一天"页上的类别：go 去了哪 / do 干了什么 / phone 刷到的、听说的 / people 谁找过她 / thing 遇到的事
_GROUPS = {"activity.started": "do", "life.doing": "do", "routine.skipped": "do", "task.crunch": "do", "task.done": "do",
           "task.missed": "do", "task.assigned": "people", "npc.contacted": "people", "life.visit": "go", "life.attended": "go",
           "life.planned": "go", "life.skipped_plan": "go", "life.heard": "phone"}


def day_events(start: float, end: float, limit: int = 60) -> list[str]:
    """[start, end) 内她在世界里过的日子，压成几十行"几点 做了什么 / 谁找过她 / 出了什么事"，给夜间反思回想。"""
    items, tail = day_items(start, end, limit)
    return [f"{item['time']} {item['text']}" for item in items] + tail


def day_items(start: float, end: float, limit: int = 60) -> tuple[list[dict[str, Any]], list[str]]:
    """同一份日子，一条条带时刻和类别（给"她的一天"页）：([{at, time, group, text}], [这天的小结几句])。"""
    if not enabled():
        return [], []
    from datetime import datetime
    try:
        rows = [dict(r) for r in _fetch_events(start, end)]
    except Exception:
        return [], []
    tz = clock.local_now().tzinfo
    lines, last, spent, worked = [], None, 0.0, {}
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
            broke = [x["label"] for x in p.get("applied", []) if x.get("label") and x.get("broke")]
            text = f"{broke[0]}坏了" if broke else p.get("label")
        elif kind == "life.visit":
            text = f"去了{p.get('name')}" + ("（第一次去）" if p.get("visits") == 1 else "")
        elif kind == "life.heard":
            text = f"听说{p.get('title')}（{p.get('via')}）"
        elif kind == "life.planned":
            text = f"打算去{p.get('title')}"
        elif kind == "life.skipped_plan":
            text = f"本来要去{p.get('title')}，{p.get('reason')}，没去"
        elif kind == "life.attended":
            text = (f"去了{p.get('title')}" + (f"（{p['plot']}）" if p.get("plot") else "")) if p.get("times", 1) == 1 else None
        elif kind == "life.doing":
            text = p.get("what")
        elif kind == "weather.changed":
            text = f"天气：{p.get('to')}" + (f"，{round(p['temp'])}°C" if p.get("temp") is not None else "")
        elif kind == "life.moment":
            text = p.get("text")
        elif kind == "task.assigned":
            text = f"{p.get('from')}布置了：{p.get('title')}"
        elif kind == "task.progress":
            worked[p.get("title")] = worked.get(p.get("title"), 0.0) + float(p.get("hours", 0))
        elif kind == "task.done":
            text = f"{p.get('title')}弄完交了" + ("（拖了几天）" if p.get("late") else "（赶在最后）" if p.get("rushed") else "")
        elif kind == "task.missed":
            text = {"overdue": f"{p.get('title')}到期了还没交", "scraped": f"{p.get('title')}没弄完，勉强应付过去了",
                    "missed": f"{p.get('title')}没弄完就到时间了"}.get(p.get("outcome"))
        elif kind == "task.crunch":
            text = f"熬夜赶{p.get('title')}"
        elif kind == "money.income":
            text = f"{p.get('label')}：¥{round(p.get('amount', 0))}"
        elif kind == "money.bill":
            text = f"交了{p.get('what')} ¥{round(p.get('amount', 0))}"
        elif kind == "money.spent":
            spent += float(p.get("amount", 0))
        elif kind == "life.fixed":
            text = f"把{p.get('label')}修好了"
        elif kind == "life.fix_failed":
            text = f"折腾了一阵{p.get('label')}，没修好"
        elif kind == "need.consequence":
            text = p.get("label")
        elif kind == "routine.skipped":
            text = f"没去{_LABELS.get(p.get('activity'), p.get('activity'))}"
        if text and text != last:
            group = "phone" if kind == "life.moment" and p.get("kind") == "surf" else _GROUPS.get(kind, "thing")
            lines.append({"at": row["sim_time"], "time": at, "group": group, "text": text})
            last = text
    lines = lines[-limit:]
    tail = []
    for title, hours in worked.items():
        if hours >= 0.3:
            tail.append(f"这天在「{title}」上花了 {round(hours, 1)} 小时")
    if spent >= 1:
        tail.append(f"这天一共花了 ¥{round(spent)}")
    return lines, tail


def _fetch_events(start: float, end: float):
    from app.storage.db import fetch_all
    return fetch_all(
        """SELECT kind, actor_id, payload, sim_time FROM world_events
           WHERE world_id = ? AND sim_time >= ? AND sim_time < ?
             AND (actor_id = ? OR (kind IN ('perturb.happened', 'weather.changed') AND actor_id IS NULL))
           ORDER BY id""",
        (config.MIND_WORLD_ID, start, end, config.MIND_WORLD_ACTOR))
