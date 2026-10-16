"""L2 深思 + L3 自我：一次结构化调用，输出经校验与截断。

思考过程（<think> 或自由文本）一律不落库、不打日志；只保留结构化结果。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app import config
from app.services.chat.llm_gateway import request_model_response
from app.services.mind.schemas import L1Appraisal
from app.utils.logging_utils import log_event

_THINK_BLOCK = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_FENCE_HEAD = re.compile(r"^```(?:json)?\s*")
_FENCE_TAIL = re.compile(r"\s*```$")
_LENGTHS = {"short", "medium", "long"}

# 截断常量（规格第 3 节）
_MAX_STATE_DELTA = 0.3
_MAX_NOTES = 3
_MAX_BELIEF_UPDATES = 3
_MAX_SAY_POINTS = 4
_MAX_HIDDEN = 3


@dataclass
class Intent:
    action: str = ""
    target: str | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return {"action": self.action, "target": self.target, "note": self.note}


@dataclass
class MemoryNote:
    summary: str = ""
    participants: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "participants": self.participants,
            "tags": self.tags,
        }


@dataclass
class BeliefUpdate:
    subject: str = ""
    content: str = ""
    confidence: float = 0.0
    evidence: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "subject": self.subject,
            "content": self.content,
            "confidence": self.confidence,
            "evidence": self.evidence,
        }


@dataclass
class Decision:
    reply: bool = True
    world_action: bool = False

    def to_dict(self) -> dict:
        return {"reply": self.reply, "world_action": self.world_action}


@dataclass
class L2Result:
    interpretation: str = ""
    feeling: str = ""
    decision: Decision = field(default_factory=Decision)
    intents: list[Intent] = field(default_factory=list)
    state_delta_valence: float = 0.0
    state_delta_arousal: float = 0.0
    memory_notes: list[MemoryNote] = field(default_factory=list)
    belief_updates: list[BeliefUpdate] = field(default_factory=list)
    # L3 段
    say_points: list[str] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)
    leak: float = 0.0
    tone: str = "平静"
    length: str = "short"
    n_messages: int = 1
    hesitate: bool = False
    sticker: str | None = None

    def to_dict(self) -> dict:
        return {
            "l2": {
                "interpretation": self.interpretation,
                "feeling": self.feeling,
                "decision": self.decision.to_dict(),
                "intents": [item.to_dict() for item in self.intents],
                "state_delta": {
                    "valence": self.state_delta_valence,
                    "arousal": self.state_delta_arousal,
                },
                "memory_notes": [item.to_dict() for item in self.memory_notes],
                "belief_updates": [item.to_dict() for item in self.belief_updates],
            },
            "l3": {
                "say_points": self.say_points,
                "hidden": self.hidden,
                "leak": self.leak,
                "tone": self.tone,
                "length": self.length,
                "n_messages": self.n_messages,
                "hesitate": self.hesitate,
                "sticker": self.sticker,
            },
        }

    @classmethod
    def from_dict(
        cls,
        data: dict,
        *,
        leak_cap: float | None,
        max_messages: int,
        sticker_names: set[str] | None = None,
    ) -> "L2Result":
        if not isinstance(data, dict):
            raise ValueError("L2 output is not a JSON object")
        l2 = data.get("l2") or {}
        l3 = data.get("l3") or {}
        if not isinstance(l2, dict) or not isinstance(l3, dict):
            raise ValueError("L2/L3 sections must be objects")

        decision_raw = l2.get("decision") or {}
        decision = Decision(
            reply=bool(decision_raw.get("reply", True)),
            world_action=bool(decision_raw.get("world_action", False)),
        )

        intents: list[Intent] = []
        for item in _as_list(l2.get("intents")):
            if not isinstance(item, dict):
                continue
            intents.append(
                Intent(
                    action=str(item.get("action") or "")[:200],
                    target=_optional_str(item.get("target")),
                    note=str(item.get("note") or "")[:200],
                )
            )
        intents = intents[:5]

        delta_raw = l2.get("state_delta") or {}
        state_valence = _clamp(_as_float(delta_raw.get("valence")), -_MAX_STATE_DELTA, _MAX_STATE_DELTA)
        state_arousal = _clamp(_as_float(delta_raw.get("arousal")), -_MAX_STATE_DELTA, _MAX_STATE_DELTA)

        notes: list[MemoryNote] = []
        for item in _as_list(l2.get("memory_notes")):
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary") or "").strip()[:300]
            if not summary:
                continue
            notes.append(
                MemoryNote(
                    summary=summary,
                    participants=[str(part)[:40] for part in _as_list(item.get("participants"))][:8],
                    tags=[str(tag)[:40] for tag in _as_list(item.get("tags"))][:8],
                )
            )
        notes = notes[:_MAX_NOTES]

        updates: list[BeliefUpdate] = []
        for item in _as_list(l2.get("belief_updates")):
            if not isinstance(item, dict):
                continue
            subject = str(item.get("subject") or "").strip()[:80]
            content = str(item.get("content") or "").strip()[:300]
            if not subject or not content:
                continue
            evidence: list[int] = []
            for raw_id in _as_list(item.get("evidence")):
                try:
                    evidence.append(int(raw_id))
                except (TypeError, ValueError):
                    continue
            updates.append(
                BeliefUpdate(
                    subject=subject,
                    content=content,
                    confidence=_clamp(_as_float(item.get("confidence")), 0.0, 1.0),
                    evidence=evidence[:12],
                )
            )
        updates = updates[:_MAX_BELIEF_UPDATES]

        say_points = [str(item).strip()[:200] for item in _as_list(l3.get("say_points"))]
        say_points = [item for item in say_points if item][:_MAX_SAY_POINTS]
        hidden = [str(item).strip()[:200] for item in _as_list(l3.get("hidden"))]
        hidden = [item for item in hidden if item][:_MAX_HIDDEN]

        # leak_cap=None：回放已存结果，不按当前时段重新收紧
        leak = max(0.0, _as_float(l3.get("leak")))
        if leak_cap is not None:
            leak = _clamp(leak, 0.0, max(0.0, leak_cap))
        tone = str(l3.get("tone") or "").strip()[:20] or "平静"
        length = str(l3.get("length") or "").strip().lower()
        if length not in _LENGTHS:
            raise ValueError(f"invalid length: {length}")
        n_messages = int(_clamp(_as_float(l3.get("n_messages", 1)), 1, max(1, max_messages)))
        hesitate = bool(l3.get("hesitate", False))
        sticker = _optional_str(l3.get("sticker"))
        if sticker and sticker_names is not None:
            if not sticker_names or sticker not in sticker_names:
                sticker = None
        if sticker:
            sticker = sticker[:40]

        return cls(
            interpretation=str(l2.get("interpretation") or "")[:600],
            feeling=str(l2.get("feeling") or "")[:600],
            decision=decision,
            intents=intents,
            state_delta_valence=state_valence,
            state_delta_arousal=state_arousal,
            memory_notes=notes,
            belief_updates=updates,
            say_points=say_points,
            hidden=hidden,
            leak=leak,
            tone=tone,
            length=length,
            n_messages=n_messages,
            hesitate=hesitate,
            sticker=sticker,
        )


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def strip_think(raw: str) -> str:
    """去掉 <think>…</think> 与 ```json 围栏；思考内容不落库不打日志。"""
    text = _THINK_BLOCK.sub("", str(raw or ""))
    text = _FENCE_HEAD.sub("", text.strip())
    text = _FENCE_TAIL.sub("", text)
    return text.strip()


def build_deliberation_messages(
    *,
    wm_text: str,
    l1: L1Appraisal,
    message: str,
    self_card: str,
    leak_cap: float,
    extra_errors: list[str] | None = None,
) -> list[dict]:
    error_block = ""
    if extra_errors:
        error_block = "\n\n【上次输出被检查拦下，请修正】\n" + "\n".join(
            f"- {item}" for item in extra_errors
        )
    prompt = f"""你是 Neno 的深思层（L2）与自我层（L3）。先想清楚，再决定怎么表现。

【工作记忆】
{wm_text}

【L1 直觉】
{json.dumps(l1.to_dict(), ensure_ascii=False)}

【当前消息】
{message[:800]}

【输出要求】
只输出一个 JSON 对象，结构为 {{"l2": {{...}}, "l3": {{...}}}}，不要任何其他内容。
- l2.interpretation：对用户意图的揣测；l2.feeling：想清楚后的感受。
- l2.decision.reply 恒为 true；l2.decision.world_action 恒为 false（世界未接入）。
- l2.intents：你打算做的事，只能是你自己的提议；不能是已完成行动，主语不能是用户或 NPC。
- l2.state_delta：本人情绪变化，各分量在 ±0.3 内。
- l2.memory_notes：≤3 条，"她视角"的经历摘要。
- l2.belief_updates：≤3 条；evidence 是已存在的经历 id 列表。
- l3.say_points：≤4 条说话要点；l3.hidden：≤3 条不直接说出口的。
- l3.leak：真实想法泄漏强度，0–{leak_cap:.2f}。
- l3.tone：语气词；l3.length：short|medium|long；l3.n_messages：1–{max(1, int(config.MIND_E3_MAX_MESSAGES))}。
- l3.hesitate：是否犹豫；l3.sticker：只从表情包表里选名字，没有合适的用 null。{error_block}"""
    system_block: dict[str, Any] = {"type": "text", "text": self_card}
    system_block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    return [
        {"role": "system", "content": [system_block]},
        {"role": "user", "content": prompt},
    ]


L2_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "mind_l2_l3_deliberation",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "l2": {
                    "type": "object",
                    "properties": {
                        "interpretation": {"type": "string"},
                        "feeling": {"type": "string"},
                        "decision": {
                            "type": "object",
                            "properties": {
                                "reply": {"type": "boolean"},
                                "world_action": {"type": "boolean"},
                            },
                            "required": ["reply", "world_action"],
                            "additionalProperties": False,
                        },
                        "intents": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "action": {"type": "string"},
                                    "target": {"type": ["string", "null"]},
                                    "note": {"type": "string"},
                                },
                                "required": ["action", "target", "note"],
                                "additionalProperties": False,
                            },
                        },
                        "state_delta": {
                            "type": "object",
                            "properties": {
                                "valence": {"type": "number"},
                                "arousal": {"type": "number"},
                            },
                            "required": ["valence", "arousal"],
                            "additionalProperties": False,
                        },
                        "memory_notes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "summary": {"type": "string"},
                                    "participants": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "tags": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": ["summary", "participants", "tags"],
                                "additionalProperties": False,
                            },
                        },
                        "belief_updates": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "subject": {"type": "string"},
                                    "content": {"type": "string"},
                                    "confidence": {"type": "number"},
                                    "evidence": {
                                        "type": "array",
                                        "items": {"type": "integer"},
                                    },
                                },
                                "required": ["subject", "content", "confidence", "evidence"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": [
                        "interpretation", "feeling", "decision", "intents",
                        "state_delta", "memory_notes", "belief_updates",
                    ],
                    "additionalProperties": False,
                },
                "l3": {
                    "type": "object",
                    "properties": {
                        "say_points": {"type": "array", "items": {"type": "string"}},
                        "hidden": {"type": "array", "items": {"type": "string"}},
                        "leak": {"type": "number"},
                        "tone": {"type": "string"},
                        "length": {"type": "string", "enum": ["short", "medium", "long"]},
                        "n_messages": {"type": "integer"},
                        "hesitate": {"type": "boolean"},
                        "sticker": {"type": ["string", "null"]},
                    },
                    "required": [
                        "say_points", "hidden", "leak", "tone", "length",
                        "n_messages", "hesitate", "sticker",
                    ],
                    "additionalProperties": False,
                },
            },
            "required": ["l2", "l3"],
            "additionalProperties": False,
        },
    },
}


def run_deliberation(
    *,
    wm_text: str,
    l1: L1Appraisal,
    message: str,
    self_card: str,
    leak_cap: float,
    sticker_names: set[str] | None,
    extra_errors: list[str] | None = None,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> tuple[L2Result, str]:
    """调用一次 L2/L3；返回 (结果, 实际使用的模型名)。解析失败抛 ValueError。"""
    model_name = config.MIND_THINK_MODEL or config.CHAT_MODEL_NAME
    messages = build_deliberation_messages(
        wm_text=wm_text,
        l1=l1,
        message=message,
        self_card=self_card,
        leak_cap=leak_cap,
        extra_errors=extra_errors,
    )
    extra = {
        **config.MIND_THINK_EXTRA_BODY,
        "response_format": L2_JSON_SCHEMA,
        "provider": {"require_parameters": True},
    }
    raw = request_model_response(
        model_name=model_name,
        messages=messages,
        trace_id=trace_id,
        session_id=session_id,
        extra_body=extra,
        timeout=config.LLM_TIMEOUT_SECONDS,
        max_tokens=config.MIND_THINK_MAX_TOKENS,
        purpose="mind_think",
    )
    text = strip_think(raw)
    data = json.loads(text)
    result = L2Result.from_dict(
        data,
        leak_cap=leak_cap,
        max_messages=int(config.MIND_E3_MAX_MESSAGES),
        sticker_names=sticker_names,
    )
    log_event(
        "mind",
        "l2_deliberation_ok",
        trace_id=trace_id,
        session_id=session_id,
        say_points=len(result.say_points),
        notes=len(result.memory_notes),
        model=model_name,
    )
    return result, model_name
