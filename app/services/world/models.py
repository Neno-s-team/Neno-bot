from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RandomSettings(Input):
    interval_seconds: int = Field(3600, ge=60, le=86400)
    probability: float = Field(0.15, ge=0, le=1)
    cooldown_seconds: int = Field(7200, ge=0, le=604800)
    max_events_per_day: int = Field(6, ge=0, le=100)
    max_events_per_region_per_day: int = Field(3, ge=0, le=100)
    rare_events_enabled: bool = False
    weather_weight: float = Field(1, ge=0, le=100)
    failure_weight: float = Field(0.1, ge=0, le=100)


class WorldCreate(Input):
    world_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    name: str = Field("Independent world", min_length=1, max_length=100)
    seed: str = Field("neno-world-1", min_length=1, max_length=128)
    start_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    utc_offset_minutes: int = Field(480, ge=-720, le=840)
    template: str = Field("neighborhood", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    running: bool = False
    rate: float = Field(1, gt=0, le=3600)
    random: RandomSettings = Field(default_factory=RandomSettings)

    @field_validator("start_at")
    @classmethod
    def aware_date(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("start_at must include a UTC offset")
        return value


class Command(Input):
    request_id: str = Field(min_length=1, max_length=128)


class Control(Command):
    operation: Literal["pause", "resume", "rate"]
    rate: float | None = Field(None, gt=0, le=3600)


class Advance(Command):
    seconds: float = Field(0, ge=0, le=604800)
    next_event: bool = False
    max_events: int = Field(500, ge=1, le=2000)


class ActionRequest(Command):
    actor_id: str = Field(min_length=1, max_length=100)
    operation: Literal["move", "take", "put", "open", "close", "turn_on", "turn_off", "clean",
                       "consume", "pour", "repair", "read", "inspect", "cook", "split", "buy"]
    target_id: str = Field(min_length=1, max_length=100)
    params: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: float | None = Field(None, ge=0, le=86400)
    thread_id: str | None = Field(None, max_length=100)

    @model_validator(mode="after")
    def validate_parameters(self):
        keys = {"put": {"destination_id"}, "pour": {"destination_id", "volume_ml"},
                "repair": {"tool_id", "part_id"}, "cook": {"ingredients"},
                "consume": {"quantity"}, "split": {"quantity"}, "buy": {"quantity"}}
        if set(self.params) - keys.get(self.operation, set()):
            raise ValueError("Unexpected action parameters")
        for key in keys.get(self.operation, set()) - {"quantity"}:
            if key not in self.params:
                raise ValueError(f"Missing parameter: {key}")
        for key, value in self.params.items():
            if key.endswith("_id") and (not isinstance(value, str) or not 1 <= len(value) <= 100):
                raise ValueError(f"Invalid entity reference: {key}")
            if key in {"quantity", "volume_ml"} and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10000):
                raise ValueError(f"Invalid positive integer: {key}")
            if key == "ingredients":
                if not isinstance(value, list) or not 1 <= len(value) <= 10 or any(not isinstance(x, str) or not 1 <= len(x) <= 100 for x in value):
                    raise ValueError("ingredients must contain 1 to 10 entity IDs")
                if len(set(value)) != len(value):
                    raise ValueError("Duplicate ingredient IDs")
        return self


class CompletionCondition(Input):
    entity_id: str = Field(min_length=1, max_length=100)
    field: str = Field(pattern=r"^(parent_id|owner_id|state\.[a-zA-Z0-9_]+)$")
    equals: str | bool | int | float | None


class ThreadRequest(Command):
    actor_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    motivation: str = Field("", max_length=1000)
    conditions: list[CompletionCondition] = Field(min_length=1, max_length=20)


class ActorControl(Command):
    active: bool


class EntityCreate(Command):
    entity_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    type_id: str = Field(min_length=1, max_length=200)
    parent_id: str = Field(min_length=1, max_length=100)
    owner_id: str | None = Field(None, max_length=100)
    label: str | None = Field(None, max_length=100)
    quantity: int = Field(1, ge=1, le=10000)


class ThreadControl(Command):
    operation: Literal["pause", "resume", "abandon"]
    reason: str = Field(min_length=1, max_length=1000)


class RandomUpdate(Command):
    settings: RandomSettings


class Inject(Command):
    kind: Literal["delivery", "weather"]
    target_id: str = Field(min_length=1, max_length=100)
    type_id: str | None = None
    label: str | None = Field(None, max_length=100)
    quantity: int = Field(1, ge=1, le=10000)
    weather: Literal["clear", "cloudy", "rain"] | None = None
    delay_seconds: float = Field(0, ge=0, le=604800)
