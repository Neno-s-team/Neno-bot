from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InboundMessage:
    chat_id: int
    message_id: int
    text: str
    content_type: str
    file_id: int | None = None
    duration_ms: int | None = None
    file_name: str | None = None
    mime_type: str | None = None
    caption: str = ""


def _caption(content: dict[str, Any]) -> str:
    caption = content.get("caption")
    if isinstance(caption, dict):
        return str(caption.get("text") or "").strip()
    return ""


def parse_update(update: dict[str, Any]) -> InboundMessage | None:
    if update.get("@type") != "updateNewMessage":
        return None
    message = update.get("message")
    if not isinstance(message, dict) or message.get("is_outgoing"):
        return None
    chat_id = int(message.get("chat_id") or 0)
    message_id = int(message.get("id") or 0)
    content = message.get("content")
    if chat_id == 0 or message_id == 0 or not isinstance(content, dict):
        return None
    kind = str(content.get("@type") or "")
    if kind == "messageText":
        text = str((content.get("text") or {}).get("text") or "").strip()
        return InboundMessage(chat_id, message_id, text, "text", caption=text)
    if kind == "messageAnimatedEmoji":
        animated = content.get("animated_emoji") or {}
        emoji = str(animated.get("emoji") or "").strip()
        if emoji:
            return InboundMessage(chat_id, message_id, emoji, "text", caption=emoji)
        return None
    if kind == "messageSticker":
        sticker = content.get("sticker") or {}
        alt = str(sticker.get("emoji") or "").strip()
        marker = f"[用户发了一个表情包{('：' + alt) if alt else ''}]"
        return InboundMessage(chat_id, message_id, marker, "text", caption=marker)
    if kind == "messagePhoto":
        sizes = ((content.get("photo") or {}).get("sizes") or [])
        file_id = None
        if sizes and isinstance(sizes[-1], dict):
            file_id = int(((sizes[-1].get("photo") or {}).get("id") or 0)) or None
        return InboundMessage(chat_id, message_id, _caption(content), "image", file_id=file_id, caption=_caption(content))
    if kind == "messageVideo":
        video = content.get("video") or {}
        return InboundMessage(
            chat_id, message_id, _caption(content), "video",
            file_id=int((video.get("video") or {}).get("id") or 0) or None,
            duration_ms=int(video.get("duration") or 0) * 1000 or None,
            mime_type=str(video.get("mime_type") or "video/mp4"), caption=_caption(content),
        )
    if kind == "messageVoiceNote":
        voice = content.get("voice_note") or {}
        return InboundMessage(
            chat_id, message_id, _caption(content), "voice",
            file_id=int((voice.get("voice") or {}).get("id") or voice.get("id") or 0) or None,
            duration_ms=int(voice.get("duration") or 0) * 1000 or None,
            mime_type=str(voice.get("mime_type") or "audio/ogg"), caption=_caption(content),
        )
    if kind == "messageDocument":
        document = content.get("document") or {}
        mime_type = str(document.get("mime_type") or "application/octet-stream").lower()
        file_id = int((document.get("document") or {}).get("id") or 0) or None
        file_name = str(document.get("file_name") or "") or None
        # Telegram clients may send PNG/WebP/JPEG as a document rather than a
        # photo. Route image/* documents through the same vision path.
        if mime_type.startswith("image/"):
            return InboundMessage(
                chat_id, message_id, _caption(content), "image",
                file_id=file_id, file_name=file_name, mime_type=mime_type,
                caption=_caption(content),
            )
        return InboundMessage(
            chat_id, message_id, _caption(content), "file",
            file_id=file_id, file_name=file_name, mime_type=mime_type, caption=_caption(content),
        )
    return None


def project_non_image_message(message: InboundMessage) -> str:
    caption = message.caption.strip()
    if message.content_type == "voice":
        duration = max(0, int(message.duration_ms or 0) // 1000)
        marker = f"[用户发送了一条 Telegram 语音消息，时长约 {duration} 秒]"
    elif message.content_type == "video":
        duration = max(0, int(message.duration_ms or 0) // 1000)
        marker = f"[用户发送了一段 Telegram 视频，时长约 {duration} 秒]"
    else:
        name = message.file_name or "未命名文件"
        marker = f"[用户发送了一个 Telegram 文件：{name}]"
    return f"{caption}\n{marker}".strip()
