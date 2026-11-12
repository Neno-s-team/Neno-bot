"""主动找他：念头从日子里来，自己冒出来；时机合适、想清楚了才发。

念头（mind_proactive 一行一个）的来处，都是已有的数据：
- moment  世界里今天遇到的、想跟人说的事（recent_moments 里 share 够高的；刷到的东西也在里面）
- concern 心里惦记的事（mind_concern）
- intent  之前就想着要说/要问的（睡前想的"明天……"里，对不上世界活动的那些）
- silence 好久没说话了
- nudge   她主动发了、他一直没回

登记时按种子抽两次：想不想说（没抽中记"没那么想说"，再不提）、过多久冒出来。之后不再反复掷骰子，
所以一个念头的结局是定的，不会"每分钟问一次、迟早会发"。
到点后看时机：她得醒着、手机在手边（打断等级 ≥ 2 且没静音）；两人没在聊。
没有每日上限、没有安静时段；频率是动态的——她发了他没回的越多，再发的劲越小
（STILL：一条没回时再发的概率 0.85，之后 0.45、0.15、0.05）。他一回，这个数清零。
最后走一遍深思 + 措辞（cycle.run_cycle 的 initiative 入口），她可以决定"算了"。

MIND_PROACTIVE：off 不动；shadow 全流程跑但不发，只记"本来想说什么"；on 排进投递表，网关照常发。
这一层不碰 TDLib：投递、已发确认、被新消息打断作废，都复用现有那套。
MIND_PROACTIVE_FUSE 是保险丝（一天最多想多少回），防程序出错刷屏烧钱，不是行为上限。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime
from typing import Any

from app import config
from app.services.mind import clock, concerns, intents, relationship, schedule, store, timeline, world_link
from app.storage.db import fetch_all, fetch_one, get_conn, list_messages
from app.utils.logging_utils import log_event

SHARE_FLOOR = 0.5                      # 世界里的小事：share 到这个数才算"想跟人说"
TTL_HOURS = {"moment": 4.0, "concern": 10.0, "intent": 12.0, "silence": 10.0, "nudge": 6.0}
STILL = [1.0, 0.85, 0.45, 0.15, 0.05]  # 她发了没回的条数 → 再发的概率
ABOUT_HIM = re.compile(r"他|聊|问问|告诉")   # 打算里跟他有关的（"问问他面试怎么样"），不是她自己的杂事
SILENCE_HOURS = 18.0                   # 这么久没说话才会"想起他"
LEAD = {"moment": "刚遇到的事，想跟他说：", "concern": "心里惦记着：", "intent": "之前就想着：",
        "silence": "", "nudge": ""}
_last_tick = 0.0


def mode() -> str:
    value = str(getattr(config, "MIND_PROACTIVE", "off") or "off").lower()
    return value if value in ("shadow", "on") else "off"


def _draw(*parts: str) -> float:
    raw = "\0".join(parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") / 2**64


def _last_message(session_id: str, role: str | None = None) -> float | None:
    """这个会话里最后一条（可限定谁说的）是什么时候。"""
    for item in reversed(list_messages(session_id, 30, ["role", "created_at"])):
        if role is None or item["role"] == role:
            return timeline.message_ts(item)
    return None


def unanswered(session_id: str) -> int:
    """他最后一次说话之后，她主动发了几回（影子模式里"本来会发"的也算，好看出她会怎么追）。"""
    since = _last_message(session_id, "user") or 0.0
    row = fetch_one("SELECT COUNT(*) AS n FROM mind_proactive WHERE session_id = ? AND status IN ('sent', 'shadow')"
                    " AND decided_at > ?", (session_id, since))
    return int(row["n"] or 0) if row else 0


# ── 登记念头 ────────────────────────────────────────────────────────────────

def _register(conn, session_id: str, key: str, kind: str, text: str, weight: float, born: float, now: float) -> bool:
    """幂等（key 唯一）。登记时就抽好：想不想说、过多久冒出来。"""
    text = str(text or "").strip()[:200]
    if not text or conn.execute("SELECT 1 FROM mind_proactive WHERE key = ?", (key,)).fetchone():
        return False
    weight = max(0.0, min(1.0, float(weight)))
    if kind == "nudge":
        want, mean, floor = 1.0, 25.0, 8.0          # 追不追由 STILL 在到点时定
    else:
        rel = relationship.get(session_id)
        want = weight * (0.5 + 0.5 * float(rel["closeness"])) * (0.3 if float(rel["distance"]) >= 0.6 else 1.0)
        mean, floor = 15.0 + 90.0 * (1.0 - weight), 2.0
    delay = min(240.0, max(floor, -mean * math.log(1.0 - min(0.999, _draw(key, "when")))))
    wanted = _draw(key, "want") < want
    conn.execute(
        "INSERT INTO mind_proactive(session_id, key, kind, text, weight, born_at, due_at, expire_at, status, reason,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, key, kind, text, weight, born, born + delay * 60, born + TTL_HOURS[kind] * 3600,
         "waiting" if wanted else "skipped", "" if wanted else "没那么想说", now, now))
    return wanted


def _reachable(session_id: str, now: float) -> tuple[bool, str]:
    seg = schedule.segment_at(now)
    if int(seg.get("level", 3)) < 2 or seg.get("muted"):
        return False, f"她在{seg.get('activity') or '忙'}，顾不上手机"
    return True, ""


def collect(session_id: str, now: float) -> int:
    """把新冒出来的念头登记进表。返回新登记且"想说"的个数。"""
    added = 0
    awake, _ = _reachable(session_id, now)
    with get_conn() as conn:
        if world_link.enabled():
            actor = world_link._actor() or {"state": {}}
            for item in actor["state"].get("recent_moments", []):
                at, share = float(item.get("at") or 0), float(item.get("share") or 0)
                if share >= SHARE_FLOOR and now - TTL_HOURS["moment"] * 3600 <= at <= now:
                    digest = hashlib.sha256(f"{at}|{item.get('text')}".encode()).hexdigest()[:16]
                    added += _register(conn, session_id, f"moment:{digest}", "moment", item.get("text"), share, at, now)
        if awake:   # 这三样是"醒着的时候想起来"的；夜里登记会让她一睁眼就全发出来
            day = datetime.fromtimestamp(now, tz=clock.local_now().tzinfo).strftime("%Y-%m-%d")
            for row in concerns.top(session_id):
                added += _register(conn, session_id, f"concern:{row['id']}:{day}", "concern",
                                   row["title"] + (f"（{row['detail']}）" if row.get("detail") else ""),
                                   0.7 * float(row["effective"]), now, now)
            for row in intents.open_intents(session_id, now):
                # 对得上世界活动的打算归世界去做；剩下的里只有跟他有关的才是"想跟他说/问他"
                if not row["choices"] and ABOUT_HIM.search(row["text"]):
                    added += _register(conn, session_id, f"intent:{row['id']}", "intent", row["text"],
                                       float(row["effective"]), now, now)
            last, his = _last_message(session_id), _last_message(session_id, "user")
            if last is not None and his is not None and now - last >= SILENCE_HOURS * 3600:
                hours = (now - last) / 3600
                weight = min(1.0, hours / 48.0) * float(relationship.get(session_id)["closeness"])
                added += _register(conn, session_id, f"silence:{day}", "silence",
                                   f"有 {timeline.gap(now - last)}没跟他说话了", weight, now, now)
        # 她主动发了、他没回 → 一个"追一句"的念头（每回主动只生一个）
        since = _last_message(session_id, "user") or 0.0
        row = conn.execute("SELECT id, decided_at, final_text FROM mind_proactive WHERE session_id = ? AND status IN ('sent','shadow')"
                           " AND decided_at > ? ORDER BY decided_at DESC LIMIT 1", (session_id, since)).fetchone()
        if row is not None:
            said = " / ".join(str(row["final_text"] or "").split("\n"))[:60]
            added += _register(conn, session_id, f"nudge:{row['id']}", "nudge", f"主动发了「{said}」，他一直没回",
                               1.0, float(row["decided_at"]), now)
    return added


# ── 时机 ────────────────────────────────────────────────────────────────────

def _set(row_id: int, **fields: Any) -> None:
    fields["updated_at"] = clock.now()
    with get_conn() as conn:
        conn.execute(f"UPDATE mind_proactive SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), row_id))


def blocked(session_id: str, now: float) -> str:
    """现在不能发的原因；空串 = 可以。没有安静时段，也没有每日上限。"""
    ok, why = _reachable(session_id, now)
    if not ok:
        return why
    if (store.newer_unhandled_perceptions(session_id, 0.0) or store.list_pending_delivery_cycles(session_id)
            or store.list_pending_deferred(session_id)):
        return "正在聊"
    spoke = fetch_one("SELECT MAX(decided_at) AS t FROM mind_proactive WHERE session_id = ?"
                      " AND status IN ('sent', 'shadow', 'queued')", (session_id,))
    last = max(_last_message(session_id) or 0.0, float((spoke["t"] if spoke else 0) or 0))
    if last and now - last < float(config.MIND_PROACTIVE_IDLE_MIN) * 60:
        return "刚聊过"     # 话会在聊天里自然带出来，不算主动
    tried = fetch_one("SELECT COUNT(*) AS n FROM mind_proactive WHERE session_id = ? AND cycle_id IS NOT NULL"
                      " AND decided_at > ?", (session_id, now - 86400))
    if tried and int(tried["n"] or 0) >= int(config.MIND_PROACTIVE_FUSE):
        return "保险丝：这一天想得太多回了"
    return ""


def _reconcile(session_id: str, now: float) -> None:
    """排了队的：真发出去了记 sent；被他的新消息打断、或一直发不出去（断网）记 cancelled。"""
    for row in fetch_all("SELECT * FROM mind_proactive WHERE session_id = ? AND status = 'queued'", (session_id,)):
        cycle_id = row["cycle_id"]
        left = fetch_one("SELECT COUNT(*) AS n FROM mind_delivery WHERE cycle_id = ? AND status = 'pending'"
                         " AND kind IN ('text', 'sticker')", (cycle_id,))
        if store.cycle_has_sent_text(cycle_id):
            _set(row["id"], status="sent", decided_at=now)
        elif not int(left["n"] or 0):
            _set(row["id"], status="cancelled", reason="他先来了消息，没发出去")
        elif now - float(row["decided_at"] or now) > float(config.MIND_PROACTIVE_EXPIRE_MIN) * 60:
            from app.services.mind import orchestration
            store.cancel_remaining_deliveries(cycle_id)
            orchestration._record_unsent(session_id, cycle_id, reason="网络不通，过期了")
            _set(row["id"], status="cancelled", reason="一直发不出去，过期作废")


def tick(session_id: str, now: float | None = None, *, force: bool = False) -> dict[str, Any]:
    """调度器每个 tick 调一次（内部限流到约 30 秒一回）。一次最多让一个念头走到"想"那一步。"""
    global _last_tick
    now = clock.now() if now is None else now
    if mode() == "off" or not session_id:
        return {}
    if not force and time.monotonic() - _last_tick < 30:
        return {}
    _last_tick = time.monotonic()
    _reconcile(session_id, now)     # 先对账（真发出去了没有），再看有没有新念头——"他没回"的念头靠这个
    result: dict[str, Any] = {"new": collect(session_id, now)}
    with get_conn() as conn:
        conn.execute("UPDATE mind_proactive SET status = 'lapsed', updated_at = ?, reason = CASE WHEN reason = ''"
                     " THEN '新鲜劲过了' ELSE reason || '，等到新鲜劲过了' END"
                     " WHERE session_id = ? AND status = 'waiting' AND expire_at < ?", (now, session_id, now))
    found = fetch_one("SELECT * FROM mind_proactive WHERE session_id = ? AND status = 'waiting' AND due_at <= ?"
                      " ORDER BY due_at ASC LIMIT 1", (session_id, now))
    if found is None:
        return result
    row = dict(found)
    why = blocked(session_id, now)
    if why:
        _set(row["id"], reason=why)     # 还等着，记一下被什么拦着
        return {**result, "held": why}
    n = unanswered(session_id)
    if _draw(row["key"], "still", str(n)) >= STILL[min(n, len(STILL) - 1)]:
        _set(row["id"], status="lapsed", reason=f"发了 {n} 回他都没回，不想再发了", decided_at=now)
        return {**result, "outcome": "lapsed"}
    return {**result, "outcome": think(row, session_id, now, n)}


# ── 想清楚，再说 ────────────────────────────────────────────────────────────

def think(row: dict[str, Any], session_id: str, now: float, n: int = 0) -> str:
    """走一遍深思 + 措辞。返回这个念头的结局：declined / shadow / queued / failed。"""
    from app.services.mind import cycle, delivery, orchestration
    from app.services.mind.schemas import L1Appraisal

    if row["kind"] == "nudge":
        text = f"你{timeline.ago(row['born_at'], now)}{row['text']}"
    else:
        text = LEAD.get(row["kind"], "") + row["text"]
        if n:
            text += f"（另外：你之前主动发的 {n} 回消息，他还没回）"
            with get_conn() as conn:    # 追问并进这一条里，不另发
                conn.execute("UPDATE mind_proactive SET status = 'merged', reason = '并进别的话里了', updated_at = ?"
                             " WHERE session_id = ? AND kind = 'nudge' AND status = 'waiting'", (now, session_id))
    ref = f"self:{row['key']}"
    cycle_id = cycle._derive_cycle_id(session_id, ref)
    _set(row["id"], cycle_id=cycle_id, decided_at=now)
    live = mode() == "on"
    l1 = L1Appraisal(category="分享", gut=str(row["text"])[:80], urgency=0.2, depth="normal", reply_will="now")
    try:
        final = cycle.run_cycle(
            session_id, text, orchestration._history_without_retracted(session_id), messages=[],
            trace_id=ref, trigger_type="self", trigger_ref=ref,
            preset_l1_json=json.dumps(l1.to_dict(), ensure_ascii=False),
            allow_fallback=False, initiative=text, commit=live)
    except Exception as exc:
        _set(row["id"], status="failed", reason=f"没想成：{type(exc).__name__}")
        log_event("mind", "proactive_failed", session_id=session_id, cycle_id=cycle_id, error_type=type(exc).__name__)
        return "failed"
    if not final.strip():
        _set(row["id"], status="declined", reason="想了想，算了")
        return "declined"
    if not live:
        _set(row["id"], status="shadow", final_text=final)
        log_event("mind", "proactive_shadow", session_id=session_id, cycle_id=cycle_id, kind=row["kind"])
        return "shadow"
    hesitate = False
    try:
        hesitate = bool(json.loads((store.get_cycle(cycle_id) or {}).get("l2_json") or "{}").get("l3", {}).get("hesitate"))
    except ValueError:
        pass
    delivery.plan_deliveries(session_id, final, cycle_id=cycle_id, base_ts=clock.now(), hesitate=hesitate)
    _set(row["id"], status="queued", final_text=final)
    log_event("mind", "proactive_queued", session_id=session_id, cycle_id=cycle_id, kind=row["kind"])
    return "queued"


# ── 给人看 ──────────────────────────────────────────────────────────────────

WORDS = {"waiting": "等着", "skipped": "没提", "lapsed": "算了", "declined": "想了想没发", "shadow": "本来会发",
         "queued": "正在发", "sent": "发了", "cancelled": "没发出去", "merged": "并进别的话里", "failed": "出错"}


def show(session_id: str, limit: int = 30) -> list[str]:
    """最近的念头和结局，一行一个，新的在前。"""
    lines = []
    for row in fetch_all("SELECT * FROM mind_proactive WHERE session_id = ? ORDER BY COALESCE(decided_at, due_at) DESC"
                         " LIMIT ?", (session_id, limit)):
        when = datetime.fromtimestamp(float(row["decided_at"] or row["due_at"]), tz=clock.local_now().tzinfo)
        tail = f"：{str(row['final_text']).replace(chr(10), ' / ')}" if row["final_text"] else ""
        why = f"（{row['reason']}）" if row["reason"] else ""
        lines.append(f"{when:%m-%d %H:%M} [{WORDS.get(row['status'], row['status'])}] {row['text'][:40]}{why}{tail}")
    return lines


if __name__ == "__main__":
    import os
    import sys

    from dotenv import load_dotenv

    load_dotenv()      # 单独运行时 app.config 已经在读 .env 之前导入了，会话名从环境里现取
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(show(os.getenv("MIND_HOME_SESSION", "").strip() or "me")) or "还没有念头")
