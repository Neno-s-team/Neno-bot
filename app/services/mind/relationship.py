"""她和对方的关系：会变，但只被两人之间的相处推动。

三个量（0–1）：亲近（熟不熟、愿不愿意分享）、心动（有没有那方面的在意）、隔阂（不舒服、
疏远）。初值来自"很好的朋友"。只有夜间反思会挪它：模型说今天的相处让她更近/更远/有点心动，
规则限幅（一晚每项 ≤ ±0.08）并按 seed 抽一个打折（0.5–1），所以变化慢、方向不预设。
世界事件和 NPC 不碰这里（关系锁防的是第三方，不是两人之间）。

给大脑的只有文字描述，不给数值。
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.services.mind import clock
from app.storage.db import fetch_one, get_conn

_INITIAL = {"closeness": 0.7, "attraction": 0.1, "distance": 0.1}
_MAX_STEP = 0.08


def _draw(*parts: str) -> float:
    raw = "\0".join(parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") / 2**64


def get(session_id: str) -> dict[str, Any]:
    row = fetch_one("SELECT * FROM mind_relationship WHERE session_id = ?", (session_id,))
    if row is None:
        return {"session_id": session_id, **_INITIAL, "view": "", "updated_at": None}
    return dict(row)


def describe(session_id: str) -> str:
    rel = get(session_id)
    c, a, d = rel["closeness"], rel["attraction"], rel["distance"]
    if d >= 0.6:
        base = "最近和他有点疏远，不太想多说"
    elif c >= 0.8:
        base = "很亲近的朋友，什么都能说"
    elif c >= 0.55:
        base = "很好的朋友"
    elif c >= 0.35:
        base = "关系还行的朋友"
    else:
        base = "不算太熟"
    if a >= 0.6:
        base += "；对他有明显的心动，自己也知道"
    elif a >= 0.35:
        base += "；对他有一点说不清的在意"
    if 0.3 <= d < 0.6:
        base += "；心里有点小疙瘩"
    if rel.get("view"):
        base += f"。她最近对他的看法：{rel['view']}"
    return base


def nudge(conn, session_id: str, night: str, change: dict[str, Any] | None) -> dict[str, float] | None:
    """夜间反思后挪一点。change 来自模型：closer / attraction / distance 各 -1..1，view 一句话。"""
    if not change:
        return None
    row = conn.execute("SELECT * FROM mind_relationship WHERE session_id = ?", (session_id,)).fetchone()
    current = dict(row) if row else {**_INITIAL, "view": ""}
    scale = 0.5 + 0.5 * _draw(session_id, night, "relation")
    updated = {}
    for key, field in (("closer", "closeness"), ("attraction", "attraction"), ("distance", "distance")):
        raw = max(-1.0, min(1.0, float(change.get(key) or 0.0)))
        updated[field] = max(0.0, min(1.0, float(current[field]) + raw * _MAX_STEP * scale))
    view = str(change.get("view") or current.get("view") or "")[:80]
    now = clock.now()
    conn.execute(
        """INSERT INTO mind_relationship(session_id, closeness, attraction, distance, view, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET closeness=excluded.closeness, attraction=excluded.attraction,
             distance=excluded.distance, view=excluded.view, updated_at=excluded.updated_at""",
        (session_id, updated["closeness"], updated["attraction"], updated["distance"], view, now),
    )
    return {k: round(v, 3) for k, v in updated.items()}


def ensure(session_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO mind_relationship(session_id, closeness, attraction, distance, view, updated_at)
               VALUES (?, ?, ?, ?, '', ?) ON CONFLICT(session_id) DO NOTHING""",
            (session_id, _INITIAL["closeness"], _INITIAL["attraction"], _INITIAL["distance"], clock.now()),
        )
