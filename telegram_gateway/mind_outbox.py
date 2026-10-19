"""网关侧 mind_outbox：投递项的本地幂等账本。

同一 core delivery_id 只会执行一次真实发送；core 租约过期后重新下发
同一项时，这里只按本地状态继续推进或补 ack，不会重复 sendMessage。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.storage.operations import operations


class MindOutbox:
    def __init__(self):
        self.store = operations()
        with self.store.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS mind_outbox (
              delivery_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending', local_id INTEGER, final_id INTEGER,
              error TEXT, acked INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
              payload_json TEXT NOT NULL DEFAULT '{}', submitted_at REAL,
              created_at REAL NOT NULL, updated_at REAL NOT NULL);
            """)
            columns = {row["name"] for row in c.execute("PRAGMA table_info(mind_outbox)").fetchall()}
            if "payload_json" not in columns:
                c.execute("ALTER TABLE mind_outbox ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'")
            if "submitted_at" not in columns:
                c.execute("ALTER TABLE mind_outbox ADD COLUMN submitted_at REAL")

    def begin(self, delivery_id: int, session_id: str, kind: str,
              payload_json: str = "{}") -> dict[str, Any] | None:
        """INSERT OR IGNORE；新行返回 None，已有行返回本地状态。"""
        now = time.time()
        with self.store.connect() as c:
            cursor = c.execute(
                """INSERT OR IGNORE INTO mind_outbox(
                     delivery_id, session_id, kind, state, payload_json, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (delivery_id, session_id, kind, "pending", payload_json, now, now),
            )
            if cursor.rowcount:
                return None
            row = c.execute(
                "SELECT * FROM mind_outbox WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            if row is not None and (row["session_id"] != session_id or row["kind"] != kind
                                    or not _same_content(row["payload_json"], payload_json)):
                # 同 id 但会话/类型/内容不符：残留行（core 库被替换、id 重新计数或测试写入），
                # 作废后按新项执行。core 对同一投递重发时内容不变，不会误伤。
                logging.warning("mind outbox stale row replaced delivery=%s", delivery_id)
                c.execute("DELETE FROM mind_outbox WHERE delivery_id=?", (delivery_id,))
                c.execute(
                    """INSERT INTO mind_outbox(
                         delivery_id, session_id, kind, state, payload_json, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (delivery_id, session_id, kind, "pending", payload_json, now, now),
                )
                return None
            return dict(row) if row is not None else None

    def find_by_local_id(self, local_id: int) -> dict[str, Any] | None:
        return self.store.one(
            "SELECT * FROM mind_outbox WHERE local_id=? AND local_id IS NOT NULL LIMIT 1",
            (local_id,),
        )

    def get(self, delivery_id: int) -> dict[str, Any] | None:
        return self.store.one("SELECT * FROM mind_outbox WHERE delivery_id=?", (delivery_id,))

    def mark_sent(self, delivery_id: int, *, local_id: int | None = None,
                  final_id: int | None = None) -> None:
        self.store.write(
            """UPDATE mind_outbox SET state='sent', local_id=COALESCE(?,local_id),
               final_id=COALESCE(?,final_id), updated_at=? WHERE delivery_id=?""",
            (local_id, final_id, time.time(), delivery_id),
        )

    def mark_uncertain(self, delivery_id: int, *, local_id: int | None = None,
                       error: str | None = None) -> None:
        self.store.write(
            """UPDATE mind_outbox SET state='uncertain', local_id=COALESCE(?,local_id),
               error=?, updated_at=? WHERE delivery_id=?""",
            (local_id, error, time.time(), delivery_id),
        )

    def mark_submitting(self, delivery_id: int) -> None:
        # 在交给 TDLib 之前落账；崩溃恢复时 submitting 按 uncertain 处理
        self.store.write(
            """UPDATE mind_outbox SET state='submitting', attempts=attempts+1,
               submitted_at=?, updated_at=? WHERE delivery_id=?""",
            (time.time(), time.time(), delivery_id),
        )

    def set_local_id(self, delivery_id: int, local_id: int) -> None:
        self.store.write(
            "UPDATE mind_outbox SET local_id=?, updated_at=? WHERE delivery_id=?",
            (local_id, time.time(), delivery_id),
        )

    def mark_pending(self, delivery_id: int, *, error: str | None = None) -> None:
        # 429 等可重试错误：还没交给 TDLib，退回 pending 由暂停后的轮询重发
        self.store.write(
            "UPDATE mind_outbox SET state='pending', error=?, updated_at=? WHERE delivery_id=?",
            (error, time.time(), delivery_id),
        )

    def mark_needs_attention(self, delivery_id: int, error: str) -> None:
        self.store.write(
            "UPDATE mind_outbox SET state='needs_attention', error=?, updated_at=? WHERE delivery_id=?",
            (error, time.time(), delivery_id),
        )

    def pending_retries(self) -> list[dict[str, Any]]:
        return self.store.all(
            "SELECT * FROM mind_outbox WHERE acked=0 AND state='pending' AND attempts>0 ORDER BY delivery_id"
        )

    def final_ids(self) -> set[int]:
        rows = self.store.all(
            "SELECT final_id FROM mind_outbox WHERE final_id IS NOT NULL"
        )
        return {int(row["final_id"]) for row in rows}

    def mark_failed(self, delivery_id: int, error: str) -> None:
        self.store.write(
            "UPDATE mind_outbox SET state='failed', error=?, updated_at=? WHERE delivery_id=?",
            (error, time.time(), delivery_id),
        )

    def mark_ack(self, delivery_id: int) -> None:
        self.store.write(
            "UPDATE mind_outbox SET acked=1, updated_at=? WHERE delivery_id=?",
            (time.time(), delivery_id),
        )

    def increment_attempts(self, delivery_id: int) -> None:
        self.store.write(
            "UPDATE mind_outbox SET attempts=attempts+1, updated_at=? WHERE delivery_id=?",
            (time.time(), delivery_id),
        )


def _same_content(stored_json: str | None, new_json: str) -> bool:
    """同一投递的内容是否一致：忽略 send_at；旧版本没存 payload 的行（空）视为一致。"""
    try:
        stored = json.loads(stored_json or "{}")
        new = json.loads(new_json or "{}")
    except ValueError:
        return True
    stored.pop("send_at", None)
    new.pop("send_at", None)
    if not stored:
        return True
    return stored == new
