"""网关侧 mind_outbox：投递项的本地幂等账本。

同一 core delivery_id 只会执行一次真实发送；core 租约过期后重新下发
同一项时，这里只按本地状态继续推进或补 ack，不会重复 sendMessage。
"""

from __future__ import annotations

import json
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
              created_at REAL NOT NULL, updated_at REAL NOT NULL);
            """)

    def begin(self, delivery_id: int, session_id: str, kind: str) -> dict[str, Any] | None:
        """INSERT OR IGNORE；新行返回 None，已有行返回本地状态。"""
        now = time.time()
        with self.store.connect() as c:
            cursor = c.execute(
                """INSERT OR IGNORE INTO mind_outbox(
                     delivery_id, session_id, kind, state, created_at, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (delivery_id, session_id, kind, "pending", now, now),
            )
            if cursor.rowcount:
                return None
            row = c.execute(
                "SELECT * FROM mind_outbox WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            return dict(row) if row is not None else None

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
