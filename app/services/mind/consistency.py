"""一致性检查：纯代码，返回 (ok, errors[])。检查失败打回 L2 重做一次。"""

from __future__ import annotations

from typing import Any

from app import config
from app.services.mind.deliberation import L2Result
from app.services.mind.memory import store as memory_store
from app.services.mind.memory.wm_builder import period_word
from app.utils.logging_utils import log_event

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

# 时段词 → 允许出现的本地小时集合（本地时区）
_TIME_WORDS: dict[str, set[int]] = {
    "早上": set(range(5, 12)),
    "早安": set(range(5, 12)),
    "中午": {11, 12, 13},
    "下午": set(range(13, 18)),
    "晚上": set(range(18, 24)),
    "晚安": set(range(21, 24)) | set(range(0, 5)),
    "深夜": {23, 0, 1, 2, 3},
    "凌晨": {0, 1, 2, 3, 4},
}
# 完成态措辞：意图只能是提议，不能是已完成行动
_COMPLETED_WORDS = ("已经", "做完了", "去过了", "完成了", "办好了", "搞定")
# 主语是这些词 → 不是 Neno 自己的提议
_FOREIGN_SUBJECTS = ("用户", "他", "她", "小雨", "NPC", "npc")


def _local_hour() -> int:
    from datetime import datetime

    if ZoneInfo is not None:
        return datetime.now(ZoneInfo(config.MIND_TIMEZONE)).hour
    return datetime.now().hour


def check_time_line(result: L2Result, *, hour: int | None = None) -> list[str]:
    current_hour = _local_hour() if hour is None else hour
    errors: list[str] = []
    texts = [*result.say_points, *result.hidden]
    for text in texts:
        for word, allowed_hours in _TIME_WORDS.items():
            if word not in text:
                continue
            if current_hour not in allowed_hours:
                errors.append(
                    f"时间线不一致：当前是{period_word(current_hour)}（{current_hour}点），"
                    f"但说「{word}」"
                )
    return errors


def check_agency(result: L2Result) -> list[str]:
    errors: list[str] = []
    for intent in result.intents:
        text = f"{intent.action} {intent.note}"
        if any(word in text for word in _COMPLETED_WORDS):
            errors.append(f"行动主导权：意图是完成态「{intent.action}」，只能是提议")
            continue
        action = intent.action
        if any(action.startswith(subject) or f"{subject}要" in action
               or f"{subject}把" in action or f"{subject}会" in action
               for subject in _FOREIGN_SUBJECTS):
            errors.append(f"行动主导权：意图主语不是 Neno「{intent.action}」")
    return errors


def check_hidden_leak(result: L2Result) -> list[str]:
    errors: list[str] = []
    for hidden in result.hidden:
        if len(hidden) < 4:
            continue
        for point in result.say_points:
            if hidden in point:
                errors.append(f"隐藏与表达隔离：hidden「{hidden}」出现在 say_points 里")
                break
    return errors


def check_belief_evidence(
    session_id: str, result: L2Result, *, episode_ids: set[int] | None = None
) -> list[str]:
    """高可信度认识被修改时必须有存在的 evidence id。"""
    errors: list[str] = []
    for update in result.belief_updates:
        existing = memory_store.get_belief(session_id, update.subject)
        if existing is None or float(existing["confidence"]) < 0.8:
            continue
        if not update.evidence:
            errors.append(
                f"认识冲突：修改高可信度认识「{update.subject}」缺少 evidence"
            )
            continue
        valid_ids = (
            episode_ids if episode_ids is not None
            else memory_store.existing_episode_ids(session_id, update.evidence)
        )
        missing = [item for item in update.evidence if item not in valid_ids]
        if missing:
            errors.append(
                f"认识冲突：修改高可信度认识「{update.subject}」引用了不存在的经历 {missing}"
            )
    return errors


def check_result(
    session_id: str,
    result: L2Result,
    *,
    hour: int | None = None,
    episode_ids: set[int] | None = None,
    trace_id: str | None = None,
) -> tuple[bool, list[str], list[str]]:
    """返回 (ok, errors, warnings)。decision.reply=false 只记日志，不判失败。"""
    warnings: list[str] = []
    if not result.decision.reply:
        log_event(
            "mind",
            "decision_not_reply_overridden",
            trace_id=trace_id,
            session_id=session_id,
        )
    errors = [
        *check_time_line(result, hour=hour),
        *check_agency(result),
        *check_hidden_leak(result),
        *check_belief_evidence(session_id, result, episode_ids=episode_ids),
    ]
    if result.decision.world_action:
        # P2 没有世界：不判失败，只记 warning；意图只存不执行
        warnings.append("world_action=true 但世界未接入；意图只存不执行")
        log_event(
            "mind",
            "world_action_ignored",
            trace_id=trace_id,
            session_id=session_id,
            intent_count=len(result.intents),
        )
    return (not errors, errors, warnings)
