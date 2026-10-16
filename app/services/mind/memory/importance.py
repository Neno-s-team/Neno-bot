"""重要性打分：纯规则，不调模型（docs/mind-plan.md 第 5 节）。"""

from __future__ import annotations

from typing import Any

from app.services.mind.memory import store as memory_store
from app.services.mind.memory.tokenize import to_query_text

# 与用户有关的参与者写法（中英文）
_USER_PARTICIPANTS = {"user", "用户", "他", "你"}
_USER_SUMMARY_WORDS = ("你", "他", "用户")
# 承诺类措辞
_PROMISE_WORDS = ("答应", "约好", "保证", "下次", "明天", "说好")
# 同一 session 下"第一次发生"的判定阈值：bm25 高于此值视为不相似
_SIMILARITY_RANK_THRESHOLD = -0.5


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def is_first_time(session_id: str, summary: str, *, exclude_cycle_id: str | None = None) -> bool:
    """同 session 下没有相似经历就算第一次发生（用 FTS 相似度近似）。"""
    query = to_query_text(summary)
    if not query:
        return True
    candidates = memory_store.search_episodes(session_id, query, limit=5)
    for item in candidates:
        if exclude_cycle_id and item["cycle_id"] == exclude_cycle_id:
            continue
        if item["rank"] <= _SIMILARITY_RANK_THRESHOLD:
            return False
    return True


def score_importance(
    session_id: str,
    note: dict[str, Any],
    *,
    valence: float = 0.0,
    arousal: float = 0.0,
    exclude_cycle_id: str | None = None,
) -> float:
    """基础 0.2 + 与用户有关 + 第一次发生 + 涉及承诺 + 情绪强烈，截到 [0,1]。"""
    score = 0.2
    summary = str(note.get("summary") or "")
    participants = [str(item).lower() for item in (note.get("participants") or [])]

    if any(item in _USER_PARTICIPANTS for item in participants) or any(
        word in summary for word in _USER_SUMMARY_WORDS
    ):
        score += 0.2
    if is_first_time(session_id, summary, exclude_cycle_id=exclude_cycle_id):
        score += 0.2
    if any(word in summary for word in _PROMISE_WORDS):
        score += 0.2
    if abs(valence) + max(arousal, 0.0) >= 0.6:
        score += 0.2
    return _clamp(score)
