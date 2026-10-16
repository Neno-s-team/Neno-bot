import json
import os
import tempfile
from pathlib import Path

from app.prompt.prompt_loader import load_text


def _env_bool(key: str, default: bool = False) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key, str(default)).strip()
    return int(raw)


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key, str(default)).strip()
    return float(raw)


def _cors_origins() -> tuple[str, ...]:
    origins = tuple(
        value.strip()
        for value in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    )
    if "*" in origins:
        raise RuntimeError("CORS_ALLOWED_ORIGINS must contain explicit origins, not '*'")
    return origins


# ── LLM provider ────────────────────────────────────────────────────────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_URL = (
    os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions").strip()
    or "https://openrouter.ai/api/v1/chat/completions"
)
OPENROUTER_PROXY = os.getenv("OPENROUTER_PROXY", "").strip() or None
CHAT_MODEL_NAME = os.getenv("OPENROUTER_CHAT_MODEL", "openai/gpt-4o-mini")
VISION_MODEL_NAME = os.getenv("OPENROUTER_VISION_MODEL", CHAT_MODEL_NAME)
IMAGE_ARCHIVE_MODEL_NAME = os.getenv("IMAGE_ARCHIVE_MODEL", "").strip() or VISION_MODEL_NAME
IMAGE_ARCHIVE_EXTRA_BODY = json.loads(os.getenv("IMAGE_ARCHIVE_EXTRA_BODY", "") or "null") or {}

# 回复长度上限：兜住模型偶发跑飞（脑补一长串假对话），顺便省 token。
CHAT_MAX_TOKENS = _env_int("CHAT_MAX_TOKENS", 220)
# 模型偶发会吐出对话轮次标记（如换行后的 "Assistant"/"Human"）；
# stop 序列让生成一碰到这些标记就立即停。
CHAT_STOP_SEQUENCES = ["\nAssistant", "\nHuman", "\nassistant", "\nUser"]
LLM_TIMEOUT_SECONDS = _env_int("LLM_TIMEOUT_SECONDS", 60)
# 厂商特定请求体补充（JSON）：fable-5.1 的思考默认 high 力度，
# {"thinking":{"type":"disabled"}} 可把 60s+ 的思考压到几秒。
LLM_EXTRA_BODY = json.loads(os.getenv("LLM_EXTRA_BODY", "") or "null") or {}

# ── 聊天历史 ────────────────────────────────────────────────────────────────
# 送入 prompt 的近期历史 token 预算（粗估：UTF-8 字节 / 2）。
HISTORY_TOKEN_LIMIT = _env_int("HISTORY_TOKEN_LIMIT", 500)
IMAGE_HISTORY_TURNS = max(1, _env_int("IMAGE_HISTORY_TURNS", 5))
IMAGE_TOOL_MAX_ROUNDS = max(1, _env_int("IMAGE_TOOL_MAX_ROUNDS", 3))
IMAGE_TOOL_TOTAL_TIMEOUT_SECONDS = max(30, _env_int("IMAGE_TOOL_TOTAL_TIMEOUT_SECONDS", 210))
IMAGE_MAX_PER_MESSAGE = 8
IMAGE_MAX_SOURCE_BYTES = 20 * 1024 * 1024

# ── Session 并发保护 ────────────────────────────────────────────────────────
# 同一 session 的突发消息先进入聚合窗口，窗口关闭后合并为一个 turn 提交。
SESSION_AGGREGATION_WINDOW_SECONDS = _env_float("SESSION_AGGREGATION_WINDOW_SECONDS", 3.0)

# ── 安全 ────────────────────────────────────────────────────────────────────
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
INTERNAL_TOKEN = os.getenv("NENO_INTERNAL_TOKEN", "").strip()
CORS_ALLOWED_ORIGINS = _cors_origins()
IMAGE_LOCAL_SPOOL_ROOTS = tuple(
    Path(value.strip()).expanduser().resolve()
    for value in (os.getenv("NENO_IMAGE_SPOOL_ROOTS", "").strip() or str(Path(tempfile.gettempdir()) / "neno-telegram")).split(os.pathsep)
    if value.strip()
)

# ── System Prompt（当前单句人设） ──────────────────────────────────────────
CHAT_PERSONA_ENABLED = _env_bool("CHAT_PERSONA_ENABLED", True)
SYSTEM_PROMPT = load_text("prompts/system.txt") if CHAT_PERSONA_ENABLED else ""

# ── 多层大脑 P1 ─────────────────────────────────────────────────────────────
MIND_ENABLED = _env_bool("MIND_ENABLED", False)
MIND_L1_MODEL = os.getenv("MIND_L1_MODEL", "").strip()
MIND_L1_EXTRA_BODY = json.loads(os.getenv("MIND_L1_EXTRA_BODY", "") or "null") or {}
MIND_EXPR_MODEL = os.getenv("MIND_EXPR_MODEL", "").strip() or CHAT_MODEL_NAME
MIND_EXPR_EXTRA_BODY = json.loads(os.getenv("MIND_EXPR_EXTRA_BODY", "") or "null") or {}
MIND_SELF_CARD_PATH = os.getenv("MIND_SELF_CARD_PATH", "prompts/mind/self_card.md").strip()
MIND_STYLE_CARD_PATH = os.getenv("MIND_STYLE_CARD_PATH", "prompts/mind/style_card.md").strip()
MIND_RECENT_TURNS = _env_int("MIND_RECENT_TURNS", 6)
MIND_AFFECT_HALF_LIFE_MINUTES = _env_int("MIND_AFFECT_HALF_LIFE_MINUTES", 180)
MIND_TIMEZONE = os.getenv("MIND_TIMEZONE", "Asia/Shanghai").strip()
MIND_E3_MAX_MESSAGES = _env_int("MIND_E3_MAX_MESSAGES", 4)
