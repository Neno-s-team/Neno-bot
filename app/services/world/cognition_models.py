from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.services.world.models import ActionRequest, Command, CompletionCondition, Input


class CognitionSettings(Input):
    enabled: bool = False
    actor_interval_seconds: int = Field(900, ge=60, le=86400)
    director_enabled: bool = True
    director_interval_seconds: int = Field(7200, ge=600, le=604800)
    director_probability: float = Field(0.25, ge=0, le=1)
    actor_profiles: dict[str, str] = Field(default_factory=dict, max_length=100)

    @model_validator(mode="after")
    def bounded_profiles(self):
        if any(not 1 <= len(k) <= 100 or len(v) > 1000 for k, v in self.actor_profiles.items()):
            raise ValueError("Profiles require actor IDs and at most 1000 characters each")
        return self


class CognitionUpdate(Command):
    settings: CognitionSettings


class ProposedGoal(Input):
    title: str = Field(min_length=1, max_length=160)
    motivation: str = Field(min_length=1, max_length=500)
    conditions: list[CompletionCondition] = Field(min_length=1, max_length=5)


class DecisionAction(Input):
    operation: str
    target_id: str = Field(min_length=1, max_length=100)
    params: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def supported_action(self):
        ActionRequest(request_id="validate", actor_id="validate", **self.model_dump())
        return self


class ActorDecision(Input):
    summary: str = Field(min_length=1, max_length=500)
    plan: list[str] = Field(default_factory=list, max_length=6)
    action: DecisionAction | None = None
    follow_up_actions: list[DecisionAction] = Field(default_factory=list, max_length=4)
    new_goal: ProposedGoal | None = None
    thread_id: str | None = Field(None, max_length=100)
    abandon_thread_id: str | None = Field(None, max_length=100)
    wait_seconds: int = Field(900, ge=60, le=86400)

    @model_validator(mode="after")
    def coherent_decision(self):
        if any(len(step) > 240 for step in self.plan):
            raise ValueError("Plan steps must be short")
        if self.new_goal and self.thread_id:
            raise ValueError("Continue an existing goal or create one, not both")
        if self.new_goal and not self.action:
            raise ValueError("A new goal must include its first action")
        if self.follow_up_actions and not self.action:
            raise ValueError("Follow-up actions require a first action")
        if self.thread_id and self.thread_id == self.abandon_thread_id:
            raise ValueError("Cannot continue and abandon the same goal")
        return self


class DirectorDecision(Input):
    kind: Literal["none", "opportunity", "weather"]
    summary: str = Field(min_length=1, max_length=500)
    region_id: str | None = Field(None, max_length=100)
    actor_id: str | None = Field(None, max_length=100)
    title: str | None = Field(None, max_length=160)
    suggestion: str | None = Field(None, max_length=500)
    weather: Literal["clear", "cloudy", "rain"] | None = None

    @model_validator(mode="after")
    def required_fields(self):
        if self.kind != "none" and not self.region_id:
            raise ValueError("An event needs a region")
        if self.kind == "opportunity" and not all([self.actor_id, self.title, self.suggestion]):
            raise ValueError("An opportunity needs an actor, title and suggestion")
        if self.kind == "weather" and not self.weather:
            raise ValueError("A weather event needs weather")
        return self
