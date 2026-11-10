"""长期心愿（D6）：不预设，只从日子里长出来。

每晚反思后看"明天想做的事"和"记挂的事"：同一件事（按关键词归一）连着出现，计数 +1；
计数 ≥ 3 后每晚按概率（0.15 × (计数−2)，封顶 0.6）升格为心愿（status=wish）。
没再出现的种子每晚淡一点，淡没了就丢；心愿本身也会淡，几周不碰就放下。
心愿进工作记忆（"一直想……"），也给世界挑事一点加分（经由 intents.boosts 的同一套候选匹配）。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app.services.mind import clock, intents
from app.storage.db import fetch_all


def _draw(*parts: str) -> float:
    raw = "\0".join(parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") / 2**64


def _normalize(text: str) -> str:
    """粗归一：取命中的候选 id 组合；没命中的取去标点后的前 8 个字。"""
    hits = intents.match(text)
    if hits:
        return "|".join(sorted(hits))
    return re.sub(r"[\s，。！？、,.!?~～]", "", text)[:8]


def observe(conn, session_id: str, night: str, thought: dict[str, Any] | None, now: float) -> dict[str, int]:
    texts = [str(x) for x in (thought or {}).get("tomorrow") or []]
    texts += [str(x.get("title") or "") for x in (thought or {}).get("new_concerns") or []]
    seen = {}
    for text in texts:
        if text.strip():
            seen.setdefault(_normalize(text), text.strip()[:60])
    grown = 0
    for norm, text in seen.items():
        row = conn.execute("SELECT * FROM mind_wish WHERE session_id = ? AND text = ?", (session_id, norm)).fetchone()
        if row is None:
            conn.execute("INSERT INTO mind_wish(session_id, text, detail, strength, seen_nights, status, created_at,"
                         " updated_at) VALUES (?, ?, ?, 0.3, 1, 'seed', ?, ?)", (session_id, norm, text, now, now))
            continue
        count = int(row["seen_nights"]) + 1
        status = row["status"]
        if status == "seed" and count >= 3 and _draw(session_id, night, norm, "grow") < min(0.6, 0.15 * (count - 2)):
            status = "wish"
            grown += 1
        conn.execute("UPDATE mind_wish SET seen_nights=?, status=?, strength=MIN(1.0, strength+0.1), detail=?,"
                     " updated_at=? WHERE id=?", (count, status, text, now, row["id"]))
    faded = 0
    for row in conn.execute("SELECT * FROM mind_wish WHERE session_id = ? AND status IN ('seed','wish')", (session_id,)).fetchall():
        if row["text"] in seen:
            continue
        keep = 0.75 if row["status"] == "seed" else 0.93
        strength = float(row["strength"]) * keep
        if strength < 0.08:
            conn.execute("UPDATE mind_wish SET status='gone', strength=?, updated_at=? WHERE id=?", (strength, now, row["id"]))
            faded += 1
        else:
            conn.execute("UPDATE mind_wish SET strength=? WHERE id=?", (strength, row["id"]))
    return {"grown": grown, "faded": faded}


def render(session_id: str, limit: int = 2) -> list[str]:
    rows = fetch_all("SELECT * FROM mind_wish WHERE session_id = ? AND status = 'wish' ORDER BY strength DESC LIMIT ?",
                     (session_id, limit))
    return [f"一直想{(r['detail'] or r['text']).lstrip('想')}（心愿）" for r in rows]
