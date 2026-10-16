"""周期结束时的记忆提交：episode + belief + 状态 + 幂等标记，一个事务内完成。"""

from __future__ import annotations

import json
import time
from typing import Any

from app import config
from app.services.mind import state
from app.services.mind.deliberation import L2Result
from app.services.mind.memory import importance as importance_rules
from app.services.mind.memory.tokenize import to_index_text
from app.storage.db import get_conn
from app.utils.logging_utils import log_event

_MAX_COMMIT_RETRIES = 2


class _VersionConflict(Exception):
    """内部信号：情绪乐观锁冲突，回滚事务后重试。"""


class _AlreadyCommitted(Exception):
    """内部信号：并发提交已完成，本次事务作废。"""


def commit_memory(
    *,
    session_id: str,
    cycle_id: str,
    result: L2Result,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """把 L2 的 memory_notes / belief_updates / state_delta 落库；幂等。

    episode、belief、情绪、memory_committed 标记全在同一个 conn 事务里；
    事务开头检查 memory_committed，已提交就直接返回（支持"提交成功后崩溃重跑"）。
    情绪走带版本号的 UPDATE；版本冲突回滚整个事务并重试，最多 _MAX_COMMIT_RETRIES 次。
    """
    for _ in range(_MAX_COMMIT_RETRIES + 1):
        try:
            with get_conn() as conn:
                committed = conn.execute(
                    "SELECT memory_committed FROM mind_cycle WHERE cycle_id = ?",
                    (cycle_id,),
                ).fetchone()
                if committed is not None and int(committed["memory_committed"] or 0) == 1:
                    return {"episode_ids": [], "skipped": "already_committed"}

                raw_state = conn.execute(
                    "SELECT session_id, valence, arousal, updated_at, version"
                    " FROM mind_state WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                current_valence, current_arousal = state.decayed_from_raw(
                    dict(raw_state) if raw_state is not None else None
                )

                # 事务内预读：重要性规则的"第一次发生"要读历史 episode
                importance_scores = [
                    importance_rules.score_importance(
                        session_id,
                        note.to_dict(),
                        valence=current_valence,
                        arousal=current_arousal,
                        exclude_cycle_id=cycle_id,
                    )
                    for note in result.memory_notes
                ]
                existing_beliefs = {
                    row["subject"]: dict(row)
                    for row in conn.execute(
                        "SELECT id, subject, content, confidence, evidence_json, version"
                        " FROM mind_belief WHERE session_id = ?",
                        (session_id,),
                    ).fetchall()
                }

                episode_ids: list[int] = []
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
                            current_valence, current_arousal, importance_scores[note_idx],
                            json.dumps({"trace_id": trace_id, "cycle_id": cycle_id},
                                       ensure_ascii=False),
                            time.time(),
                        ),
                    )
                    if cursor.rowcount == 0:
                        continue
                    episode_id = int(cursor.lastrowid)
                    episode_ids.append(episode_id)
                    conn.execute(
                        "INSERT INTO mind_episode_fts(rowid, idx_text, tags) VALUES (?, ?, ?)",
                        (episode_id, to_index_text(note.summary),
                         to_index_text(" ".join(note.tags))),
                    )

                for update in result.belief_updates:
                    existing = existing_beliefs.get(update.subject)
                    if existing is None:
                        # 首插：confidence = min(模型值, MIND_BELIEF_INIT_MAX)
                        new_confidence = min(
                            max(0.0, min(1.0, update.confidence)),
                            float(config.MIND_BELIEF_INIT_MAX),
                        )
                        cursor = conn.execute(
                            """INSERT INTO mind_belief(
                                 session_id, subject, content, confidence, evidence_json,
                                 updated_at, version)
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
                    existing_confidence = float(existing["confidence"])
                    min_confidence = max(0.0, existing_confidence - 0.3)
                    max_confidence = min(1.0, existing_confidence + 0.3)
                    new_confidence = max(min_confidence, min(max_confidence, update.confidence))
                    existing_evidence = json.loads(existing["evidence_json"] or "[]")
                    merged_evidence = list(dict.fromkeys([*existing_evidence, *update.evidence]))
                    conn.execute(
                        "INSERT INTO mind_belief_fts(mind_belief_fts, rowid, idx_text)"
                        " VALUES ('delete', ?, ?)",
                        (existing["id"],
                         to_index_text(f"{existing['subject']} {existing['content']}")),
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
                    written = state.apply_affect_delta_in_conn(
                        conn, session_id,
                        result.state_delta_valence, result.state_delta_arousal,
                    )
                    if written is None:
                        # 版本冲突：回滚整个事务（含 episode/belief），下一轮重试
                        raise _VersionConflict()

                marked = conn.execute(
                    """UPDATE mind_cycle SET memory_committed = 1
                       WHERE cycle_id = ? AND memory_committed = 0""",
                    (cycle_id,),
                ).rowcount
                if marked == 0:
                    current = conn.execute(
                        "SELECT memory_committed FROM mind_cycle WHERE cycle_id = ?",
                        (cycle_id,),
                    ).fetchone()
                    if current is not None:
                        # 并发已提交：本次事务作废
                        raise _AlreadyCommitted()
                    # 行不存在（例如直接从测试/脚本调用）：补一行标记，保持幂等
                    conn.execute(
                        """INSERT INTO mind_cycle(
                             cycle_id, session_id, trigger_type, trigger_ref, status,
                             memory_committed, created_at, updated_at)
                           VALUES (?, ?, 'message', ?, 'completed', 1, ?, ?)""",
                        (cycle_id, session_id, trace_id, time.time(), time.time()),
                    )
        except _VersionConflict:
            continue
        except _AlreadyCommitted:
            return {"episode_ids": [], "skipped": "already_committed"}
        else:
            log_event(
                "mind",
                "memory_committed",
                trace_id=trace_id,
                session_id=session_id,
                episodes=len(episode_ids),
                beliefs=len(result.belief_updates),
            )
            return {"episode_ids": episode_ids}

    log_event(
        "mind",
        "memory_commit_conflict",
        trace_id=trace_id,
        session_id=session_id,
        retries=_MAX_COMMIT_RETRIES,
    )
    return {"episode_ids": [], "skipped": "conflict"}
