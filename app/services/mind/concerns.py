"""在意事项：夜间反思里产生/放下；白天被线索勾起会重新升温；强度按天自然淡去。

强度惰性衰减：eff = intensity × 2^(−天数 × decay_per_day)，锚点为 touched_at。
升温幅度按 seed 抽（同一句话不一定每次都勾起同样强的念头）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app import config
from app.services.mind import clock
from app.services.mind.memory.tokenize import to_index_text
from app.storage.db import fetch_all, get_conn
from app.utils.logging_utils import log_event


def _draw(*parts: str) -> float:
    raw = "\0".join(parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") / 2**64


def effective(row: dict[str, Any], now: float | None = None) -> float:
    now = clock.now() if now is None else now
    days = max(0.0, (now - float(row["touched_at"] or row["created_at"] or now)) / 86400.0)
    return float(row["intensity"]) * 2 ** (-days * float(row["decay_per_day"]))


def list_open(session_id: str, now: float | None = None) -> list[dict[str, Any]]:
    rows = fetch_all("SELECT * FROM mind_concern WHERE session_id = ? AND status = 'open'", (session_id,))
    items = []
    for row in rows:
        item = dict(row)
        item["effective"] = effective(item, now)
        item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
        items.append(item)
    items.sort(key=lambda x: -x["effective"])
    return items


def top(session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    limit = max(0, int(config.MIND_WM_CONCERNS if limit is None else limit))
    floor = float(config.MIND_CONCERN_DROP_BELOW)
    return [x for x in list_open(session_id) if x["effective"] >= floor][:limit]


def render(items: list[dict[str, Any]]) -> list[str]:
    result = []
    for item in items:
        level = "很在意" if item["effective"] >= 0.6 else "有点在意" if item["effective"] >= 0.3 else "隐约记着"
        detail = f"：{item['detail']}" if item.get("detail") else ""
        from app.services.mind import timeline
        level = f"{level}，{timeline.ago(item['created_at'])}开始的"
        result.append(f"{item['title']}（{level}）{detail}")
    return result


def open_concern(conn, session_id: str, title: str, detail: str, intensity: float,
                 decay_per_day: float, evidence: list[int], now: float) -> int:
    cursor = conn.execute(
        """INSERT INTO mind_concern(session_id, title, detail, intensity, decay_per_day, status,
             evidence_json, created_at, updated_at, touched_at)
           VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
        (session_id, title[:60], detail[:200], max(0.05, min(1.0, intensity)),
         max(0.02, min(2.0, decay_per_day)), json.dumps(evidence), now, now, now),
    )
    return int(cursor.lastrowid)


def adjust(conn, session_id: str, concern_id: int, *, intensity: float | None = None,
           close_reason: str | None = None, detail: str | None = None, now: float) -> bool:
    row = conn.execute("SELECT * FROM mind_concern WHERE id = ? AND session_id = ? AND status = 'open'",
                       (concern_id, session_id)).fetchone()
    if row is None:
        return False
    if close_reason is not None:
        conn.execute("UPDATE mind_concern SET status='closed', closed_at=?, close_reason=?, updated_at=? WHERE id=?",
                     (now, close_reason[:120], now, concern_id))
        return True
    current = effective(dict(row), now)
    # 一晚上最多改 ±0.4：想通了也不会一下子全放下
    target = current if intensity is None else max(current - 0.4, min(current + 0.4, intensity))
    conn.execute("UPDATE mind_concern SET intensity=?, touched_at=?, updated_at=?, detail=? WHERE id=?",
                 (max(0.0, min(1.0, target)), now, now, (detail if detail is not None else row["detail"])[:200], concern_id))
    return True


def drop_faded(conn, session_id: str, now: float) -> int:
    floor = float(config.MIND_CONCERN_DROP_BELOW)
    dropped = 0
    for row in conn.execute("SELECT * FROM mind_concern WHERE session_id = ? AND status = 'open'", (session_id,)).fetchall():
        if effective(dict(row), now) < floor:
            conn.execute("UPDATE mind_concern SET status='faded', closed_at=?, close_reason='淡了', updated_at=? WHERE id=?",
                         (now, now, row["id"]))
            dropped += 1
    return dropped


def rewarm(session_id: str, cues: list[str], message: str, trace_id: str | None = None) -> list[int]:
    """线索与在意事项标题/细节有词重叠 → 按 seed 抽一个升温幅度（0.05–0.25），锚点刷新。"""
    probe = set(to_index_text(" ".join([*(cues or []), message or ""])).split())
    if not probe:
        return []
    now = clock.now()
    warmed = []
    with get_conn() as conn:
        for row in conn.execute("SELECT * FROM mind_concern WHERE session_id = ? AND status = 'open'", (session_id,)).fetchall():
            terms = set(to_index_text(f"{row['title']} {row['detail']}").split())
            if len(probe & terms) < 1 or not (probe & {t for t in terms if len(t) >= 2}):
                continue
            bump = 0.05 + 0.2 * _draw(session_id, str(row["id"]), str(trace_id or now), "rewarm")
            value = min(1.0, effective(dict(row), now) + bump)
            conn.execute("UPDATE mind_concern SET intensity=?, touched_at=?, updated_at=? WHERE id=?",
                         (value, now, now, row["id"]))
            warmed.append(int(row["id"]))
    if warmed:
        log_event("mind", "concern_rewarmed", session_id=session_id, trace_id=trace_id, concerns=warmed)
    return warmed
