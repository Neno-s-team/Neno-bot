from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class WorldLLMConfig:
    enabled: bool = False
    model: str = ""
    url: str = ""
    api_key: str = field(default="", repr=False)
    max_calls_per_day: int = 48
    min_call_interval_seconds: int = 60
    timeout_seconds: int = 45
    max_output_tokens: int = 1200
    max_context_chars: int = 14000
    extra_body: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls):
        def integer(key, default, low, high):
            value = int(os.getenv(key, str(default)))
            if not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
            return value

        extra_body = json.loads(os.getenv("WORLD_LLM_EXTRA_BODY", "") or "{}")
        if not isinstance(extra_body, dict):
            raise ValueError("WORLD_LLM_EXTRA_BODY must be a JSON object")
        return cls(
            enabled=os.getenv("WORLD_LLM_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            model=os.getenv("WORLD_LLM_MODEL", "").strip(),
            url=(os.getenv("WORLD_LLM_URL", "").strip() or os.getenv("OPENROUTER_URL", "").strip()),
            api_key=(os.getenv("WORLD_LLM_API_KEY", "").strip() or os.getenv("OPENROUTER_API_KEY", "").strip()),
            max_calls_per_day=integer("WORLD_LLM_MAX_CALLS_PER_DAY", 48, 0, 10000),
            min_call_interval_seconds=integer("WORLD_LLM_MIN_CALL_INTERVAL_SECONDS", 60, 1, 86400),
            timeout_seconds=integer("WORLD_LLM_TIMEOUT_SECONDS", 45, 5, 180),
            max_output_tokens=integer("WORLD_LLM_MAX_OUTPUT_TOKENS", 1200, 200, 8000),
            max_context_chars=integer("WORLD_LLM_MAX_CONTEXT_CHARS", 14000, 2000, 48000),
            extra_body=extra_body,
        )

    def public_status(self):
        missing = [name for name in ("model", "url", "api_key") if not getattr(self, name)]
        return {
            "enabled": self.enabled, "configured": not missing, "missing": missing,
            "model": self.model, "max_calls_per_utc_day": self.max_calls_per_day,
            "min_call_interval_seconds": self.min_call_interval_seconds,
            "max_output_tokens": self.max_output_tokens, "max_context_chars": self.max_context_chars,
        }
