from fastapi import APIRouter

from app import config
from app.services.stats_service import get_health_summary
from app.storage.db import check_db_connection, count_recent_error_events

router = APIRouter()


@router.get("/")
def root():
    return {"status": "ok"}


@router.get("/health")
def health():
    """最小健康检查：进程存活 + SQLite 可用 + 近 24h 错误数。"""
    db_ok = check_db_connection()
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "recent_errors_24h": count_recent_error_events(24),
        "chat": {
            "persona_enabled": config.CHAT_PERSONA_ENABLED,
            "image_history_turns": config.IMAGE_HISTORY_TURNS,
            "image_archive_model": config.IMAGE_ARCHIVE_MODEL_NAME,
            "image_recall_enabled": True,
        },
        **get_health_summary(),
    }
