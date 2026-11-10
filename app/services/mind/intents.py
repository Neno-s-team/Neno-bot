"""她的打算（D4）：睡前想的"明天做什么"、聊天里随口说的"一会儿去……"。

存在大脑侧 mind_intent；世界的空档挑事（life.choose）读它，给匹配的候选加分。
文字 → 候选：按关键词粗匹配到 life.json 的 choices（写论文→study_*，散步/江边→walk_river，
咖啡→coffee……），匹配不上就只当心事，不影响挑事。
强度会淡：每天乘 0.5–0.8（抽样）；过期或做过就收起；做过的打算记"done"。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.services.mind import clock
from app.storage.db import fetch_all, get_conn

# 关键词 → 候选 id（同一打算可以对上几个候选，挑哪个仍由世界决定）
KEYWORDS: list[tuple[str, list[str]]] = [
    (r"论文|文献|写.{0,4}(章|稿)|改稿|开题|读书报告|看文献", ["study_desk", "study_library", "study_cafe"]),
    (r"图书馆", ["study_library"]),
    (r"咖啡|拿铁|美式", ["coffee", "study_cafe"]),
    (r"江边|散步|走走|透透气|出去转", ["walk_river"]),
    (r"小说|闲书|看书|追剧|看剧", ["read_bed"]),
    (r"睡个?午觉|补觉|睡一会|眯一会", ["nap"]),
    (r"歇|休息|躺", ["rest_sofa", "nap"]),
    (r"做饭|下厨|煮", ["cook"]),
    (r"吃|饭|零食|奶茶", ["eat_home", "cook", "snack", "eat_canteen"]),
    (r"妈|爸|家里人|打电话", ["call_parents"]),
    (r"刷手机|刷视频|小红书|抖音|b站", ["scroll"]),
]


def _draw(*parts: str) -> float:
    raw = "\0".join(parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") / 2**64


def match(text: str) -> list[str]:
    hits: list[str] = []
    for pattern, ids in KEYWORDS:
        if re.search(pattern, text or "", re.IGNORECASE):
            hits.extend(i for i in ids if i not in hits)
    return hits


def add(conn, session_id: str, text: str, *, source: str, strength: float, due_at: float, key: str) -> int | None:
    text = str(text or "").strip()[:80]
    if not text:
        return None
    now = clock.now()
    cursor = conn.execute(
        """INSERT INTO mind_intent(session_id, text, choices_json, strength, source, status, due_at,
             created_at, updated_at, key)
           VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?) ON CONFLICT(key) DO NOTHING""",
        (session_id, text, json.dumps(match(text)), max(0.05, min(1.0, strength)), source, due_at, now, now, key),
    )
    return int(cursor.lastrowid) if cursor.rowcount else None


def effective(row: dict[str, Any], now: float) -> float:
    days = max(0.0, (now - float(row["created_at"])) / 86400.0)
    keep = 0.5 + 0.3 * _draw(str(row["id"]), "keep")  # 每天留下 50–80%
    return float(row["strength"]) * keep ** days


def open_intents(session_id: str | None = None, now: float | None = None) -> list[dict[str, Any]]:
    now = clock.now() if now is None else now
    sql = "SELECT * FROM mind_intent WHERE status = 'open'" + (" AND session_id = ?" if session_id else "")
    rows = [dict(r) for r in fetch_all(sql, (session_id,) if session_id else ())]
    result = []
    for row in rows:
        row["choices"] = json.loads(row.pop("choices_json") or "[]")
        row["effective"] = effective(row, now)
        if row["effective"] >= 0.05 and now <= float(row["due_at"]) + 86400:
            result.append(row)
    return sorted(result, key=lambda r: -r["effective"])


def boosts(now: float | None = None) -> dict[str, float]:
    """候选 id → 打算带来的加分（取各打算里最大的）。给世界的挑事用。"""
    result: dict[str, float] = {}
    for row in open_intents(now=now):
        for choice in row["choices"]:
            result[choice] = max(result.get(choice, 0.0), 1.5 * row["effective"])
    for row in fetch_all("SELECT detail, text, strength FROM mind_wish WHERE status = 'wish'"):
        for choice in match(row["detail"] or row["text"]):
            result[choice] = max(result.get(choice, 0.0), 0.6 * float(row["strength"]))
    return result


def mark_done(choice_id: str, now: float | None = None) -> list[int]:
    """世界里做了某件事 → 对得上的打算收起（只收最强的一条）。"""
    now = clock.now() if now is None else now
    for row in open_intents(now=now):
        if choice_id in row["choices"]:
            with get_conn() as conn:
                conn.execute("UPDATE mind_intent SET status='done', updated_at=? WHERE id=?", (now, row["id"]))
            return [int(row["id"])]
    return []


def expire(conn, now: float) -> int:
    count = 0
    for row in conn.execute("SELECT * FROM mind_intent WHERE status = 'open'").fetchall():
        item = dict(row)
        if effective(item, now) < 0.05 or now > float(item["due_at"]) + 86400:
            conn.execute("UPDATE mind_intent SET status='lapsed', updated_at=? WHERE id=?", (now, item["id"]))
            count += 1
    return count


def render(session_id: str, limit: int = 3) -> list[str]:
    items = open_intents(session_id)[:limit]
    return [f"{row['text']}（{'挺想做' if row['effective'] >= 0.5 else '有点想'}）" for row in items]
