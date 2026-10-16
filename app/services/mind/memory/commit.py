"""周期结束时的记忆提交：episode + belief + 状态，一个事务内完成。"""

from __future__ import annotations

import json
import time
from typing import Any

from app.services.mind import state
from app.services.mind.deliberation import L2Result
from app.services.mind.memory import importance as importance_rules
from app.services.mind.memory import store as memory_store
from app.services.mind.memory.tokenize import to_index_text
from app.storage.db import get_conn
from app.utils.logging_utils import log_event


def commit_memory(
    *,
    session_id: str,
    cycle_id: str,
    result: L2Result,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """把 L2 的 memory_notes / belief_updates / state_delta 落库；幂等。

    episode 依赖 UNIQUE(cycle_id, note_idx) 幂等；belief 按 (session_id, subject)
    upsert；状态走 state.apply_affect_delta（唯一写入入口）。
    事务只覆盖数据库写入；状态写入自带乐观锁，放在事务外。
    """
    current = state.read_state(session_id)
    # 事务外预读：写事务持有 RESERVED 锁时，同库第二条连接读取容易撞 locked
    importance_scores = [
        importance_rules.score_importance(
            session_id,
            note.to_dict(),
            valence=current["valence"],
            arousal=current["arousal"],
            exclude_cycle_id=cycle_id,
        )
        for note in result.memory_notes
    ]
    existing_beliefs = {
        update.subject: memory_store.get_belief(session_id, update.subject)
        for update in result.belief_updates
    }
    episode_ids: list[int] = []
    with get_conn() as conn:
        for note_idx, note in enumerate(result.memory_notes):
            cursor = conn.execute(
                """INSERT INTO mind_episode(
                     session_id, cycle_id, note_idx, ts, participants_json, summary,
                     tags_json, valence, arousal, importance, strength, refs_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?)
                   ON CONFLICT(cycle_id, note_idx) DO NOTHING""",
                (
                    session_id, cycle_id, note_idx, time.time(),
                    json.dumps(note.participants, ensure_ascii=False), note.summary,
                    json.dumps(note.tags, ensure_ascii=False),
                    current["valence"], current["arousal"], importance_scores[note_idx],
                    json.dumps({"trace_id": trace_id, "cycle_id": cycle_id}, ensure_ascii=False),
                    time.time(),
                ),
            )
            if cursor.rowcount == 0:
                continue
            episode_id = int(cursor.lastrowid)
            episode_ids.append(episode_id)
            conn.execute(
                "INSERT INTO mind_episode_fts(rowid, idx_text, tags) VALUES (?, ?, ?)",
                (
                    episode_id,
                    to_index_text(note.summary),
                    to_index_text(" ".join(note.tags)),
                ),
            )

        for update in result.belief_updates:
            existing = existing_beliefs.get(update.subject)
            if existing is None:
                new_confidence = max(0.0, min(1.0, update.confidence))
                cursor = conn.execute(
                    """INSERT INTO mind_belief(
                         session_id, subject, content, confidence, evidence_json, updated_at, version)
                       VALUES (?, ?, ?, ?, ?, ?, 1)
                       ON CONFLICT(session_id, subject) DO NOTHING""",
                    (session_id, update.subject, update.content, new_confidence,
                     json.dumps(update.evidence, ensure_ascii=False), time.time()),
                )
                if cursor.rowcount:
                    conn.execute(
                        "INSERT INTO mind_belief_fts(rowid, idx_text) VALUES (?, ?)",
                        (int(cursor.lastrowid),
                         to_index_text(f"{update.subject} {update.content}")),
                    )
                continue

            # confidence 单次变化 ≤ ±0.3
            min_confidence = max(0.0, float(existing["confidence"]) - 0.3)
            max_confidence = min(1.0, float(existing["confidence"]) + 0.3)
            new_confidence = max(min_confidence, min(max_confidence, update.confidence))
            merged_evidence = list(dict.fromkeys([*existing["evidence"], *update.evidence]))
            conn.execute(
                "INSERT INTO mind_belief_fts(mind_belief_fts, rowid, idx_text)"
                " VALUES ('delete', ?, ?)",
                (existing["id"], to_index_text(f"{existing['subject']} {existing['content']}")),
            )
            conn.execute(
                """UPDATE mind_belief
                   SET content = ?, confidence = ?, evidence_json = ?,
                       updated_at = ?, version = version + 1
                   WHERE id = ?""",
                (update.content, new_confidence,
                 json.dumps(merged_evidence, ensure_ascii=False),
                 time.time(), existing["id"]),
            )
            conn.execute(
                "INSERT INTO mind_belief_fts(rowid, idx_text) VALUES (?, ?)",
                (existing["id"], to_index_text(f"{update.subject} {update.content}")),
            )

    if result.state_delta_valence or result.state_delta_arousal:
        state.apply_affect_delta(
            session_id, result.state_delta_valence, result.state_delta_arousal
        )
    log_event(
        "mind",
        "memory_committed",
        trace_id=trace_id,
        session_id=session_id,
        episodes=len(episode_ids),
        beliefs=len(result.belief_updates),
    )
    return {"episode_ids": episode_ids}
