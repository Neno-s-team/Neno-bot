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
MIND_E3_MAX_SINGLE_CHARS = _env_int("MIND_E3_MAX_SINGLE_CHARS", 200)

# ── 多层大脑 P2（L2 深思 + L3 自我 + 工作记忆 + 记忆存储） ────────────────────
# L2 模型；留空 = 复用聊天主模型。开思考，JSON Schema 约束输出。
MIND_THINK_MODEL = os.getenv("MIND_THINK_MODEL", "").strip()
MIND_THINK_EXTRA_BODY = json.loads(
    os.getenv("MIND_THINK_EXTRA_BODY", "").strip() or '{"reasoning": {"effort": "medium"}}'
)
MIND_THINK_MAX_TOKENS = _env_int("MIND_THINK_MAX_TOKENS", 8000)
# L1 判定 depth=fast 时跳过 L2（省钱）
MIND_FAST_SKIP_L2 = _env_bool("MIND_FAST_SKIP_L2", True)
# 工作记忆预算与槽位条数
MIND_WM_MAX_CHARS = _env_int("MIND_WM_MAX_CHARS", 6000)
MIND_WM_EPISODES = _env_int("MIND_WM_EPISODES", 5)
MIND_WM_BELIEFS = _env_int("MIND_WM_BELIEFS", 5)
# 记忆衰减半衰期（天）：recency 用检索新近度，memory 用记忆强度
MIND_RECENCY_HALF_LIFE_DAYS = _env_float("MIND_RECENCY_HALF_LIFE_DAYS", 7.0)
MIND_MEMORY_HALF_LIFE_DAYS = _env_float("MIND_MEMORY_HALF_LIFE_DAYS", 14.0)
# 检索权重：rel 相关性 / rec 新近 / imp 重要性 / emo 情绪强度 / str 记忆强度
MIND_RETRIEVAL_WEIGHTS = json.loads(
    os.getenv("MIND_RETRIEVAL_WEIGHTS", "").strip()
    or '{"rel": 1, "rec": 1, "imp": 1, "emo": 1, "str": 1}'
)
# 记忆范围；P2 只实现 session
MIND_MEMORY_SCOPE = os.getenv("MIND_MEMORY_SCOPE", "session").strip() or "session"
# 认识首插可信度上限：min(模型值, 该值)
MIND_BELIEF_INIT_MAX = _env_float("MIND_BELIEF_INIT_MAX", 0.6)

# ── 多层大脑 P3a（L0 感知/课表/待想队列/投递/调度器） ────────────────────────
# false 时 /chat 行为与 P2 完全一致，L0 不参与
MIND_ASYNC_DELIVERY = _env_bool("MIND_ASYNC_DELIVERY", False)
MIND_SCHEDULE_PATH = os.getenv("MIND_SCHEDULE_PATH", "prompts/mind/schedule.json").strip()
MIND_L0_LAMBDA_PER_MIN = json.loads(
    os.getenv(
        "MIND_L0_LAMBDA_PER_MIN", ""
    ).strip() or '{"0": 0, "1": 0.025, "2": 0.12, "3": 0.35, "4": 3.0}'
)
MIND_PREVIEW_CHARS = _env_int("MIND_PREVIEW_CHARS", 30)
MIND_PREEMPT_URGENCY = _env_float("MIND_PREEMPT_URGENCY", 0.8)
MIND_FOCUS_WINDOW_MIN = _env_int("MIND_FOCUS_WINDOW_MIN", 5)
MIND_TYPING_CPS = _env_float("MIND_TYPING_CPS", 5.0)
MIND_MSG_GAP_SEC = json.loads(os.getenv("MIND_MSG_GAP_SEC", "").strip() or "[1, 4]")
MIND_HESITATE_PAUSE_SEC = json.loads(
    os.getenv("MIND_HESITATE_PAUSE_SEC", "").strip() or "[5, 20]"
)
MIND_SCHEDULER_TICK_SEC = _env_float("MIND_SCHEDULER_TICK_SEC", 2.0)
MIND_LEASE_SEC = _env_int("MIND_LEASE_SEC", 120)
# /mind/deliveries/due 认领后的投递租约（秒）
MIND_DELIVERY_LEASE_SEC = _env_int("MIND_DELIVERY_LEASE_SEC", 60)
# mind_deferred 作业重试上限；超过后 status=failed
MIND_JOB_MAX_ATTEMPTS = _env_int("MIND_JOB_MAX_ATTEMPTS", 3)
