from __future__ import annotations

import json
from typing import Any

from app.config import MIND_L1_EXTRA_BODY, MIND_L1_MODEL
from app.services.chat.llm_gateway import request_model_response
from app.services.mind.schemas import L1Appraisal, L1_JSON_SCHEMA
from app.utils.logging_utils import log_event


def _build_l1_messages(
    message: str,
    state: dict[str, Any],
    self_card: str,
    recent_turns: list[dict],
) -> list[dict]:
    recent_text = "\n".join(
        f"{item['role']}: {str(item.get('content',''))[:200]}"
        for item in recent_turns[-2:]
    )
    prompt = f"""你是 Neno 的直觉层（L1）。快速判断当前消息的情绪意义和紧急程度。

当前情绪状态：valence={state['valence']:.2f}, arousal={state['arousal']:.2f}

自我卡（精简）：
{self_card[:600]}

最近原话：
{recent_text}

当前消息：
{message[:800]}

请严格按 JSON Schema 输出，不要添加任何其他内容。"""
    return [
        {"role": "system", "content": "你是一个快速情绪直觉判断器。只输出 JSON。"},
        {"role": "user", "content": prompt},
    ]


def run_l1(
    message: str,
    state: dict[str, Any],
    self_card: str,
    recent_turns: list[dict],
    *,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> L1Appraisal:
    messages = _build_l1_messages(message, state, self_card, recent_turns)
    extra = {**MIND_L1_EXTRA_BODY, **L1_JSON_SCHEMA}

    for attempt in range(2):
        try:
            raw = request_model_response(
                model_name=MIND_L1_MODEL,
                messages=messages,
                trace_id=trace_id,
                session_id=session_id,
                extra_body=extra,
                purpose="mind_l1",
            )
            data = json.loads(raw)
            appraisal = L1Appraisal.from_dict(data)
            log_event(
                "mind",
                "l1_appraisal_ok",
                trace_id=trace_id,
                session_id=session_id,
                category=appraisal.category,
                depth=appraisal.depth,
                attempt=attempt + 1,
            )
            return appraisal
        except Exception as exc:
            log_event(
                "mind",
                "l1_appraisal_retry",
                trace_id=trace_id,
                session_id=session_id,
                error_type=type(exc).__name__,
                attempt=attempt + 1,
            )
            if attempt == 0:
                messages.append({
                    "role": "user",
                    "content": f"上次输出解析失败：{type(exc).__name__}。请严格重新输出符合 JSON Schema 的结果。",
                })
                continue
            raise
