"""工作记忆拼装：固定槽位顺序，超预算按槽位优先级裁剪。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from app import config
from app.services.mind.memory import retrieval

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


def _local_now() -> datetime:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo(config.MIND_TIMEZONE))
    return datetime.now()


def period_word(hour: int) -> str:
    """时段词：早晨/上午/中午/下午/晚上/深夜/凌晨（与一致性检查共用表）。"""
    if 5 <= hour < 9:
        return "早晨"
    if 9 <= hour < 12:
        return "上午"
    if 12 <= hour < 13:
        return "中午"
    if 13 <= hour < 18:
        return "下午"
    if 18 <= hour < 23:
        return "晚上"
    if hour == 23:
        return "深夜"
    return "凌晨"


def _seconds_since_last_user_message(history: list[dict]) -> float | None:
    now = time.time()
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        created_at = item.get("created_at")
        if not created_at:
            return None
        try:
            stamp = datetime.strptime(str(created_at), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=ZoneInfo("UTC") if ZoneInfo else None
            ).timestamp()
        except ValueError:
            return None
        return max(0.0, now - stamp)
    return None


def _render_situation(situation: dict[str, Any]) -> str:
    lines = [
        f"现在是 {situation['local_time']}（周{situation['weekday']}，{situation['period']}）",
    ]
    gap = situation.get("seconds_since_last_user_message")
    if gap is not None:
        lines.append(f"距上一条用户消息：{int(gap // 60)} 分钟")
    lines.append(f"可打断等级：{situation['interrupt_level']}（world={situation['world']}）")
    return "\n".join(lines)


def _render_episode(episode: dict[str, Any]) -> str:
    if ZoneInfo is not None:
        stamp = datetime.fromtimestamp(
            float(episode["ts"]), tz=ZoneInfo(config.MIND_TIMEZONE)
        ).strftime("%m-%d %H:%M")
    else:
        stamp = datetime.fromtimestamp(float(episode["ts"])).strftime("%m-%d %H:%M")
    valence = float(episode.get("valence") or 0.0)
    arousal = float(episode.get("arousal") or 0.0)
    return (
        f"- [{stamp}]（情绪 v={valence:+.2f} a={arousal:.2f}）{episode['summary']}"
    )


def _render_belief(belief: dict[str, Any]) -> str:
    return f"- {belief['subject']}：{belief['content']}（可信度 {float(belief['confidence']):.2f}）"


def build_working_memory(
    session_id: str,
    *,
    l1: Any,
    message: str,
    history: list[dict],
    self_card: str,
    now: float | None = None,
) -> dict[str, Any]:
    """拼装工作记忆包；返回 dict + 渲染文本 + 裁剪记录。"""
    current = now if now is not None else time.time()
    local = _local_now()
    situation = {
        "local_time": local.strftime("%Y-%m-%d %H:%M"),
        "weekday": _WEEKDAYS[local.weekday()],
        "period": period_word(local.hour),
        "seconds_since_last_user_message": _seconds_since_last_user_message(history),
        # P2 没有 L0/世界：默认打断等级 3，明确标注世界不可用
        "interrupt_level": 3,
        "world": "unavailable",
    }
    cues = [str(cue) for cue in (getattr(l1, "cues", None) or [])]
    episodes = retrieval.retrieve_episodes(
        session_id,
        cues,
        message,
        limit=max(1, int(config.MIND_WM_EPISODES)),
        apply_recall_boost=True,
    )
    beliefs = retrieval.retrieve_beliefs(
        session_id, cues, message, limit=max(1, int(config.MIND_WM_BELIEFS))
    )
    recent_turns = [
        {
            "role": item.get("role"),
            "content": str(item.get("content") or "")[:300],
        }
        for item in history
        if item.get("role") in ("user", "assistant")
    ][-max(2, int(config.MIND_RECENT_TURNS)):]

    memory = {
        "situation": situation,
        "concerns": [],  # P2 留空；槽位保留
        "episodes": episodes,
        "beliefs": beliefs,
        "recent_turns": recent_turns,
        "self_card": self_card,
    }
    trimmed = _trim_to_budget(memory)
    text = render_working_memory(memory)
    return {
        "situation": situation,
        "concerns": memory["concerns"],
        "episodes": memory["episodes"],
        "beliefs": memory["beliefs"],
        "recent_turns": memory["recent_turns"],
        "self_card": memory["self_card"],
        "text": text,
        "trimmed": trimmed,
    }


def _budget() -> int:
    return max(400, int(config.MIND_WM_MAX_CHARS))


def _trim_to_budget(memory: dict[str, Any]) -> list[str]:
    """裁剪顺序：经历 → 认识 → 最近原话（各保留最少 2 条），最后截短 summary。"""
    trimmed: list[str] = []
    if len(render_working_memory(memory)) <= _budget():
        return trimmed

    def over() -> bool:
        return len(render_working_memory(memory)) > _budget()

    # ① 减少经历条数，最少 2
    while over() and len(memory["episodes"]) > 2:
        memory["episodes"].pop()
        trimmed.append("episodes")
    # ② 减少认识条数，最少 2
    while over() and len(memory["beliefs"]) > 2:
        memory["beliefs"].pop()
        trimmed.append("beliefs")
    # ③ 减少最近原话条数，最少 2
    while over() and len(memory["recent_turns"]) > 2:
        memory["recent_turns"].pop(0)
        trimmed.append("recent_turns")
    # ④ 截短每段 summary（自我卡不裁剪）
    if over():
        trimmed.append("summaries")
        for item in memory["episodes"]:
            item["summary"] = str(item.get("summary") or "")[:60]
            if not over():
                break
        for item in memory["beliefs"]:
            item["content"] = str(item.get("content") or "")[:60]
            if not over():
                break
        for item in memory["recent_turns"]:
            item["content"] = str(item.get("content") or "")[:80]
            if not over():
                break
    return trimmed


def render_working_memory(memory: dict[str, Any]) -> str:
    sections: list[str] = []
    sections.append("【当前情境】\n" + _render_situation(memory["situation"]))
    concerns = memory.get("concerns") or []
    if concerns:
        sections.append("【在意事项】\n" + "\n".join(f"- {item}" for item in concerns))
    episodes = memory.get("episodes") or []
    if episodes:
        sections.append("【相关经历】\n" + "\n".join(_render_episode(item) for item in episodes))
    beliefs = memory.get("beliefs") or []
    if beliefs:
        sections.append("【认识】\n" + "\n".join(_render_belief(item) for item in beliefs))
    recent = memory.get("recent_turns") or []
    if recent:
        lines = [f"{item['role']}：{item['content']}" for item in recent]
        sections.append("【最近原话】\n" + "\n".join(lines))
    if memory.get("self_card"):
        sections.append("【自我卡】\n" + memory["self_card"])
    return "\n\n".join(sections)
