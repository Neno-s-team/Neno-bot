"""mind_episode / mind_belief 的读写与 FTS 同步。

FTS 表是 contentless（content=''），rowid 与主表 id 一一对应；
删除/更新时必须用 'delete' 命令带上旧索引文本，否则索引会残留。
"""

from __future__ import annotations

import json
import time
from typing import Any

from app.storage.db import execute_write, fetch_all, fetch_one, get_conn
from app.services.mind.memory.tokenize import to_index_text


_EPISODE_FIELDS = [
    "id", "session_id", "cycle_id", "note_idx", "ts", "participants_json",
    "summary", "tags_json", "valence", "arousal", "importance", "strength",
    "recall_count", "last_recalled_at", "refs_json", "created_at",
]


def _episode_row(row) -> dict[str, Any]:
    item = {field: row[field] for field in _EPISODE_FIELDS}
    item["participants"] = json.loads(item.pop("participants_json") or "[]")
    item["tags"] = json.loads(item.pop("tags_json") or "[]")
    item["refs"] = json.loads(item.pop("refs_json") or "{}")
    return item


def get_episode(session_id: str, episode_id: int) -> dict[str, Any] | None:
    row = fetch_one(
        f"SELECT {', '.join(_EPISODE_FIELDS)} FROM mind_episode"
        " WHERE session_id = ? AND id = ?",
        (session_id, episode_id),
    )
    return _episode_row(row) if row is not None else None


def get_episode_by_cycle(session_id: str, cycle_id: str, note_idx: int) -> dict[str, Any] | None:
    row = fetch_one(
        f"SELECT {', '.join(_EPISODE_FIELDS)} FROM mind_episode"
        " WHERE session_id = ? AND cycle_id = ? AND note_idx = ?",
        (session_id, cycle_id, note_idx),
    )
    return _episode_row(row) if row is not None else None


def list_episodes(session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = fetch_all(
        f"SELECT {', '.join(_EPISODE_FIELDS)} FROM mind_episode"
        " WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    )
    return [_episode_row(row) for row in rows]


def count_episodes(session_id: str) -> int:
    row = fetch_one(
        "SELECT COUNT(*) AS n FROM mind_episode WHERE session_id = ?",
        (session_id,),
    )
    return int(row["n"]) if row is not None else 0


def existing_episode_ids(session_id: str, episode_ids: list[int]) -> set[int]:
    """返回属于该 session 且真实存在的 episode id 集合。"""
    cleaned = [int(item) for item in episode_ids]
    if not cleaned:
        return set()
    placeholders = ",".join("?" for _ in cleaned)
    rows = fetch_all(
        f"SELECT id FROM mind_episode WHERE session_id = ? AND id IN ({placeholders})",
        (session_id, *cleaned),
    )
    return {int(row["id"]) for row in rows}


def insert_episode(
    session_id: str,
    cycle_id: str,
    note_idx: int,
    *,
    summary: str,
    participants: list[str] | None = None,
    tags: list[str] | None = None,
    valence: float = 0.0,
    arousal: float = 0.0,
    importance: float = 0.0,
    strength: float = 1.0,
    refs: dict[str, Any] | None = None,
    ts: float | None = None,
) -> int:
    """写入 episode 并同步 FTS。UNIQUE(cycle_id, note_idx) 保证幂等。"""
    now = time.time()
    index_text = to_index_text(summary)
    tags_text = to_index_text(" ".join(tags or []))
    with get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO mind_episode(
                 session_id, cycle_id, note_idx, ts, participants_json, summary,
                 tags_json, valence, arousal, importance, strength, refs_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(cycle_id, note_idx) DO NOTHING""",
            (
                session_id, cycle_id, note_idx, now if ts is None else ts,
                json.dumps(participants or [], ensure_ascii=False), summary,
                json.dumps(tags or [], ensure_ascii=False),
                valence, arousal, importance, strength,
                json.dumps(refs or {}, ensure_ascii=False), now,
            ),
        )
        if cursor.rowcount == 0:
            return 0
        episode_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO mind_episode_fts(rowid, idx_text, tags) VALUES (?, ?, ?)",
            (episode_id, index_text, tags_text),
        )
        return episode_id


def update_episode_memory(
    episode_id: int,
    *,
    strength: float | None = None,
    recall_count: int | None = None,
    last_recalled_at: float | None = None,
) -> None:
    """回忆增强：只动强度/回忆计数，不重写 FTS（summary 未变）。"""
    fields: list[str] = []
    params: list[Any] = []
    if strength is not None:
        fields.append("strength = ?")
        params.append(strength)
    if recall_count is not None:
        fields.append("recall_count = ?")
        params.append(recall_count)
    if last_recalled_at is not None:
        fields.append("last_recalled_at = ?")
        params.append(last_recalled_at)
    if not fields:
        return
    params.append(episode_id)
    execute_write(f"UPDATE mind_episode SET {', '.join(fields)} WHERE id = ?", tuple(params))


def update_episode_summary(episode_id: int, summary: str, tags: list[str] | None = None) -> None:
    """改 summary/tags 时同步重写 FTS（先删旧索引再插新索引）。"""
    row = fetch_one(
        "SELECT summary, tags_json FROM mind_episode WHERE id = ?", (episode_id,),
    )
    if row is None:
        return
    old_text = to_index_text(row["summary"])
    old_tags = to_index_text(" ".join(json.loads(row["tags_json"] or "[]")))
    new_tags = json.loads(row["tags_json"] or "[]") if tags is None else list(tags)
    new_tags_text = to_index_text(" ".join(new_tags))
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO mind_episode_fts(mind_episode_fts, rowid, idx_text, tags)"
            " VALUES ('delete', ?, ?, ?)",
            (episode_id, old_text, old_tags),
        )
        conn.execute(
            "UPDATE mind_episode SET summary = ?, tags_json = ? WHERE id = ?",
            (summary, json.dumps(new_tags, ensure_ascii=False), episode_id),
        )
        conn.execute(
            "INSERT INTO mind_episode_fts(rowid, idx_text, tags) VALUES (?, ?, ?)",
            (episode_id, to_index_text(summary), new_tags_text),
        )


def delete_episode(episode_id: int) -> None:
    row = fetch_one(
        "SELECT summary, tags_json FROM mind_episode WHERE id = ?", (episode_id,),
    )
    if row is None:
        return
    old_text = to_index_text(row["summary"])
    old_tags = to_index_text(" ".join(json.loads(row["tags_json"] or "[]")))
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO mind_episode_fts(mind_episode_fts, rowid, idx_text, tags)"
            " VALUES ('delete', ?, ?, ?)",
            (episode_id, old_text, old_tags),
        )
        conn.execute("DELETE FROM mind_episode WHERE id = ?", (episode_id,))


def search_episodes(session_id: str, query: str, limit: int = 30) -> list[dict[str, Any]]:
    """FTS 候选：返回 episode 行 + bm25 分数（越小越相关）。"""
    if not query:
        return []
    rows = fetch_all(
        f"""SELECT e.{', e.'.join(_EPISODE_FIELDS)},
                   bm25(mind_episode_fts) AS rank
            FROM mind_episode_fts
            JOIN mind_episode e ON e.id = mind_episode_fts.rowid
            WHERE mind_episode_fts MATCH ? AND e.session_id = ?
            ORDER BY rank
            LIMIT ?""",
        (query, session_id, limit),
    )
    result = []
    for row in rows:
        item = _episode_row(row)
        item["rank"] = float(row["rank"])
        result.append(item)
    return result


# ── belief ──────────────────────────────────────────────────────────────────


_BELIEF_FIELDS = [
    "id", "session_id", "subject", "content", "confidence", "evidence_json",
    "updated_at", "version",
]


def _belief_row(row) -> dict[str, Any]:
    item = {field: row[field] for field in _BELIEF_FIELDS}
    item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
    return item


def get_belief(session_id: str, subject: str) -> dict[str, Any] | None:
    row = fetch_one(
        f"SELECT {', '.join(_BELIEF_FIELDS)} FROM mind_belief"
        " WHERE session_id = ? AND subject = ?",
        (session_id, subject),
    )
    return _belief_row(row) if row is not None else None


def get_belief_by_id(belief_id: int) -> dict[str, Any] | None:
    row = fetch_one(
        f"SELECT {', '.join(_BELIEF_FIELDS)} FROM mind_belief WHERE id = ?",
        (belief_id,),
    )
    return _belief_row(row) if row is not None else None


def upsert_belief(
    session_id: str,
    subject: str,
    content: str,
    confidence: float,
    evidence: list[int],
    *,
    max_confidence_step: float = 0.3,
) -> dict[str, Any]:
    """按 (session_id, subject) 插入或更新；confidence 单次变化 ≤ max_confidence_step。"""
    now = time.time()
    existing = get_belief(session_id, subject)
    with get_conn() as conn:
        if existing is None:
            index_text = to_index_text(f"{subject} {content}")
            cursor = conn.execute(
                """INSERT INTO mind_belief(
                     session_id, subject, content, confidence, evidence_json, updated_at, version)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (session_id, subject, content, confidence,
                 json.dumps(evidence, ensure_ascii=False), now),
            )
            belief_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO mind_belief_fts(rowid, idx_text) VALUES (?, ?)",
                (belief_id, index_text),
            )
            return {
                "id": belief_id, "session_id": session_id, "subject": subject,
                "content": content, "confidence": confidence, "evidence": evidence,
                "updated_at": now, "version": 1,
            }

        min_confidence = max(0.0, existing["confidence"] - max_confidence_step)
        max_confidence = min(1.0, existing["confidence"] + max_confidence_step)
        new_confidence = max(min_confidence, min(max_confidence, confidence))
        merged_evidence = list(dict.fromkeys([*existing["evidence"], *evidence]))
        new_text = to_index_text(f"{subject} {content}")
        old_text = to_index_text(f"{existing['subject']} {existing['content']}")
        conn.execute(
            "INSERT INTO mind_belief_fts(mind_belief_fts, rowid, idx_text)"
            " VALUES ('delete', ?, ?)",
            (existing["id"], old_text),
        )
        conn.execute(
            """UPDATE mind_belief
               SET content = ?, confidence = ?, evidence_json = ?,
                   updated_at = ?, version = version + 1
               WHERE id = ?""",
            (content, new_confidence, json.dumps(merged_evidence, ensure_ascii=False),
             now, existing["id"]),
        )
        conn.execute(
            "INSERT INTO mind_belief_fts(rowid, idx_text) VALUES (?, ?)",
            (existing["id"], new_text),
        )
        return {
            "id": existing["id"], "session_id": session_id, "subject": subject,
            "content": content, "confidence": new_confidence,
            "evidence": merged_evidence, "updated_at": now,
            "version": existing["version"] + 1,
        }


def list_beliefs(session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = fetch_all(
        f"SELECT {', '.join(_BELIEF_FIELDS)} FROM mind_belief"
        " WHERE session_id = ? ORDER BY confidence DESC, updated_at DESC LIMIT ?",
        (session_id, limit),
    )
    return [_belief_row(row) for row in rows]


def search_beliefs(session_id: str, query: str, limit: int = 30) -> list[dict[str, Any]]:
    if not query:
        return []
    rows = fetch_all(
        f"""SELECT b.{', b.'.join(_BELIEF_FIELDS)},
                   bm25(mind_belief_fts) AS rank
            FROM mind_belief_fts
            JOIN mind_belief b ON b.id = mind_belief_fts.rowid
            WHERE mind_belief_fts MATCH ? AND b.session_id = ?
            ORDER BY rank
            LIMIT ?""",
        (query, session_id, limit),
    )
    result = []
    for row in rows:
        item = _belief_row(row)
        item["rank"] = float(row["rank"])
        result.append(item)
    return result
