"""L4 夜间反思：入睡后回顾当天，补记闲聊、修正认识、放下或记挂事情、情绪收敛、遗忘。

触发：课表进入睡眠段后，延迟一段按 seed 抽的时间（MIND_REFLECT_DELAY_MIN）。
一晚一次（mind_reflection 主键 session_id + night），租约 + 重试，与回复作业同一把 session 锁。

只有"想"调模型（一次）；遗忘、情绪收敛、在意事项淡去都是规则 + 抽样，模型失败也照做。
规则不替她决定：模型给建议，规则只限幅（一晚认识最多 ±0.3、在意最多 ±0.4、情绪最多收一半）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from app import config
from app.services.chat.llm_gateway import request_model_response
from app.services.mind import clock, concerns, intents, relationship, schedule, state, timeline, wishes, world_link
from app.services.mind.deliberation import BeliefUpdate, strip_think
from app.services.mind.memory import importance as importance_rules
from app.services.mind.memory.commit import apply_belief_update
from app.services.mind.memory.retrieval import _eff_strength
from app.services.mind.memory.tokenize import to_index_text
from app.storage.db import fetch_all, fetch_one, get_conn
from app.utils.logging_utils import log_event

_MAX_TURNS = 120
_MAX_TURN_CHARS = 200
_MAX_EPISODES = 40
_MAX_NEW_NOTES = 5


def _draw(*parts: str) -> float:
    raw = "\0".join(parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") / 2**64


def _between(low_high, *parts: str) -> float:
    low, high = float(low_high[0]), float(low_high[1])
    return low + (high - low) * _draw(*parts)


# ── 排期 ────────────────────────────────────────────────────────────────────

def _sleep_start(ts: float) -> float | None:
    """ts 若在睡眠段内，返回这一觉的开始时刻（跨午夜的睡眠段在课表里是两段，要连起来看）。"""
    if schedule.segment_at(ts).get("activity") != "睡觉":
        return None
    segments = list(schedule.iter_segments(ts - 86400, ts + 1))
    start = ts
    for seg_start, seg_end, seg in reversed(segments):
        if seg_start > ts:
            continue
        if seg.get("activity") != "睡觉" or seg_end < start - 60:
            break
        start = seg_start
    return start


def plan_tonight(session_id: str, now: float | None = None) -> dict[str, Any] | None:
    """在睡眠段里就为这一晚排一次反思（幂等）；返回该行。"""
    now = clock.now() if now is None else now
    start = _sleep_start(now)
    if start is None:
        return None
    night = datetime.fromtimestamp(start, tz=clock.local_now().tzinfo).strftime("%Y-%m-%d")
    previous = fetch_one(
        "SELECT window_end FROM mind_reflection WHERE session_id = ? AND night < ? ORDER BY night DESC LIMIT 1",
        (session_id, night),
    )
    window_start = float(previous["window_end"]) if previous else start - 86400
    delay = _between(config.MIND_REFLECT_DELAY_MIN, session_id, night, "delay") * 60
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO mind_reflection(session_id, night, status, window_start, window_end, due_at,
                 created_at, updated_at)
               VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
               ON CONFLICT(session_id, night) DO NOTHING""",
            (session_id, night, window_start, start, start + delay, now, now),
        )
    row = fetch_one("SELECT * FROM mind_reflection WHERE session_id = ? AND night = ?", (session_id, night))
    return dict(row) if row else None


def active_sessions(since: float) -> list[str]:
    rows = fetch_all(
        "SELECT DISTINCT session_id FROM mind_cycle WHERE created_at >= ? AND status = 'completed'",
        (since,),
    )
    sessions = [row["session_id"] for row in rows]
    home = config.MIND_HOME_SESSION
    if home and world_link.enabled() and home not in sessions:
        sessions.append(home)  # 世界接上后，没聊天的日子她也照样睡前回想
    return sessions


def claim_due(now: float) -> list[dict[str, Any]]:
    lease = now + max(60, int(config.MIND_LEASE_SEC)) * 3
    claimed = []
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM mind_reflection
               WHERE (status = 'pending' AND due_at <= ?) OR (status = 'running' AND lease_until < ?)""",
            (now, now),
        ).fetchall()
        for row in rows:
            changed = conn.execute(
                """UPDATE mind_reflection SET status='running', lease_until=?, attempts=attempts+1, updated_at=?
                   WHERE session_id=? AND night=? AND status=?""",
                (lease, now, row["session_id"], row["night"], row["status"]),
            ).rowcount
            if changed:
                claimed.append({**dict(row), "attempts": int(row["attempts"]) + 1})
    return claimed


# ── 素材 ────────────────────────────────────────────────────────────────────

def _day_material(session_id: str, start: float, end: float) -> dict[str, Any]:
    tz = clock.local_now().tzinfo
    turns = fetch_all(
        """SELECT role, content, created_at FROM messages
           WHERE session_id = ? AND role IN ('user','assistant')
             AND CAST(strftime('%s', created_at) AS INTEGER) >= ?
             AND CAST(strftime('%s', created_at) AS INTEGER) < ?
           ORDER BY id DESC LIMIT ?""",
        (session_id, int(start), int(end), _MAX_TURNS),
    )
    episodes = fetch_all(
        """SELECT id, ts, summary, importance, valence, arousal FROM mind_episode
           WHERE session_id = ? AND ts >= ? AND ts < ? ORDER BY ts LIMIT ?""",
        (session_id, start, end, _MAX_EPISODES),
    )
    beliefs = fetch_all(
        "SELECT id, subject, content, confidence FROM mind_belief WHERE session_id = ? ORDER BY confidence DESC LIMIT 20",
        (session_id,),
    )
    return {
        "turns": [{"role": r["role"], "at": _clock_text(r["created_at"], tz), "content": str(r["content"])[:_MAX_TURN_CHARS]}
                  for r in reversed(turns)],
        "episodes": [dict(r) for r in episodes],
        "beliefs": [dict(r) for r in beliefs],
        "concerns": concerns.list_open(session_id, end),
        "session_id": session_id,
        "lived": world_link.day_events(start, end) if session_id == config.MIND_HOME_SESSION else [],
    }


def _clock_text(created_at: str, tz) -> str:
    try:
        stamp = datetime.fromisoformat(str(created_at)).replace(tzinfo=timezone.utc)
        return stamp.astimezone(tz).strftime("%H:%M")
    except ValueError:
        return ""


REFLECT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {"name": "mind_l4_reflection", "strict": True, "schema": {
        "type": "object", "additionalProperties": False,
        "required": ["day_summary", "new_notes", "belief_updates", "concern_updates", "new_concerns", "mood", "relation", "tomorrow"],
        "properties": {
            "day_summary": {"type": "string"},
            "new_notes": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["summary", "tags"],
                "properties": {"summary": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}}},
            "belief_updates": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["subject", "content", "confidence", "evidence"],
                "properties": {"subject": {"type": "string"}, "content": {"type": "string"},
                               "confidence": {"type": "number"}, "evidence": {"type": "array", "items": {"type": "integer"}}}}},
            "concern_updates": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["id", "action", "intensity", "note"],
                "properties": {"id": {"type": "integer"}, "action": {"type": "string", "enum": ["keep", "ease", "grow", "let_go"]},
                               "intensity": {"type": "number"}, "note": {"type": "string"}}}},
            "new_concerns": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["title", "detail", "intensity", "evidence"],
                "properties": {"title": {"type": "string"}, "detail": {"type": "string"}, "intensity": {"type": "number"},
                               "evidence": {"type": "array", "items": {"type": "integer"}}}}},
            "mood": {"type": "object", "additionalProperties": False, "required": ["valence", "settle"],
                     "properties": {"valence": {"type": "number"}, "settle": {"type": "number"}}},
            "relation": {"type": "object", "additionalProperties": False, "required": ["closer", "attraction", "distance", "view"],
                         "properties": {"closer": {"type": "number"}, "attraction": {"type": "number"},
                                        "distance": {"type": "number"}, "view": {"type": "string"}}},
            "tomorrow": {"type": "array", "items": {"type": "string"}},
        }}},
}


def _messages(self_card: str, material: dict[str, Any], mood: dict[str, Any]) -> list[dict]:
    turns = "\n".join(f"[{t['at']}] {'他' if t['role'] == 'user' else '我'}：{t['content']}" for t in material["turns"]) or "（今天没聊天）"
    episodes = "\n".join(f"#{e['id']} [{timeline.stamp(e['ts'])}] {e['summary']}" for e in material["episodes"]) or "（无）"
    beliefs = "\n".join(f"- {b['subject']}：{b['content']}（把握 {b['confidence']:.2f}）" for b in material["beliefs"]) or "（无）"
    bond = relationship.describe(material["session_id"])
    worries = "\n".join(f"- id={c['id']} {c['title']}（{timeline.ago(c['created_at'])}开始记挂，强度 {c['effective']:.2f}）{c['detail']}"
                        for c in material["concerns"]) or "（无）"
    lived = "\n".join(material.get("lived") or []) or "（没记下什么）"
    prompt = f"""现在是 {timeline.today_line()}，你躺下了，在脑子里过一遍今天。用你自己的视角想，不要写成报告。

【你今天过的日子】
{lived}

【今天的聊天原话】
{turns}

【今天已经记下的事】
{episodes}

【你现有的认识】
{beliefs}

【心里记挂着的事】
{worries}

【你和他现在的关系】
{bond}

【此刻心情】valence={mood['valence']:.2f} arousal={mood['arousal']:.2f}

【输出要求】只输出一个 JSON 对象。
- day_summary：一两句话，今天对你来说是怎样的一天。
- new_notes：≤{_MAX_NEW_NOTES} 条。今天过的日子和聊天原话里值得记住、但【今天已经记下的事】里没有的事（小事也算，比如谁打来电话、去了哪、出了点什么状况），"我"的视角。没有就空。
- belief_updates：≤3 条，对人或事的认识变化；evidence 只能填上面出现过的 #id 数字。
- concern_updates：对每条记挂的事选 keep（还挂着）/ ease（松了些）/ grow（更在意了）/ let_go（放下了），intensity 是你觉得现在的在意程度 0–1。
- new_concerns：≤2 条今天新冒出来、明天可能还会想起的事；没有就空。
- mood.valence：睡前心情（-1 到 1）；mood.settle：情绪平复了多少（0 没平复，1 完全平复）。
- relation：今天和对方的相处让你们的关系怎么动了，各项 -1 到 1，没变化就填 0（大多数日子都在 0 附近）。
  closer 更近还是更远；attraction 心动多了还是少了；distance 多了隔阂还是解开了；view 一句话说你现在怎么看他。
- tomorrow：≤3 条明天想做或想记得的事，可以为空。
- 写下来的东西里提到时间时，写清是哪天（"今天下午""11月12日周三"），不要只写"明天""那天"——过几天再看就不知道指哪天了。"""
    return [{"role": "system", "content": self_card or "你是一个普通人，在睡前回想今天。"},
            {"role": "user", "content": prompt}]


def _think(session_id: str, material: dict[str, Any], mood: dict[str, Any]) -> dict[str, Any]:
    from app.services.mind.cycle import _load_self_card

    raw = request_model_response(
        model_name=config.MIND_REFLECT_MODEL or config.MIND_THINK_MODEL or config.CHAT_MODEL_NAME,
        messages=_messages(_load_self_card(), material, mood),
        session_id=session_id,
        extra_body={**config.MIND_THINK_EXTRA_BODY, "response_format": REFLECT_SCHEMA,
                    "provider": {"require_parameters": True}},
        timeout=config.LLM_TIMEOUT_SECONDS,
        max_tokens=config.MIND_REFLECT_MAX_TOKENS,
        purpose="mind_reflect",
    )
    data = json.loads(strip_think(raw))
    if not isinstance(data, dict):
        raise ValueError("reflection is not an object")
    return data


# ── 落实（规则限幅 + 抽样）──────────────────────────────────────────────────

def _apply(session_id: str, night: str, window_end: float, thought: dict[str, Any] | None,
           material: dict[str, Any]) -> dict[str, Any]:
    now = clock.now()
    known_ids = {int(e["id"]) for e in material["episodes"]}
    summary: dict[str, Any] = {"notes": 0, "beliefs": 0, "concerns_new": 0, "concerns_changed": 0}
    with get_conn() as conn:
        if thought:
            _apply_notes(conn, session_id, night, window_end, thought, material, known_ids, summary, now)
            _apply_beliefs(conn, session_id, thought, known_ids, summary)
            _apply_concerns(conn, session_id, night, thought, material, known_ids, summary, now)
            summary["relation"] = relationship.nudge(conn, session_id, night, thought.get("relation"))
            summary["intents"] = _apply_tomorrow(conn, session_id, night, thought, now)
            summary["wishes"] = wishes.observe(conn, session_id, night, thought, now)
        summary["intents_lapsed"] = intents.expire(conn, now)
        summary["concerns_faded"] = concerns.drop_faded(conn, session_id, now)
        summary["episodes_faded"] = _fade(conn, session_id, night, now)
    summary["mood"] = _settle_mood(session_id, night, thought)
    if thought:
        summary["day_summary"] = str(thought.get("day_summary") or "")[:200]
        summary["tomorrow"] = [str(x)[:80] for x in (thought.get("tomorrow") or [])][:3]
    return summary


def _apply_tomorrow(conn, session_id, night, thought, now) -> int:
    """睡前想的"明天做什么" → 打算（明天到期）；强度按 seed 抽 0.4–0.8。"""
    added = 0
    for idx, text in enumerate((thought.get("tomorrow") or [])[:3]):
        strength = _between([0.4, 0.8], session_id, night, str(idx), "intent")
        if intents.add(conn, session_id, str(text), source="reflection", strength=strength,
                       due_at=now + 86400, key=f"reflect:{session_id}:{night}:{idx}"):
            added += 1
    return added


def _apply_notes(conn, session_id, night, window_end, thought, material, known_ids, summary, now):
    existing = {str(e["summary"]) for e in material["episodes"]}
    for idx, note in enumerate((thought.get("new_notes") or [])[:_MAX_NEW_NOTES]):
        text = str(note.get("summary") or "").strip()[:300]
        if not text or text in existing:
            continue
        tags = [str(t)[:20] for t in (note.get("tags") or [])][:8]
        score = importance_rules.score_importance(session_id, {"summary": text, "participants": [], "tags": tags})
        cursor = conn.execute(
            """INSERT INTO mind_episode(session_id, cycle_id, note_idx, ts, participants_json, summary, tags_json,
                 valence, arousal, importance, strength, refs_json, created_at, source)
               VALUES (?, ?, ?, ?, '[]', ?, ?, 0, 0, ?, 0.8, ?, ?, 'reflection')
               ON CONFLICT(cycle_id, note_idx) DO NOTHING""",
            (session_id, f"reflect:{session_id}:{night}", idx, window_end - 60, text,
             json.dumps(tags, ensure_ascii=False), score, json.dumps({"night": night}), now),
        )
        if cursor.rowcount:
            new_id = int(cursor.lastrowid)
            known_ids.add(new_id)
            conn.execute("INSERT INTO mind_episode_fts(rowid, idx_text, tags) VALUES (?, ?, ?)",
                         (new_id, to_index_text(text), to_index_text(" ".join(tags))))
            summary["notes"] += 1


def _apply_beliefs(conn, session_id, thought, known_ids, summary):
    beliefs = {r["subject"]: dict(r) for r in conn.execute(
        "SELECT id, subject, content, confidence, evidence_json, version FROM mind_belief WHERE session_id = ?",
        (session_id,)).fetchall()}
    for item in (thought.get("belief_updates") or [])[:3]:
        subject = str(item.get("subject") or "").strip()[:60]
        content = str(item.get("content") or "").strip()[:300]
        if not subject or not content:
            continue
        evidence = [int(x) for x in (item.get("evidence") or []) if int(x) in known_ids]
        update = BeliefUpdate(subject=subject, content=content,
                              confidence=max(0.0, min(1.0, float(item.get("confidence") or 0.0))), evidence=evidence)
        apply_belief_update(conn, session_id, update, beliefs.get(subject))
        summary["beliefs"] += 1


def _apply_concerns(conn, session_id, night, thought, material, known_ids, summary, now):
    open_ids = {int(c["id"]) for c in material["concerns"]}
    for item in thought.get("concern_updates") or []:
        cid = int(item.get("id") or 0)
        if cid not in open_ids:
            continue
        action = item.get("action")
        note = str(item.get("note") or "")
        if action == "let_go":
            # 说放下不一定真放得下：按 seed 抽，有时只是松了一大截
            if _draw(session_id, night, str(cid), "let_go") < 0.75:
                concerns.adjust(conn, session_id, cid, close_reason=note or "放下了", now=now)
            else:
                current = next(c["effective"] for c in material["concerns"] if int(c["id"]) == cid)
                left = current * _between([0.3, 0.6], session_id, night, str(cid), "left")
                concerns.adjust(conn, session_id, cid, intensity=max(left, 0.1), now=now)
        elif action in {"ease", "grow", "keep"}:
            concerns.adjust(conn, session_id, cid, intensity=float(item.get("intensity") or 0.0),
                            detail=note or None, now=now)
        else:
            continue
        summary["concerns_changed"] += 1
    for item in (thought.get("new_concerns") or [])[:2]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        decay = _between([0.1, 0.45], session_id, night, title, "decay")
        evidence = [int(x) for x in (item.get("evidence") or []) if int(x) in known_ids]
        concerns.open_concern(conn, session_id, title, str(item.get("detail") or ""),
                              min(0.8, float(item.get("intensity") or 0.4)), decay, evidence, now)
        summary["concerns_new"] += 1


def _fade(conn, session_id: str, night: str, now: float) -> int:
    """有效强度低于今晚抽到的阈值 → summary 换成大意（前一小截 + "……"），原文移到 full_summary。

    阈值每晚在区间内抽；重要或情绪强的事更难淡（阈值最多打到两成）。原始聊天永不删除。
    """
    threshold = _between(config.MIND_FADE_THRESHOLD, session_id, night, "fade")
    faded = 0
    rows = conn.execute(
        "SELECT * FROM mind_episode WHERE session_id = ? AND faded_at IS NULL", (session_id,)
    ).fetchall()
    for row in rows:
        item = dict(row)
        weight = max(float(item["importance"] or 0),
                     min(1.0, abs(float(item["valence"] or 0)) + max(float(item["arousal"] or 0), 0)))
        if _eff_strength(item, now) >= threshold * (1 - 0.8 * weight):
            continue
        text = str(item["summary"])
        keep = max(6, int(len(text) * _between([0.25, 0.45], session_id, night, str(item["id"]), "gist")))
        gist = text[:keep].rstrip("，。、；：,.;: ") + "……"
        tags = json.loads(item["tags_json"] or "[]")
        conn.execute(
            "INSERT INTO mind_episode_fts(mind_episode_fts, rowid, idx_text, tags) VALUES ('delete', ?, ?, ?)",
            (item["id"], to_index_text(text), to_index_text(" ".join(tags))))
        conn.execute("UPDATE mind_episode SET summary = ?, full_summary = ?, faded_at = ? WHERE id = ?",
                     (gist, text, now, item["id"]))
        conn.execute("INSERT INTO mind_episode_fts(rowid, idx_text, tags) VALUES (?, ?, ?)",
                     (item["id"], to_index_text(gist), to_index_text(" ".join(tags))))
        faded += 1
    return faded


def _settle_mood(session_id: str, night: str, thought: dict[str, Any] | None) -> dict[str, float]:
    """睡一觉情绪收敛：激动大半退去；好坏朝睡前心情靠一点，幅度按 seed 抽。"""
    current = state.read_state(session_id)
    mood = (thought or {}).get("mood") or {}
    settle = max(0.0, min(1.0, float(mood.get("settle", 0.5))))
    settle *= _between([0.6, 1.0], session_id, night, "settle")
    target_v = max(-1.0, min(1.0, float(mood.get("valence", current["valence"]))))
    dv = (target_v - current["valence"]) * 0.5 * settle - current["valence"] * 0.3 * settle
    da = -current["arousal"] * max(0.5, settle)
    state.apply_affect_delta(session_id, dv, da)
    after = state.read_state(session_id)
    return {"valence": round(after["valence"], 3), "arousal": round(after["arousal"], 3)}


def run(row: dict[str, Any]) -> dict[str, Any]:
    """跑一晚的反思；模型失败且还有重试次数就抛出，最后一次失败则只做规则部分。"""
    session_id, night = row["session_id"], row["night"]
    material = _day_material(session_id, float(row["window_start"]), float(row["window_end"]))
    thought, error = None, None
    if material["turns"] or material["episodes"] or material["concerns"] or material.get("lived"):
        try:
            thought = _think(session_id, material, state.read_state(session_id))
        except Exception as exc:
            error = type(exc).__name__
            if int(row.get("attempts") or 1) < max(1, int(config.MIND_JOB_MAX_ATTEMPTS)):
                raise
    result = _apply(session_id, night, float(row["window_end"]), thought, material)
    result["model"] = "ok" if thought else ("skipped" if error is None else f"failed:{error}")
    return result


def finish(row: dict[str, Any], result: dict[str, Any] | None = None, error: str | None = None) -> None:
    now = clock.now()
    with get_conn() as conn:
        if error is None:
            conn.execute(
                "UPDATE mind_reflection SET status='done', result_json=?, lease_until=NULL, updated_at=?"
                " WHERE session_id=? AND night=?",
                (json.dumps(result, ensure_ascii=False), now, row["session_id"], row["night"]))
            return
        final = int(row.get("attempts") or 1) >= max(1, int(config.MIND_JOB_MAX_ATTEMPTS))
        conn.execute(
            "UPDATE mind_reflection SET status=?, error=?, lease_until=NULL, due_at=?, updated_at=?"
            " WHERE session_id=? AND night=?",
            ("failed" if final else "pending", error[:200], now + 600, now, row["session_id"], row["night"]))
