from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class AffectDelta:
    valence: float = 0.0
    arousal: float = 0.0


@dataclass
class L1Appraisal:
    category: Literal["分享", "提问", "求安慰", "约行动", "调情", "冲突", "确认", "晚回复"] = "分享"
    gut: str = ""
    affect_delta: AffectDelta = field(default_factory=AffectDelta)
    urgency: float = 0.0
    cues: list[str] = field(default_factory=list)
    depth: Literal["fast", "normal", "deep"] = "normal"
    reply_will: Literal["now", "later", "not"] = "now"

    @classmethod
    def from_dict(cls, data: dict) -> "L1Appraisal":
        affect = data.get("affect_delta", {})
        return cls(
            category=data.get("category", "分享"),
            gut=data.get("gut", ""),
            affect_delta=AffectDelta(
                valence=float(affect.get("valence", 0.0)),
                arousal=float(affect.get("arousal", 0.0)),
            ),
            urgency=float(data.get("urgency", 0.0)),
            cues=list(data.get("cues", [])),
            depth=data.get("depth", "normal"),
            reply_will=data.get("reply_will", "now"),
        )

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "gut": self.gut,
            "affect_delta": {"valence": self.affect_delta.valence, "arousal": self.affect_delta.arousal},
            "urgency": self.urgency,
            "cues": self.cues,
            "depth": self.depth,
            "reply_will": self.reply_will,
        }


@dataclass
class E1Plan:
    say_points: list[str] = field(default_factory=list)
    gut: str = ""
    leak: float = 0.0
    length: Literal["short", "medium", "long"] = "medium"
    n_messages: int = 1
    tone: str = "中性"

    def to_dict(self) -> dict:
        return {
            "say_points": self.say_points,
            "gut": self.gut,
            "leak": self.leak,
            "length": self.length,
            "n_messages": self.n_messages,
            "tone": self.tone,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "E1Plan":
        return cls(
            say_points=list(data.get("say_points", [])),
            gut=data.get("gut", ""),
            leak=float(data.get("leak", 0.0)),
            length=data.get("length", "medium"),
            n_messages=int(data.get("n_messages", 1)),
            tone=data.get("tone", "中性"),
        )


L1_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "mind_l1_appraisal",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["分享", "提问", "求安慰", "约行动", "调情", "冲突", "确认", "晚回复"],
                },
                "gut": {"type": "string"},
                "affect_delta": {
                    "type": "object",
                    "properties": {
                        "valence": {"type": "number"},
                        "arousal": {"type": "number"},
                    },
                    "required": ["valence", "arousal"],
                    "additionalProperties": False,
                },
                "urgency": {"type": "number"},
                "cues": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "depth": {
                    "type": "string",
                    "enum": ["fast", "normal", "deep"],
                },
                "reply_will": {
                    "type": "string",
                    "enum": ["now", "later", "not"],
                },
            },
            "required": ["category", "gut", "affect_delta", "urgency", "cues", "depth", "reply_will"],
            "additionalProperties": False,
        },
    },
}
