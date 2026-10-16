import base64
import mimetypes
from pathlib import Path

import re

from app.config import (
    CHAT_MAX_TOKENS,
    CHAT_MODEL_NAME,
    CHAT_STOP_SEQUENCES,
    LLM_EXTRA_BODY,
    LLM_TIMEOUT_SECONDS,
    OPENROUTER_API_KEY,
    OPENROUTER_URL,
    VISION_MODEL_NAME,
)

# 兜底：stop 序列拦不住出现在「最开头」（前面没换行）的角色标签，这里把开头的剥掉。
_LEADING_ROLE_LABEL = re.compile(r"^\s*(?:assistant|human|user)\s*[:：]?\s*", re.IGNORECASE)


def _strip_leading_role_label(text: str) -> str:
    out, prev = text, None
    while out != prev:
        prev = out
        out = _LEADING_ROLE_LABEL.sub("", out, count=1)
    return out
from app.llm.openrouter_client import chat_with_openrouter, multimodal_chat_with_openrouter
from app.schemas import MediaAttachment
from app.utils.logging_utils import log_event


def require_api_key() -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    return OPENROUTER_API_KEY


def request_model_response(
    *,
    model_name: str,
    messages: list[dict],
    timeout: int = 60,
    trace_id: str | None = None,
    max_tokens: int | None = None,
    stop: list[str] | None = None,
    extra_body: dict | None = None,
    session_id: str | None = None,
    tools: list[dict] | None = None,
    tool_choice: str = "auto",
    return_message: bool = False,
    purpose: str = "chat",
) -> str | dict:
    return chat_with_openrouter(
        api_key=require_api_key(),
        url=OPENROUTER_URL,
        model_name=model_name,
        messages=messages,
        timeout=timeout,
        trace_id=trace_id,
        max_tokens=max_tokens,
        stop=stop,
        extra_body=extra_body,
        session_id=session_id,
        tools=tools,
        tool_choice=tool_choice,
        return_message=return_message,
        purpose=purpose,
    )


def generate_chat_reply(
    messages: list[dict], trace_id: str | None = None, session_id: str | None = None
) -> str:
    if session_id:
        from app.storage.image_store import has_images
        if has_images(session_id):
            from app.services.chat.image_memory import generate_image_reply
            return generate_image_reply(messages, session_id, trace_id)
    reply = request_model_response(
        model_name=CHAT_MODEL_NAME,
        messages=messages,
        timeout=LLM_TIMEOUT_SECONDS,
        trace_id=trace_id,
        max_tokens=CHAT_MAX_TOKENS,
        stop=CHAT_STOP_SEQUENCES,
        extra_body=LLM_EXTRA_BODY,
        session_id=session_id,
    )
    return _strip_leading_role_label(reply)


def generate_multimodal_chat_reply(
    *,
    text_prompt: str,
    attachments: list[MediaAttachment],
    trace_id: str | None = None,
) -> str:
    image_inputs = []
    for item in attachments:
        if item.kind != "image":
            continue
        image_input = resolve_multimodal_image_input(item, trace_id=trace_id)
        if image_input:
            image_inputs.append(image_input)

    if not image_inputs:
        raise RuntimeError("no supported multimodal attachments")

    return multimodal_chat_with_openrouter(
        api_key=require_api_key(),
        url=OPENROUTER_URL,
        model_name=VISION_MODEL_NAME,
        text_prompt=text_prompt,
        image_urls=image_inputs,
        timeout=LLM_TIMEOUT_SECONDS,
        trace_id=trace_id,
    )


def resolve_multimodal_image_input(
    attachment: MediaAttachment,
    *,
    trace_id: str | None = None,
) -> str | None:
    media_path = (attachment.media_path or "").strip()
    if media_path:
        return build_image_data_url(media_path, attachment.mime_type, trace_id=trace_id)

    image_url = (attachment.url or "").strip()
    if image_url:
        log_event(
            "multimodal",
            "image_attachment_input_selected",
            trace_id=trace_id,
            source=attachment.source,
            input_kind="url",
            has_media_path=False,
            has_url=True,
        )
        return image_url

    return None


def build_image_data_url(
    media_path: str,
    mime_type: str | None,
    *,
    trace_id: str | None = None,
) -> str:
    path = Path(media_path).expanduser()
    if not path.is_file():
        raise RuntimeError("image attachment local file missing")

    image_bytes = path.read_bytes()
    resolved_mime = resolve_image_mime_type(path, image_bytes, mime_type)
    encoded = base64.b64encode(image_bytes).decode("ascii")

    log_event(
        "multimodal",
        "image_attachment_input_selected",
        trace_id=trace_id,
        input_kind="data_url",
        mime_type=resolved_mime,
        byte_len=len(image_bytes),
        has_media_path=True,
        has_url=False,
    )
    return f"data:{resolved_mime};base64,{encoded}"


def resolve_image_mime_type(path: Path, image_bytes: bytes, mime_type: str | None) -> str:
    normalized = (mime_type or "").strip().lower()
    if normalized.startswith("image/"):
        return normalized

    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and guessed.startswith("image/"):
        return guessed

    magic = image_bytes[:16]
    if magic.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if magic.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if magic.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if magic.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if magic.startswith(b"BM"):
        return "image/bmp"

    raise RuntimeError("image attachment local file is not a supported image")
