"""基础诊断统计：每轮聊天的成败/时延落库，供健康检查读取。"""

from __future__ import annotations

from app.config import CHAT_MODEL_NAME
from app.storage.db import execute_write, fetch_one
from app.utils.logging_utils import log_event


def record_chat_stat(
    *,
    source: str,
    session_id: str,
    message: str | None,
    reply: str | None,
    success: bool,
    latency_ms: int,
    error_type: str | None = None,
    model: str | None = None,
) -> None:
    try:
        execute_write(
            """
            INSERT INTO chat_stats (
                source, platform, session_id, message_len, reply_len,
                success, latency_ms, error_type, model
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                source,
                session_id,
                len(message or ""),
                len(reply or ""),
                1 if success else 0,
                latency_ms,
                (error_type or "")[:80] or None,
                model or CHAT_MODEL_NAME,
            ),
        )
    except Exception as exc:
        log_event(
            "stats",
            "stats_write_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )


def get_health_summary() -> dict:
    """健康检查摘要：DB 可用性 + 近 24h 成功率/错误数/平均时延。"""
    try:
        counts = fetch_one(
            """
            SELECT
                COUNT(CASE WHEN created_at >= datetime('now', '-24 hours') THEN 1 END) AS last_24h_turns,
                COUNT(CASE WHEN created_at >= datetime('now', '-24 hours') AND success = 0 THEN 1 END) AS last_24h_errors,
                ROUND(AVG(CASE WHEN created_at >= datetime('now', '-24 hours') THEN latency_ms END)) AS avg_latency_ms_24h,
                MAX(created_at) AS last_turn_at
            FROM chat_stats
            """
        )
    except Exception:
        counts = None

    return {
        "turns_24h": int(counts["last_24h_turns"] or 0) if counts else 0,
        "errors_24h": int(counts["last_24h_errors"] or 0) if counts else 0,
        "avg_latency_ms_24h": int(counts["avg_latency_ms_24h"] or 0) if counts else 0,
        "last_turn_at": counts["last_turn_at"] if counts else None,
        "model": CHAT_MODEL_NAME,
    }
