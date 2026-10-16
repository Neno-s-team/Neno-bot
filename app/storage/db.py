"""SQLite 消息历史与诊断事件存储。

只保留核心表：messages（聊天历史）、chat_stats（基础诊断统计）、
debug_events（trace 日志）。metadata_json 在写入时冻结，事后不回改。
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

DB_DIR = Path("data")
DB_PATH = DB_DIR / "bot.db"


@contextmanager
def get_conn():
    DB_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def fetch_all(query: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(query, tuple(params)).fetchall()


def fetch_one(query: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(query, tuple(params)).fetchone()


def execute_write(query: str, params: Iterable[Any] = ()) -> int:
    with get_conn() as conn:
        cursor = conn.execute(query, tuple(params))
        return cursor.rowcount


def rows_to_dicts(rows: list[sqlite3.Row], fields: list[str]) -> list[dict]:
    return [{field: row[field] for field in fields} for row in rows]


def row_to_dict(row: sqlite3.Row | None, fields: list[str]) -> dict | None:
    if row is None:
        return None
    return {field: row[field] for field in fields}


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                trace_id TEXT,
                message_type TEXT DEFAULT 'text',
                source TEXT DEFAULT 'chat',
                metadata_json TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)"
        )
        message_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "trace_id" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN trace_id TEXT")
        if "message_type" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN message_type TEXT DEFAULT 'text'")
        if "source" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN source TEXT DEFAULT 'chat'")
        if "metadata_json" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN metadata_json TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                source TEXT,
                platform TEXT,
                session_id TEXT,
                session_id_hash TEXT,
                message_len INTEGER,
                reply_len INTEGER,
                success INTEGER,
                latency_ms INTEGER,
                error_type TEXT,
                model TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS debug_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                trace_id TEXT,
                module TEXT,
                event TEXT,
                level TEXT DEFAULT 'info',
                success INTEGER,
                skipped INTEGER,
                action TEXT,
                reason TEXT,
                target_label TEXT,
                candidate_id INTEGER,
                metadata_json TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_debug_events_trace ON debug_events(trace_id, id)"
        )
        # mind 表（P1 骨架）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mind_state (
                session_id TEXT PRIMARY KEY,
                valence REAL NOT NULL DEFAULT 0.0,
                arousal REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0,
                version INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mind_cycle (
                cycle_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                trigger_type TEXT NOT NULL DEFAULT 'message',
                trigger_ref TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                l1_json TEXT,
                plan_json TEXT,
                expr_text TEXT,
                final_text TEXT,
                models_json TEXT DEFAULT '{}',
                error TEXT,
                created_at REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        # P2 加列：老库直接 ALTER TABLE 升级
        cycle_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(mind_cycle)").fetchall()
        }
        for column, ddl in (
            ("l2_json", "l2_json TEXT"),
            ("wm_json", "wm_json TEXT"),
            ("check_json", "check_json TEXT"),
            ("l2_attempts", "l2_attempts INTEGER NOT NULL DEFAULT 0"),
            ("memory_committed", "memory_committed INTEGER NOT NULL DEFAULT 0"),
            ("degraded", "degraded TEXT"),
        ):
            if column not in cycle_columns:
                conn.execute(f"ALTER TABLE mind_cycle ADD COLUMN {ddl}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mind_cycle_session ON mind_cycle(session_id, created_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mind_session_context (
                session_id TEXT PRIMARY KEY,
                sticker_table_text TEXT,
                sticker_names_json TEXT,
                updated_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        # P2 记忆：情景记忆 + 语义记忆 + FTS5 索引（contentless，rowid = 主表 id）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mind_episode (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                note_idx INTEGER NOT NULL DEFAULT 0,
                ts REAL NOT NULL DEFAULT 0.0,
                participants_json TEXT NOT NULL DEFAULT '[]',
                summary TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]',
                valence REAL NOT NULL DEFAULT 0.0,
                arousal REAL NOT NULL DEFAULT 0.0,
                importance REAL NOT NULL DEFAULT 0.0,
                strength REAL NOT NULL DEFAULT 1.0,
                recall_count INTEGER NOT NULL DEFAULT 0,
                last_recalled_at REAL,
                refs_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0,
                UNIQUE(cycle_id, note_idx)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mind_episode_session ON mind_episode(session_id, ts)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mind_belief (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.0,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                updated_at REAL NOT NULL DEFAULT 0.0,
                version INTEGER NOT NULL DEFAULT 1,
                UNIQUE(session_id, subject)
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS mind_episode_fts USING fts5(
                idx_text, tags, tokenize='unicode61', content=''
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS mind_belief_fts USING fts5(
                idx_text, tokenize='unicode61', content=''
            )
            """
        )
        # P3a：感知 / 待想队列 / 投递计划
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mind_perception (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                trace_id TEXT NOT NULL UNIQUE,
                received_at REAL NOT NULL DEFAULT 0.0,
                glance_at REAL NOT NULL DEFAULT 0.0,
                seen TEXT NOT NULL DEFAULT 'none',
                seen_at REAL,
                preview_text TEXT,
                l1_preview_json TEXT,
                retracted INTEGER NOT NULL DEFAULT 0,
                retracted_seen TEXT,
                deferred_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mind_perception_glance"
            " ON mind_perception(status, glance_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mind_deferred (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                trace_ids_json TEXT NOT NULL DEFAULT '[]',
                enqueued_at REAL NOT NULL DEFAULT 0.0,
                due_at REAL NOT NULL DEFAULT 0.0,
                reason TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                lease_until REAL,
                cycle_id TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        deferred_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(mind_deferred)").fetchall()
        }
        if "attempts" not in deferred_columns:
            conn.execute("ALTER TABLE mind_deferred ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        if "payload_json" not in deferred_columns:
            conn.execute("ALTER TABLE mind_deferred ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mind_deferred_due"
            " ON mind_deferred(status, due_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mind_delivery (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                seq INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                send_at REAL NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'pending',
                acked_at REAL,
                error TEXT,
                lease_until REAL,
                created_at REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0,
                UNIQUE(cycle_id, seq)
            )
            """
        )
        delivery_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(mind_delivery)").fetchall()
        }
        if "lease_until" not in delivery_columns:
            conn.execute("ALTER TABLE mind_delivery ADD COLUMN lease_until REAL")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mind_delivery_due"
            " ON mind_delivery(session_id, status, send_at)"
        )
    from app.storage.image_store import init_image_store
    init_image_store()


# ── messages ────────────────────────────────────────────────────────────────


def add_message(
    session_id: str,
    role: str,
    content: str,
    *,
    trace_id: str | None = None,
    message_type: str = "text",
    source: str = "chat",
    metadata: dict[str, Any] | None = None,
) -> int:
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (
                session_id, role, content, trace_id, message_type, source, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                role,
                content,
                trace_id,
                message_type,
                source,
                json.dumps(metadata, ensure_ascii=False) if metadata else None,
            ),
        )
        return int(cursor.lastrowid)


def _decode_json_field(raw: Any) -> dict[str, Any] | None:
    if raw in (None, ""):
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


_MESSAGE_FIELDS = [
    "id",
    "session_id",
    "role",
    "content",
    "trace_id",
    "message_type",
    "source",
    "metadata_json",
    "created_at",
]


def _normalize_message_row(item: dict[str, Any]) -> dict[str, Any]:
    item["metadata"] = _decode_json_field(item.pop("metadata_json", None))
    return item


def list_messages(session_id: str, limit: int, fields: list[str]) -> list[dict]:
    rows = fetch_all(
        f"""
        SELECT {", ".join(fields)}
        FROM messages
        WHERE session_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (session_id, limit),
    )
    items = rows_to_dicts(list(reversed(rows)), fields)
    if "metadata_json" in fields:
        return [_normalize_message_row(item) for item in items]
    return items


def get_recent_messages(session_id: str, limit: int = 8):
    return list_messages(session_id, limit, ["role", "content", "created_at"])


def get_recent_messages_by_tokens(session_id: str, token_limit: int) -> list[dict]:
    """按 token 预算取最近的对话历史（粗估：UTF-8 字节 / 2），时间正序返回。"""
    rows = fetch_all(
        """
        SELECT id, role, content, created_at
        FROM messages
        WHERE session_id = ? AND role IN ('user', 'assistant')
        ORDER BY id DESC
        LIMIT 200
        """,
        (session_id,),
    )
    total = 0
    result: list[dict] = []
    for row in rows:
        content = (row["content"] or "").replace("\n", " ")
        total += len(content.encode("utf-8")) // 2
        result.append({
            "id": row["id"],
            "role": row["role"],
            "content": row["content"],
            "created_at": row["created_at"],
        })
        if total >= token_limit:
            break
    result.reverse()
    return result


def get_session_messages(session_id: str, limit: int = 50):
    return list_messages(session_id, limit, _MESSAGE_FIELDS)


def get_message_by_id(message_id: int) -> dict[str, Any] | None:
    row = fetch_one(
        f"""
        SELECT {", ".join(_MESSAGE_FIELDS)}
        FROM messages
        WHERE id = ?
        LIMIT 1
        """,
        (message_id,),
    )
    if row is None:
        return None
    return _normalize_message_row(row_to_dict(row, _MESSAGE_FIELDS) or {})


def delete_message_turn(message_id: int) -> dict[str, Any] | None:
    """按 trace_id 删除整轮对话（同一 turn 的 user+assistant）。"""
    row = fetch_one(
        "SELECT id, session_id, role, trace_id FROM messages WHERE id = ? LIMIT 1",
        (message_id,),
    )
    if row is None:
        return None

    session_id = row["session_id"]
    from app.storage.operations import operations
    trace_id = row["trace_id"]
    deleted_ids: list[int] = []

    with operations().invalidate(session_id, trace_id=trace_id, cancel_delivery=bool(trace_id)):
        with get_conn() as conn:
            if trace_id:
                rows = conn.execute(
                    """
                    SELECT id FROM messages
                    WHERE session_id = ? AND trace_id = ?
                    ORDER BY id ASC
                    """,
                    (session_id, trace_id),
                ).fetchall()
                deleted_ids = [int(item["id"]) for item in rows]
                conn.execute(
                    "DELETE FROM messages WHERE session_id = ? AND trace_id = ?",
                    (session_id, trace_id),
                )
            else:
                conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
                deleted_ids = [message_id]

        if deleted_ids:
            placeholders = ','.join('?' for _ in deleted_ids)
            execute_write(
                f"DELETE FROM image_assets WHERE session_id=? AND message_id IN ({placeholders})",
                (session_id, *deleted_ids),
            )
    return {
        "message_id": int(row["id"]),
        "session_id": session_id,
        "trace_id": trace_id,
        "deleted_ids": deleted_ids,
        "deleted_count": len(deleted_ids),
        "deleted_scope": "turn" if trace_id else "message",
    }


def clear_session_messages(session_id: str) -> int:
    from app.storage.operations import operations
    with operations().invalidate(session_id):
        with get_conn() as conn:
            conn.execute("DELETE FROM image_assets WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM image_turns WHERE session_id=?", (session_id,))
            return conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,)).rowcount


def get_sessions():
    rows = fetch_all(
        """
        SELECT
            session_id,
            COUNT(*) AS message_count,
            MAX(created_at) AS last_message_at
        FROM messages
        GROUP BY session_id
        ORDER BY last_message_at DESC
        """
    )
    return rows_to_dicts(rows, ["session_id", "message_count", "last_message_at"])


# ── debug_events（trace 诊断） ──────────────────────────────────────────────


def add_debug_event(
    *,
    trace_id: str | None,
    module: str,
    event: str,
    level: str = "info",
    success: bool | None = None,
    skipped: bool | None = None,
    action: str | None = None,
    reason: str | None = None,
    target_label: str | None = None,
    candidate_id: int | None = None,
    metadata_json: str = "{}",
) -> int:
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO debug_events (
                trace_id, module, event, level, success, skipped,
                action, reason, target_label, candidate_id, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace_id,
                module,
                event,
                level or "info",
                None if success is None else 1 if success else 0,
                None if skipped is None else 1 if skipped else 0,
                action,
                (reason or "")[:240] or None,
                target_label,
                candidate_id,
                metadata_json,
            ),
        )
        event_id = int(cursor.lastrowid)
        # 保留最近 1000 条，防止无限膨胀
        conn.execute(
            """
            DELETE FROM debug_events
            WHERE id NOT IN (
                SELECT id FROM debug_events ORDER BY id DESC LIMIT 1000
            )
            """
        )
        return event_id


def list_debug_events(
    *,
    limit: int = 100,
    module: str | None = None,
    event: str | None = None,
    trace_id: str | None = None,
    level: str | None = None,
) -> list[dict]:
    fields = [
        "id",
        "created_at",
        "trace_id",
        "module",
        "event",
        "level",
        "success",
        "skipped",
        "action",
        "reason",
        "metadata_json",
    ]
    bounded_limit = max(1, min(int(limit), 300))
    clauses = []
    params: list[Any] = []
    for column, value in (
        ("module", module),
        ("event", event),
        ("trace_id", trace_id),
        ("level", level),
    ):
        cleaned = (value or "").strip() if isinstance(value, str) else value
        if cleaned:
            clauses.append(f"{column} = ?")
            params.append(cleaned)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(bounded_limit)
    rows = fetch_all(
        f"""
        SELECT {", ".join(fields)}
        FROM debug_events
        {where}
        ORDER BY id DESC
        LIMIT ?
        """,
        tuple(params),
    )
    return rows_to_dicts(rows, fields)


def check_db_connection() -> bool:
    try:
        return fetch_one("SELECT 1 AS ok") is not None
    except Exception:
        return False


def count_recent_error_events(hours: int = 24) -> int:
    try:
        row = fetch_one(
            """
            SELECT COUNT(*) AS count
            FROM debug_events
            WHERE level = 'error' AND created_at >= datetime('now', ?)
            """,
            (f"-{int(hours)} hours",),
        )
        return int(row["count"] or 0) if row else 0
    except Exception:
        return 0
