from __future__ import annotations

import asyncio
import argparse
import json
import sqlite3
import logging
import mimetypes
import os
import tempfile
from pathlib import Path
from typing import Any

from telegram_gateway.config import GatewayConfig
from telegram_gateway.core_client import NenoCoreClient
from telegram_gateway.auth import authorize
from telegram_gateway.normalize import InboundMessage, parse_update, project_non_image_message
from telegram_gateway.policy import accepts_chat
from telegram_gateway.sticker_table import StickerTable, StickerTableInjector
from telegram_gateway.tdlib_client import TdlibClient

logging.getLogger().setLevel(logging.INFO)  # 发送回执走 INFO 级，保证可见


class TelegramFloodWait(RuntimeError):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, int(retry_after))
        super().__init__(f"Telegram flood wait: retry after {self.retry_after}s")


def split_outbound_text(text: str, limit: int = 4096) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in normalized.split("\n") if line.strip()]
    if not lines:
        lines = ["抱歉，我刚刚没能生成回复。"]
    return [line[index : index + limit] for line in lines for index in range(0, len(line), limit)]


class TelegramGateway:
    def __init__(self, config: GatewayConfig):
        self.config = config
        self.tdlib = TdlibClient(config.sidecar_binary, config.tdjson_library)
        self.core = NenoCoreClient(config.core_url, config.internal_token)
        self._seen: set[tuple[int, int]] = set()
        self._chat_locks: dict[int, asyncio.Lock] = {}
        # 每条消息都带表情包表（一次性上下文，core 落库时剥离）：
        # 名字表必须每轮可见，否则模型下一轮就忘了能发什么。
        self.stickers = StickerTable.load_optional(config.sticker_pack_dir)
        self._sticker_injector = StickerTableInjector(self.stickers, mode="always")
        from telegram_gateway.delivery import Delivery
        self.delivery = Delivery(self)
        self.tdlib.send_status_observer = self.delivery.receipt
        self._delivery_task = None

    async def close(self) -> None:
        self.delivery.stopping = True
        if self._delivery_task:
            self._delivery_task.cancel()
            try:
                await self._delivery_task
            except asyncio.CancelledError:
                pass
        await self.core.close()
        await self.tdlib.close()

    async def start(self) -> None:
        self.config.database_directory.mkdir(parents=True, exist_ok=True)
        self.config.files_directory.mkdir(parents=True, exist_ok=True)
        await self.tdlib.start()
        await self.tdlib.send(self.config.tdlib_parameters_request())
        await authorize(
            self.tdlib,
            interactive=False,
            database_encryption_key_b64=self.config.database_encryption_key_b64(),
        )
        me = await self.tdlib.request({'@type': 'getMe'}, timeout=30)
        if me.get('@type') != 'user' or not me.get('id'):
            raise RuntimeError('Telegram authorization completed but getMe failed')
        logging.info('Telegram authorization ready; durable delivery enabled')

    async def run(self) -> None:
        await self.start()
        await self.run_updates()

    async def run_updates(self) -> None:
        self._delivery_task = asyncio.create_task(self.delivery.run(), name='durable-telegram-delivery')
        async for update in self.tdlib.updates():
            state_type = str(update.get("@type") or "")
            if state_type == "updateAuthorizationState":
                continue
            message = parse_update(update)
            if message is None:
                if state_type == "updateNewMessage":
                    raw = update.get("message") or {}
                    if not raw.get("is_outgoing"):
                        content = (raw.get("content") or {}).get("@type")
                        logging.warning(
                            "inbound update dropped by normalizer: content_type=%s", content
                        )
                continue
            if not accepts_chat(message.chat_id, self.config.allowed_chat_ids):
                continue
            self.delivery.accept(message)

    async def _handle_message(self, message: InboundMessage) -> None:
        key = (message.chat_id, message.message_id)
        if key in self._seen:
            return
        self._seen.add(key)
        if len(self._seen) > 20_000:
            self._seen = set(list(self._seen)[-10_000:])

        temp_path: Path | None = None
        attachments: list[dict[str, Any]] = []
        text = message.text
        if message.content_type == "image":
            if message.file_id:
                suffix = self._media_suffix(message)
                temp_path = await self._download_file(message.file_id, suffix=suffix)
                attachments.append(
                    {
                        "kind": "image",
                        "media_path": str(temp_path),
                        # Leave photo MIME unset when Telegram does not expose
                        # it; Core resolves the real container from magic bytes.
                        "mime_type": message.mime_type,
                        "source": "telegram",
                        "text_hint": message.caption or None,
                    }
                )
            else:
                logging.warning(
                    "telegram image update has no downloadable file chat=%s message_id=%s",
                    message.chat_id, message.message_id,
                )
                text = "[用户发送了一张 Telegram 图片，但暂时无法读取图片文件]"
        elif message.content_type != "text":
            text = project_non_image_message(message)

        session_id = f"tg:{message.chat_id}"
        text = self._sticker_injector.decorate(session_id, text)
        try:
            result = await self.core.chat(
                session_id=session_id,
                message=text,
                attachments=attachments,
            )
            await self._send_reply(
                message.chat_id,
                str(result["reply"]),
                trace_id=str(result.get("trace_id") or ""),
            )
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def _media_suffix(message: InboundMessage) -> str:
        suffix = Path(message.file_name or "").suffix.lower()
        if suffix and len(suffix) <= 10 and suffix.replace(".", "").isalnum():
            return suffix
        guessed = mimetypes.guess_extension(message.mime_type or "")
        return guessed or ".bin"

    async def _download_file(self, file_id: int, suffix: str) -> Path:
        response = await self.tdlib.request(
            {
                "@type": "downloadFile",
                "file_id": file_id,
                "priority": 32,
                "offset": 0,
                "limit": 0,
                "synchronous": True,
            },
            timeout=self.config.download_timeout_seconds,
        )
        local = response.get("local") or {}
        path = Path(str(local.get("path") or ""))
        if not path.is_file() or path.stat().st_size > self.config.max_media_bytes:
            raise RuntimeError("Telegram media is missing or exceeds configured size")
        temp_root = Path(tempfile.gettempdir()) / "neno-telegram"
        temp_root.mkdir(parents=True, exist_ok=True)
        destination = temp_root / f"{file_id}{suffix}"
        destination.write_bytes(path.read_bytes())
        return destination

    async def _send_confirmed(
        self, request: dict[str, Any], *, chat_id: int, kind: str, detail: str
    ) -> tuple[dict[str, Any], str]:
        """Send one message and observe TDLib's final network result.

        A sendMessage response is only local acceptance. Explicit failures are
        retried once; an acknowledgement timeout is marked uncertain and is not
        retried, because retrying an already-accepted request could duplicate it.
        """
        for attempt in range(1, 3):
            response = await self.tdlib.request(request, timeout=60)
            message_id = response.get("id") if isinstance(response, dict) else None
            try:
                status = await self.tdlib.wait_message_send_result(int(message_id), timeout=8)
            except (asyncio.TimeoutError, TypeError, ValueError):
                logging.warning(
                    "outbound delivery uncertain chat=%s message_id=%s kind=%s detail=%s",
                    chat_id, message_id, kind, detail,
                )
                return response, "uncertain"
            status_type = str(status.get("@type") or "")
            if status_type == "updateMessageSendSucceeded":
                logging.info(
                    "outbound delivery confirmed chat=%s message_id=%s kind=%s detail=%s attempt=%d",
                    chat_id, message_id, kind, detail, attempt,
                )
                return response, "confirmed"
            error = status.get("error") or {}
            code = int(error.get("code") or 0) if str(error.get("code") or "").lstrip("-").isdigit() else 0
            error_message = str(error.get("message") or "")
            logging.error(
                "outbound delivery failed chat=%s message_id=%s kind=%s detail=%s attempt=%d code=%s message=%s",
                chat_id, message_id, kind, detail, attempt, code, error_message,
            )
            if code == 429:
                import re
                match = re.search(r"retry after (\d+)", error_message, re.IGNORECASE)
                raise TelegramFloodWait(int(match.group(1)) if match else 60)
            if attempt == 2:
                return response, "failed"
        raise RuntimeError("unreachable send retry state")

    async def _send_text(self, chat_id: int, text: str) -> int:
        chunks = split_outbound_text(text)
        for index, chunk in enumerate(chunks):
            response, delivery = await self._send_confirmed(
                {
                    "@type": "sendMessage",
                    "chat_id": chat_id,
                    "input_message_content": {
                        "@type": "inputMessageText",
                        "text": {"@type": "formattedText", "text": chunk},
                    },
                },
                chat_id=chat_id,
                kind="text",
                detail=f"chunk={index + 1}/{len(chunks)} chars={len(chunk)}",
            )
            logging.info(
                "outbound chunk sent chat=%s message_id=%s chars=%d delivery=%s",
                chat_id,
                response.get("id") if isinstance(response, dict) else None,
                len(chunk),
                delivery,
            )
            if index + 1 < len(chunks):
                await asyncio.sleep(2.0)
        return len(chunks)


    async def _send_reply(self, chat_id: int, reply: str, trace_id: str = "") -> None:
        """Send one consolidated text message, then any stickers at the end.

        Telegram flood limits apply per message request. Text is therefore no
        longer split on model newlines; sticker markers are removed from the
        text and their real sticker files are sent afterward.
        """
        parts = self.stickers.segment(reply) if self.stickers is not None else []
        if parts:
            text_parts = [part.text.strip() for part in parts if part.kind == "text" and part.text.strip()]
            sticker_parts = [part.entry for part in parts if part.kind == "sticker" and part.entry is not None]
            text = "\n".join(text_parts)
        else:
            text = reply.strip()
            sticker_parts = []

        text_chunks = 0
        sticker_count = 0
        if text:
            if len(text) <= 4096:
                response, delivery = await self._send_confirmed(
                    {
                        "@type": "sendMessage",
                        "chat_id": chat_id,
                        "input_message_content": {
                            "@type": "inputMessageText",
                            "text": {"@type": "formattedText", "text": text},
                        },
                    },
                    chat_id=chat_id,
                    kind="text",
                    detail=f"consolidated chars={len(text)}",
                )
                logging.info(
                    "outbound consolidated text chat=%s message_id=%s chars=%d delivery=%s",
                    chat_id, response.get("id") if isinstance(response, dict) else None, len(text), delivery,
                )
                text_chunks = 1
            else:
                text_chunks = await self._send_text(chat_id, text)

        for entry in sticker_parts:
            await asyncio.sleep(2.0)
            await self._send_sticker(chat_id, entry)
            sticker_count += 1

        logging.info(
            "reply dispatch done chat=%s trace_id=%s mode=consolidated parts=[text,stickers_at_end] "
            "text_chunks=%d stickers=%d reply_len=%d",
            chat_id, trace_id or None, text_chunks, sticker_count, len(reply),
        )


    async def _send_sticker(self, chat_id: int, entry: Any) -> None:
        from telegram_gateway.stickerkit import build_send_photo_request, build_send_sticker_request

        if entry is None or entry.file_path is None or not Path(entry.file_path).is_file():
            return
        try:
            response, delivery = await self._send_confirmed(
                build_send_sticker_request(chat_id, Path(entry.file_path), entry.emoji),
                chat_id=chat_id,
                kind="sticker",
                detail=f"name={entry.name} mode=sticker",
            )
            logging.info(
                "outbound sticker sent chat=%s message_id=%s name=%s mode=sticker delivery=%s",
                chat_id,
                response.get("id") if isinstance(response, dict) else None,
                entry.name,
                delivery,
            )
        except TelegramFloodWait:
            raise
        except Exception:
            try:
                # fall back to a photo so the emotion still lands
                response, delivery = await self._send_confirmed(
                    build_send_photo_request(chat_id, Path(entry.file_path)),
                    chat_id=chat_id,
                    kind="photo_fallback",
                    detail=f"name={entry.name}",
                )
                logging.info(
                    "outbound sticker sent chat=%s message_id=%s name=%s mode=photo_fallback delivery=%s",
                    chat_id,
                    response.get("id") if isinstance(response, dict) else None,
                    entry.name,
                    delivery,
                )
            except TelegramFloodWait:
                raise
            except Exception:
                logging.exception("sticker send failed: %s", entry.name)


async def resend_stored_reply(trace_id: str, gateway: TelegramGateway | None = None) -> None:
    """Re-dispatch one stored assistant reply without writing to Core storage."""
    config = GatewayConfig.from_env()
    row = sqlite3.connect(str(config.database_directory.parent.parent / ".." / "bot.db"))
    try:
        row.row_factory = sqlite3.Row
        message = row.execute(
            "SELECT session_id, content FROM messages "
            "WHERE role = 'assistant' AND trace_id = ? ORDER BY id DESC LIMIT 1",
            (trace_id,),
        ).fetchone()
    finally:
        row.close()
    if message is None:
        raise RuntimeError(f"stored assistant reply not found for trace_id={trace_id}")
    session_id = str(message["session_id"])
    if not session_id.startswith("tg:"):
        raise RuntimeError(f"trace is not a Telegram session: {session_id}")
    chat_id = int(session_id.removeprefix("tg:"))
    if chat_id not in config.allowed_chat_ids:
        raise RuntimeError(f"chat is not in TELEGRAM_ALLOWED_CHAT_IDS: {chat_id}")
    owns_gateway = gateway is None
    gateway = gateway or TelegramGateway(config)
    try:
        if owns_gateway:
            await gateway.start()
        await gateway._send_reply(chat_id, str(message["content"]), trace_id=trace_id)
        logging.info("resend complete trace_id=%s chat=%s storage_write=false", trace_id, chat_id)
    finally:
        if owns_gateway:
            await gateway.close()


async def run_gateway() -> None:
    config = GatewayConfig.from_env()
    gateway = TelegramGateway(config)
    try:
        await gateway.start()
        resend_trace = os.getenv("NENO_RESEND_TRACE", "").strip()
        if resend_trace:
            await resend_stored_reply(resend_trace, gateway)
            return
        await gateway.run_updates()
    finally:
        await gateway.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--resend-trace", default=None)
    args = parser.parse_args()
    asyncio.run(resend_stored_reply(args.resend_trace) if args.resend_trace else run_gateway())

