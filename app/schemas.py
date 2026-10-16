from typing import Any

from pydantic import BaseModel, Field


class MediaAttachment(BaseModel):
    """低耦合的多模态输入描述：只描述「是什么、在哪」，不绑定任何平台。"""

    kind: str = Field(..., max_length=32)
    url: str | None = Field(default=None, max_length=2000)
    media_path: str | None = Field(default=None, max_length=1000)
    mime_type: str | None = Field(default=None, max_length=128)
    source: str | None = Field(default=None, max_length=64)
    text_hint: str | None = Field(default=None, max_length=500)
    duration_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    image_id: str | None = Field(default=None, max_length=64)


class ChatRequest(BaseModel):
    request_id: str | None = Field(default=None, max_length=160)
    message: str = Field(default="", max_length=2000)
    session_id: str = Field(default="default", max_length=128)
    attachments: list[MediaAttachment] = Field(default_factory=list, max_length=8)
    source: str = Field(default="local", max_length=64)


class ChatResponse(BaseModel):
    reply: str
    trace_id: str | None = None
    session_id: str | None = None
    user_message_id: int | None = None
    user_message_ids: list[int] = Field(default_factory=list)
    assistant_message_id: int | None = None
    message_type: str | None = None
    latency_ms: int | None = None
    error: dict[str, Any] | None = None


class SessionRequest(BaseModel):
    session_id: str = Field(default="default", max_length=128)


class SessionMessageDeleteRequest(BaseModel):
    message_id: int = Field(..., ge=1)
