"""P3b 轮询执行器：拉取 core 的投递项并按类型执行。

不同会话并发（上限 NENO_MIND_MAX_CONCURRENCY），同一会话严格串行。
真实发送以 mind_outbox 的 delivery_id 幂等：core 租约过期重发不会导致
重复 sendMessage。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from telegram_gateway.mind_outbox import MindOutbox
from telegram_gateway.tdlib_client import TdlibError

from telegram_gateway.stickerkit import build_send_photo_request, build_send_sticker_request

_FLOOD_RE = re.compile(r"retry after (\d+)", re.I)
_TYPING_INTERVAL = 4.0
_TYPING_ACTION = {"@type": "sendChatAction", "action": {"@type": "chatActionTyping"}}


class ChatFloodWait(RuntimeError):
    def __init__(self, retry_after: float):
        self.retry_after = max(1.0, float(retry_after))
        super().__init__(f"chat flood wait: retry after {self.retry_after}s")


def _flood_from_error(error: Any) -> ChatFloodWait | None:
    text = str(error)
    code = ""
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        text = str(error.get("message") or text)
    match = _FLOOD_RE.search(text)
    if match or "429" in code or "429" in text:
        return ChatFloodWait(int(match.group(1)) if match else 60)
    return None


def text_chunks(text: str, limit: int = 4096) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [text]


class MindRunner:
    def __init__(self, gateway):
        self.gateway = gateway
        self.outbox = MindOutbox()
        self.stopping = False
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._pause_until: dict[int, float] = {}
        self._presence_task: asyncio.Task | None = None
        self._presence_until = 0.0

    def _lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def stop(self) -> None:
        self.stopping = True

    # ── presence（NENO_MIND_PRESENCE=true 时启用） ─────────────────────────
    async def _presence_on(self) -> None:
        if not getattr(self.gateway.config, "mind_presence", False):
            return
        self._presence_until = time.time() + 60.0
        if self._presence_task is None or self._presence_task.done():
            self._presence_task = asyncio.create_task(self._presence_worker())

    async def _presence_online(self) -> None:
        try:
            await self.gateway.tdlib.request(
                {"@type": "setOption", "name": "online",
                 "value": {"@type": "optionValueBoolean", "value": True}},
                timeout=15,
            )
        except Exception:
            logging.warning("mind presence online set failed")

    async def _presence_offline(self) -> None:
        try:
            await self.gateway.tdlib.request(
                {"@type": "setOption", "name": "online",
                 "value": {"@type": "optionValueBoolean", "value": False}},
                timeout=15,
            )
        except Exception:
            logging.warning("mind presence offline set failed")

    async def _presence_worker(self) -> None:
        # 先上线；最后一个动作完成 60 秒后下线
        await self._presence_online()
        while not self.stopping and time.time() < self._presence_until:
            await asyncio.sleep(1.0)
        await self._presence_offline()

    async def _ensure_offline_at_start(self) -> None:
        if not getattr(self.gateway.config, "mind_presence", False):
            return
        await self._presence_offline()

    # ── 主循环 ─────────────────────────────────────────────────────────────
    async def run(self) -> None:
        await self._ensure_offline_at_start()
        poll = max(0.1, float(getattr(self.gateway.config, "mind_poll_seconds", 1)))
        concurrency = max(1, int(getattr(self.gateway.config, "mind_max_concurrency", 4)))
        semaphore = asyncio.Semaphore(concurrency)

        async def worker(chat_id: int) -> None:
            async with semaphore:
                await self._drain_chat(chat_id)

        while not self.stopping:
            now = time.time()
            chats = [
                chat_id for chat_id in self.gateway.config.allowed_chat_ids
                if self._pause_until.get(chat_id, 0.0) <= now
            ]
            if chats:
                await asyncio.gather(*(worker(chat) for chat in chats))
            await asyncio.sleep(poll)

    async def _drain_chat(self, chat_id: int) -> None:
        session_id = f"tg:{chat_id}"
        async with self._lock(chat_id):
            try:
                items = await self.gateway.core.mind_due(
                    session_id=session_id, limit=200,
                )
            except Exception as exc:
                logging.warning("mind_due failed chat=%s error=%s", chat_id, type(exc).__name__)
                return
            for item in items:
                if self.stopping:
                    return
                if self._pause_until.get(chat_id, 0.0) > time.time():
                    return
                try:
                    await self._execute(chat_id, item)
                except ChatFloodWait as exc:
                    self._pause_until[chat_id] = time.time() + exc.retry_after
                    logging.warning("mind chat paused chat=%s retry_after=%ss", chat_id, exc.retry_after)
                    return
                except Exception as exc:
                    logging.warning("mind delivery failed chat=%s delivery=%s error=%s",
                                    chat_id, item.get("id"), type(exc).__name__)

    # ── 单项执行 ───────────────────────────────────────────────────────────
    async def _execute(self, chat_id: int, item: dict[str, Any]) -> None:
        delivery_id = int(item["id"])
        kind = str(item.get("kind") or "")
        payload = item.get("payload") or {}
        local = self.outbox.begin(delivery_id, str(item.get("session_id") or ""), kind)
        if local is not None:
            # 已经执行/执行过：只补 ack 或继续推进
            await self._resume(chat_id, local, payload)
            return
        self.outbox.increment_attempts(delivery_id)
        await self._presence_on()
        try:
            if kind == "mark_read":
                await self._do_mark_read(chat_id, delivery_id, payload)
            elif kind == "typing":
                await self._do_typing(chat_id, delivery_id, payload)
            elif kind == "text":
                await self._do_text(chat_id, delivery_id, payload)
            elif kind == "sticker":
                await self._do_sticker(chat_id, delivery_id, payload)
            else:
                self.outbox.mark_failed(delivery_id, f"unknown kind: {kind}")
                await self._ack(delivery_id, "failed", error=f"unknown kind: {kind}")
        except ChatFloodWait:
            raise
        except Exception as exc:
            self.outbox.mark_failed(delivery_id, str(exc)[:250])
            await self._ack(delivery_id, "failed", error=str(exc)[:200])

    async def _resume(self, chat_id: int, local: dict[str, Any], payload: dict[str, Any]) -> None:
        delivery_id = int(local["delivery_id"])
        if int(local.get("acked") or 0):
            return
        state = str(local.get("state") or "")
        if state == "sent":
            await self._ack(delivery_id, "sent")
        elif state == "failed":
            await self._ack(delivery_id, "failed", error=str(local.get("error") or "")[:200])
        elif state == "uncertain":
            # 结果不确定：确认之前不 ack、不重发
            logging.warning("mind delivery stays uncertain delivery=%s", delivery_id)
        else:
            logging.info("mind delivery in progress delivery=%s state=%s", delivery_id, state)

    async def _ack(self, delivery_id: int, status: str, error: str | None = None) -> None:
        try:
            await self.gateway.core.mind_ack(delivery_id, status, error=error)
            self.outbox.mark_ack(delivery_id)
        except Exception as exc:
            # ack 失败保持 acked=0，下一轮重试
            logging.warning("mind ack deferred delivery=%s error=%s", delivery_id, type(exc).__name__)

    # ── mark_read ─────────────────────────────────────────────────────────
    async def _do_mark_read(self, chat_id: int, delivery_id: int, payload: dict[str, Any]) -> None:
        trace_ids = [str(t) for t in payload.get("trace_ids") or []]
        message_ids: list[int] = []
        missing = []
        for trace_id in trace_ids:
            row = self.outbox.store.one(
                "SELECT message_id FROM inbox WHERE trace_id=? AND chat_id=?",
                (trace_id, chat_id),
            )
            if row:
                message_ids.append(int(row["message_id"]))
            else:
                missing.append(trace_id)
        if missing:
            # 兜底：该聊天里 created_at <= 当前时刻的最新一条入站消息
            row = self.outbox.store.one(
                """SELECT message_id FROM inbox WHERE chat_id=? AND created_at<=?
                   ORDER BY message_id DESC LIMIT 1""",
                (chat_id, time.time()),
            )
            logging.warning(
                "mark_read trace mapping missing chat=%s missing=%d fallback=%s",
                chat_id, len(missing), row["message_id"] if row else None,
            )
            if row:
                message_ids.append(int(row["message_id"]))
        if message_ids:
            await self.gateway.tdlib.request({
                "@type": "viewMessages",
                "chat_id": chat_id,
                "message_ids": message_ids,
                "source": {"@type": "messageSourceChatHistory"},
                "force_read": True,
            }, timeout=30)
        self.outbox.mark_sent(delivery_id)
        await self._ack(delivery_id, "sent")

    # ── typing ────────────────────────────────────────────────────────────
    async def _do_typing(self, chat_id: int, delivery_id: int, payload: dict[str, Any]) -> None:
        duration = max(1.0, float(payload.get("duration") or 1.0))
        action = dict(_TYPING_ACTION)
        action["chat_id"] = chat_id
        deadline = time.monotonic() + duration
        sent = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await self.gateway.tdlib.request(action, timeout=15)
            except TdlibError as exc:
                flood = _flood_from_error(str(exc))
                if flood:
                    raise flood
            sent += 1
            await asyncio.sleep(min(_TYPING_INTERVAL, max(0.1, remaining)))
        self.outbox.mark_sent(delivery_id)
        await self._ack(delivery_id, "sent")

    # ── text ──────────────────────────────────────────────────────────────
    async def _do_text(self, chat_id: int, delivery_id: int, payload: dict[str, Any]) -> None:
        text = str(payload.get("text") or "")
        parts = text_chunks(text)
        for index, part in enumerate(parts):
            request = {
                "@type": "sendMessage",
                "chat_id": chat_id,
                "input_message_content": {
                    "@type": "inputMessageText",
                    "text": {"@type": "formattedText", "text": part},
                },
            }
            try:
                response = await self.gateway.tdlib.request(request, timeout=60)
            except TdlibError as exc:
                flood = _flood_from_error(str(exc))
                if flood:
                    raise flood
                self.outbox.mark_failed(delivery_id, str(exc)[:250])
                await self._ack(delivery_id, "failed", error=str(exc)[:200])
                return
            local_id = int(response.get("id") or 0)
            if index == 0:
                self.outbox.mark_uncertain(delivery_id, local_id=local_id)
            try:
                status = await self.gateway.tdlib.wait_message_send_result(local_id, timeout=8)
            except asyncio.TimeoutError:
                # 结果不确定：确认之前不 ack、不重发
                logging.warning("mind text uncertain chat=%s delivery=%s local_id=%s",
                                chat_id, delivery_id, local_id)
                return
            status_type = str(status.get("@type") or "")
            if status_type == "updateMessageSendFailed":
                flood = _flood_from_error(status.get("error") or {})
                if flood:
                    raise flood
                error = status.get("error") or {}
                self.outbox.mark_failed(delivery_id, json.dumps(error)[:250])
                await self._ack(delivery_id, "failed", error=str(error.get("message") or "")[:200])
                return
        self.outbox.mark_sent(delivery_id)
        await self._ack(delivery_id, "sent")

    # ── sticker ───────────────────────────────────────────────────────────
    async def _do_sticker(self, chat_id: int, delivery_id: int, payload: dict[str, Any]) -> None:
        name = str(payload.get("name") or "").strip()
        table = getattr(self.gateway, "stickers", None)
        entry = table.resolve(name) if table is not None else None
        if entry is None or entry.file_path is None:
            self.outbox.mark_failed(delivery_id, "unknown sticker")
            await self._ack(delivery_id, "failed", error="unknown sticker")
            return
        request = None
        try:
            request = build_send_sticker_request(chat_id, entry.file_path, entry.emoji)
        except Exception:
            request = build_send_photo_request(chat_id, entry.file_path)
        try:
            response = await self.gateway.tdlib.request(request, timeout=60)
        except TdlibError as exc:
            flood = _flood_from_error(str(exc))
            if flood:
                raise flood
            try:
                fallback = build_send_photo_request(chat_id, entry.file_path)
                response = await self.gateway.tdlib.request(fallback, timeout=60)
            except TdlibError as inner:
                flood = _flood_from_error(str(inner))
                if flood:
                    raise flood
                self.outbox.mark_failed(delivery_id, str(inner)[:250])
                await self._ack(delivery_id, "failed", error=str(inner)[:200])
                return
        local_id = int(response.get("id") or 0)
        self.outbox.mark_uncertain(delivery_id, local_id=local_id)
        try:
            status = await self.gateway.tdlib.wait_message_send_result(local_id, timeout=8)
        except asyncio.TimeoutError:
            logging.warning("mind sticker uncertain chat=%s delivery=%s", chat_id, delivery_id)
            return
        if str(status.get("@type") or "") == "updateMessageSendFailed":
            error = status.get("error") or {}
            self.outbox.mark_failed(delivery_id, json.dumps(error)[:250])
            await self._ack(delivery_id, "failed", error=str(error.get("message") or "")[:200])
            return
        self.outbox.mark_sent(delivery_id)
        await self._ack(delivery_id, "sent")
