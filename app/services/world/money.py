"""钱（M1）：每月到账、固定开销、每笔花销；手头松紧影响她挑什么，但不替她决定。不调模型，可重放。

- 配置在作息文件的 `money`：收入（哪天到、多少）、账单、几样固定价格、车费、家里囤的吃的、松紧的标准。
- 余额记在角色 state.credits（单位：元）。每笔收支记事件 money.income / money.spent / money.bill，
  并按天汇总进 settings.money.ledger（留 62 天）。
- 松紧：离下次到账还有几天、扣掉这期间要交的账单后，平均每天能花多少。越紧，贵的事越不想做
  （贵的店、奶茶、进城），在家吃和食堂相对变香——是加减分，不是禁止；余额不够才是真的做不成。
- 家里的吃的不再吃不完：买菜才有，吃一顿少一份，没了就得买菜或出去吃。
- 真见底了（余额很低、离到账还远）有概率跟家里开口，妈妈转一点——有冷却，不是每次都有。
"""
from __future__ import annotations

from datetime import date, timedelta

from app.services.world import almanac, rhythms
from app.storage.world_store import WorldError, emit, entity, job, save_entity

LEVELS = ("宽裕", "还行", "有点紧", "很紧")


def config(w) -> dict | None:
    return (rhythms.config(w) or {}).get("money")


def enabled(w, aid: str | None = None) -> bool:
    cfg = config(w)
    return bool(cfg) and bool(w["settings"].get("money", {}).get("ready")) and (aid is None or aid == cfg.get("actor", "neno"))


def state(w) -> dict:
    return w["settings"].setdefault("money", {"ledger": {}})


def _draw(w, key: str, purpose: str, low: float = 0.0, high: float = 1.0) -> float:
    return rhythms._draw(w, "money:" + key, purpose, low, high)


def _midnight(w, d: date) -> float:
    return (d - date(1970, 1, 1)).days * 86400 - w["settings"].get("utc_offset_minutes", 0) * 60


# ── 到账与账单 ────────────────────────────────────────────────────────────────

def _due_date(w, entry: dict, year: int, month: int) -> date:
    jitter = entry.get("jitter_days", 0)
    shift = round(_draw(w, f"{entry['id']}:{year}-{month:02d}", "day", -jitter, jitter)) if jitter else 0
    return date(year, month, min(entry["day"], 28)) + timedelta(days=shift)


def _next_due(w, entry: dict, after: float) -> tuple[date, float]:
    """这一项下一次是哪天、几点（本地 9–20 点之间抽一个钟点）。"""
    today = almanac.local_date(w, after)
    year, month = today.year, today.month
    for _ in range(3):
        d = _due_date(w, entry, year, month)
        when = _midnight(w, d) + _draw(w, f"{entry['id']}:{d.isoformat()}", "hour", 9, 20) * 3600
        if when > after:
            return d, when
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    raise WorldError("money schedule failed")


def _entries(w) -> list[tuple[str, dict]]:
    cfg = config(w)
    return [("income", e) for e in cfg.get("income", [])] + [("bill", e) for e in cfg.get("bills", [])]


def _schedule(c, w, kind: str, entry: dict, after: float) -> None:
    d, when = _next_due(w, entry, after)
    job(c, w["world_id"], f"money:{entry['id']}:{d.isoformat()}", when, "money_due", {"kind": kind, "id": entry["id"]})


def setup(c, w) -> None:
    """建世界 / 第一次启用：定初始余额、把价格换成元、定家里囤了几顿、排好到账和账单。"""
    cfg = config(w)
    if not cfg or state(w).get("ready"):
        return
    aid = cfg.get("actor", "neno")
    actor = entity(c, w["world_id"], aid)
    actor["state"]["credits"] = round(_draw(w, "setup", "balance", *cfg["initial"]))
    save_entity(c, actor)
    for item_id, price in cfg.get("prices", {}).items():
        row = c.execute("SELECT 1 FROM world_entities WHERE world_id=? AND entity_id=?", (w["world_id"], item_id)).fetchone()
        if row:
            item = entity(c, w["world_id"], item_id)
            item["state"]["price_credits"] = price
            save_entity(c, item)
    pantry = cfg.get("pantry")
    if pantry:
        food = entity(c, w["world_id"], pantry["item"])
        food["state"]["quantity"] = round(_draw(w, "setup", "pantry", *pantry["initial"]))
        save_entity(c, food)
    for kind, entry in _entries(w):
        _schedule(c, w, kind, entry, w["sim_time"])
    state(w)["ready"] = True


def _due(c, w, task) -> None:
    cfg = config(w)
    kind, eid = task["payload"]["kind"], task["payload"]["id"]
    entry = next((e for k, e in _entries(w) if k == kind and e["id"] == eid), None)
    if entry is None:
        return
    aid = cfg.get("actor", "neno")
    amount = round(_draw(w, task["job_id"], "amount", *entry["amount"]))
    if kind == "income":
        earn(c, w, aid, amount, entry["label"], task["job_id"])
    else:
        spend(c, w, aid, amount, entry["label"], "bill", task["job_id"], force=True, exact=True)
    _schedule(c, w, kind, entry, task["run_at"] + 86400)


# ── 收支 ─────────────────────────────────────────────────────────────────────

def balance(c, w, aid: str) -> float:
    return float(entity(c, w["world_id"], aid)["state"].get("credits", 0))


def _log(w, field: str, amount: float, category: str | None = None) -> None:
    ledger = state(w).setdefault("ledger", {})
    day = ledger.setdefault(almanac.local_date(w, w["sim_time"]).isoformat(), {"spent": 0, "income": 0, "cats": {}})
    day[field] = round(day[field] + amount, 1)
    if category:
        day["cats"][category] = round(day["cats"].get(category, 0) + amount, 1)
    for old in sorted(ledger)[:-62]:
        ledger.pop(old)


def earn(c, w, aid: str, amount: float, label: str, key: str) -> None:
    actor = entity(c, w["world_id"], aid)
    actor["state"]["credits"] = round(actor["state"].get("credits", 0) + amount, 1)
    save_entity(c, actor)
    _log(w, "income", amount)
    emit(c, w, key + ":income", "money.income", {"amount": amount, "label": label, "balance": actor["state"]["credits"]}, aid)


def spend(c, w, aid: str, amount: float, what: str, category: str, key: str, *, force: bool = False,
          exact: bool = False) -> float:
    """花一笔。价钱每次略有出入（点的不一样）；钱不够就做不成（force：账单、车费，扣到 0 为止）。返回实际花的。"""
    if not enabled(w, aid) or amount <= 0:
        return 0.0
    if not exact:
        spread = config(w).get("price_variation", 0.0)
        amount = round(amount * (1 + spread * (2 * _draw(w, key, "price") - 1)), 1 if amount < 10 else 0)
    actor = entity(c, w["world_id"], aid)
    have = float(actor["state"].get("credits", 0))
    if have < amount:
        if not force:
            raise WorldError("Not enough money", 409)
        amount = have
    actor["state"]["credits"] = round(have - amount, 1)
    save_entity(c, actor)
    _log(w, "spent", amount, category)
    emit(c, w, f"{key}:spent:{category}", "money.bill" if category == "bill" else "money.spent",
         {"amount": amount, "what": what, "category": category, "balance": actor["state"]["credits"]}, aid)
    return amount


def fare(w, way: dict | None) -> float:
    """这一趟的车费：单车一块五，地铁按远近。走路不花钱。"""
    cfg = config(w) or {}
    if not way or way.get("mode") not in ("bike", "metro"):
        return 0.0
    rates = cfg.get("fare", {})
    if way["mode"] == "bike":
        return float(rates.get("bike", 1.5))
    low, high = rates.get("metro", [2, 8])
    return float(min(high, low + int(way["minutes"] // 9)))


# ── 松紧 ─────────────────────────────────────────────────────────────────────

def feel(c, w, aid: str) -> dict | None:
    """手头怎么样：余额、离下次到账几天、这期间平均每天能花多少、松紧（0 宽裕 … 3 很紧）和 0–1.3 的紧度。"""
    if not enabled(w, aid):
        return None
    cfg = config(w)
    have = balance(c, w, aid)
    now = w["sim_time"]
    incomes = [_next_due(w, e, now) for e in cfg.get("income", [])]
    if not incomes:
        return None
    next_day, next_at = min(incomes, key=lambda x: x[1])
    days = max(1.0, (next_at - now) / 86400)
    bills = sum(sum(e["amount"]) / 2 for e in cfg.get("bills", []) if _next_due(w, e, now)[1] < next_at)
    per_day = (have - bills) / max(days, 3.0)  # 明天就到账也不会把剩下的一天花光：至少按三天匀
    ok, low = cfg["tight"]["comfortable_per_day"], cfg["tight"]["tight_per_day"]
    t = max(0.0, min(1.3, (ok - per_day) / (ok - low)))
    level = 0 if per_day >= 2 * ok else 1 if t < 0.35 else 2 if t < 1.0 else 3
    return {"balance": have, "days": days, "next": next_day.isoformat(), "per_day": per_day, "t": t, "level": level,
            "word": LEVELS[level]}


def line(c, w, aid: str) -> str:
    """给她的脑子：手头怎么样（"¥820，离下次到账还有 9 天，有点紧；家里吃的快没了"）。"""
    info = feel(c, w, aid)
    if not info:
        return ""
    if info["level"] == 0:
        text = "宽裕，不用算着花"      # 不缺钱的时候她不会惦记余额和哪天到账
    else:
        days = round(info["days"])
        when = "明天到账" if days <= 1 else f"离下次到账还有 {days} 天"
        text = f"¥{round(info['balance'])}，{when}，{info['word']}"
    stock = pantry(c, w)
    if stock is not None and stock <= config(w)["pantry"]["low"]:
        text += "；家里吃的" + ("没了" if stock < 1 else "快没了")
    return text


def pinch(state_: dict | None, cost: float) -> float:
    """这件事要花 cost：手头越紧越舍不得（加减分）。刚到账手头宽裕时，花点小钱反而痛快。"""
    if not state_ or cost <= 0:
        return 0.0
    if state_["level"] == 0:
        return 0.08 if cost <= 40 else 0.0
    return -state_["t"] * min(1.2, cost / 45)


def pantry(c, w) -> int | None:
    spec = (config(w) or {}).get("pantry")
    if not spec or not state(w).get("ready"):
        return None
    return int(entity(c, w["world_id"], spec["item"])["state"].get("quantity", 0))


def restock(c, w, key: str) -> int:
    """买了菜：家里多出几顿的量。"""
    spec = config(w)["pantry"]
    food = entity(c, w["world_id"], spec["item"])
    added = round(_draw(w, key, "restock", *spec["per_trip"]))
    food["state"]["quantity"] = int(food["state"].get("quantity", 0)) + added
    save_entity(c, food)
    return added


def ask_family(c, w, aid: str, key: str) -> bool:
    """真见底了：有概率跟家里开口。有冷却；开了口妈妈转一点。"""
    cfg = config(w)
    rule = cfg.get("ask_family")
    info = feel(c, w, aid)
    if not rule or not info or info["balance"] >= rule["below"] or info["days"] < rule["min_days_left"]:
        return False
    st = state(w)
    if w["sim_time"] - st.get("asked_at", -1e12) < rule["cooldown_days"] * 86400:
        return False
    if _draw(w, key, "ask") >= rule["chance"]:
        return False
    st["asked_at"] = w["sim_time"]
    amount = round(_draw(w, key, "gift", *rule["amount"]) / 50) * 50
    earn(c, w, aid, amount, rule["label"], key)
    return True


def handle(c, w, task) -> bool:
    if task["kind"] != "money_due":
        return False
    if config(w):
        _due(c, w, task)
    return True
