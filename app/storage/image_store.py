"""Image references survive prompt trimming; explicit user deletion removes references."""

from __future__ import annotations

import time

from app.storage import db


def init_image_store() -> None:
    with db.get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS image_turns (
                session_id TEXT PRIMARY KEY,
                completed_turns INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS image_assets (
                image_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                message_id INTEGER,
                turn_index INTEGER,
                file_path TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                caption TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                archive_state TEXT NOT NULL DEFAULT 'pending',
                retry_after REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_image_assets_session
                ON image_assets(session_id, turn_index, message_id);
            """
        )


def next_turn(session_id: str) -> int:
    with db.get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO image_turns(session_id, completed_turns)
               SELECT ?, COUNT(*) FROM messages WHERE session_id=? AND role='assistant'""",
            (session_id, session_id),
        )
        return int(conn.execute(
            "SELECT completed_turns FROM image_turns WHERE session_id=?", (session_id,)
        ).fetchone()[0]) + 1


def complete_turn(session_id: str, turn_index: int) -> None:
    db.execute_write(
        "UPDATE image_turns SET completed_turns=MAX(completed_turns,?) WHERE session_id=?",
        (turn_index, session_id),
    )


def add_image(asset: dict) -> None:
    db.execute_write(
        """INSERT INTO image_assets
           (image_id,session_id,file_path,mime_type,sha256,caption) VALUES (?,?,?,?,?,?)""",
        tuple(asset[key] for key in
              ("image_id", "session_id", "file_path", "mime_type", "sha256", "caption")),
    )


def bind_image(session_id: str, image_id: str, message_id: int, turn_index: int) -> None:
    count = db.execute_write(
        """UPDATE image_assets SET message_id=?,turn_index=?
           WHERE session_id=? AND image_id=? AND message_id IS NULL""",
        (message_id, turn_index, session_id, image_id),
    )
    if count != 1:
        raise ValueError("Image reference is unavailable in this session")


def get_image(session_id: str, image_id: str, *, include_pending: bool = False) -> dict | None:
    query = "SELECT * FROM image_assets WHERE session_id=? AND image_id=?"
    if not include_pending:
        query += " AND turn_index IS NOT NULL"
    row = db.fetch_one(query, (session_id, image_id))
    return dict(row) if row else None


def recent_images(session_id: str, current_turn: int, keep_turns: int) -> list[dict]:
    return [dict(row) for row in db.fetch_all(
        """SELECT * FROM image_assets WHERE session_id=? AND turn_index>?
           ORDER BY message_id,image_id""", (session_id, current_turn - keep_turns)
    )]


def images_for_messages(session_id: str, message_ids: list[int]) -> list[dict]:
    if not message_ids:
        return []
    placeholders = ','.join('?' for _ in message_ids)
    return [dict(row) for row in db.fetch_all(
        f"SELECT * FROM image_assets WHERE session_id=? AND message_id IN ({placeholders}) ORDER BY message_id,image_id",
        (session_id, *message_ids),
    )]


def archive_candidates(session_id: str, current_turn: int, keep_turns: int) -> list[dict]:
    return [dict(row) for row in db.fetch_all(
        """SELECT * FROM image_assets WHERE session_id=? AND turn_index<=?
           AND archive_state='pending' AND retry_after<=?
           ORDER BY turn_index DESC,message_id DESC LIMIT 8""",
        (session_id, current_turn - keep_turns, time.time()),
    )]


def save_description(session_id: str, image_id: str, description: str, *, only_pending: bool = False) -> None:
    db.execute_write(
        """UPDATE image_assets SET description=?,archive_state='ready',retry_after=0
           WHERE session_id=? AND image_id=?""" + (" AND archive_state='pending'" if only_pending else ''),
        (description.strip()[:2400], session_id, image_id),
    )


def defer_description(session_id: str, image_id: str) -> None:
    db.execute_write(
        "UPDATE image_assets SET retry_after=? WHERE session_id=? AND image_id=?",
        (time.time() + 300, session_id, image_id),
    )


def search_images(session_id: str, query: str = '', limit: int = 6, offset: int = 0) -> list[dict]:
    clauses = ["session_id=?", "turn_index IS NOT NULL"]
    params: list = [session_id]
    for word in query.strip().split()[:8]:
        escaped = word[:200].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        clauses.append("(caption LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\' OR image_id LIKE ? ESCAPE '\\')")
        params.extend([f'%{escaped}%'] * 3)
    params.extend((max(1, min(int(limit), 20)), max(0, int(offset))))
    return [dict(row) for row in db.fetch_all(
        "SELECT * FROM image_assets WHERE " + ' AND '.join(clauses) +
        " ORDER BY turn_index DESC,message_id DESC LIMIT ? OFFSET ?", params
    )]


def has_images(session_id: str) -> bool:
    return db.fetch_one(
        "SELECT 1 FROM image_assets WHERE session_id=? AND turn_index IS NOT NULL LIMIT 1",
        (session_id,),
    ) is not None
