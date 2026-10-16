"""P3b 轮询执行器：拉取 core 的投递项并按类型执行。

每个会话一个独立循环（各自 sleep 轮询间隔），执行时用 Semaphore 限制并发；
同一会话严格串行。真实发送以 mind_outbox 的 delivery_id 幂等，状态机为
pending → submitting → sent / failed / uncertain / needs_attention。
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


class _ChatPaused(RuntimeError):
    """会话处于 429 暂停期：中断当前批，由该会话的独立循环等暂停结束。"""


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
        self._chat_loops: dict[int, asyncio.Task] = {}
        self._retract_task: asyncio.Task | None = None
        self._presence_task: asyncio.Task | None = None
        self._presence_until = 0.0
        # 正在等 TDLib 发送结果的项：回执由等待方处理，receipt() 不插手
        self._inflight: set[int] = set()

    def _lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def stop(self) -> None:
        self.stopping = True
        tasks = [*self._chat_loops.values()]
        if self._retract_task is not None:
            tasks.append(self._retract_task)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ── presence（NENO_MIND_PRESENCE=true 时启用） ─────────────────────────
    async def _presence_on(self) -> None:
        if not getattr(self.gateway.config, "mind_presence", False):
            return
        linger = max(1.0, float(getattr(self.gateway.config, "mind_presence_linger_sec", 60)))
        self._presence_until = time.time() + linger
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
        # 先上线；最后一个动作完成 NENO_MIND_PRESENCE_LINGER_SEC 秒后下线
        await self._presence_online()
        while not self.stopping and time.time() < self._presence_until:
            await asyncio.sleep(1.0)
        await self._presence_offline()

    async def _ensure_offline_at_start(self) -> None:
        if not getattr(self.gateway.config, "mind_presence", False):
            return
        await self._presence_offline()

    # ── 主循环：每会话独立轮询 + 并发上限 ─────────────────────────────────
    async def run(self) -> None:
        await self._ensure_offline_at_start()
        poll = max(0.1, float(getattr(self.gateway.config, "mind_poll_seconds", 1)))
        concurrency = max(1, int(getattr(self.gateway.config, "mind_max_concurrency", 4)))
        semaphore = asyncio.Semaphore(concurrency)
        self._retract_task = asyncio.create_task(self._retract_retry_loop(), name="mind-retract-retry")

        def spawn(chat_id: int) -> None:
            task = self._chat_loops.get(chat_id)
            if task is None or task.done():
                self._chat_loops[chat_id] = asyncio.create_task(
                    self._chat_loop(chat_id, semaphore), name=f"mind-runner-{chat_id}")

        for chat_id in self.gateway.config.allowed_chat_ids:
            spawn(chat_id)
        while not self.stopping:
            # allowed 列表变化时补新循环；已有循环各自 sleep，不再每轮 gather
            for chat_id in self.gateway.config.allowed_chat_ids:
                spawn(chat_id)
            await asyncio.sleep(max(poll, 1.0))

    async def _chat_loop(self, chat_id: int, semaphore: asyncio.Semaphore) -> None:
        poll = max(0.1, float(getattr(self.gateway.config, "mind_poll_seconds", 1)))
        while not self.stopping:
            pause = self._pause_until.get(chat_id, 0.0)
            now = time.time()
            if pause > now:
                await asyncio.sleep(max(0.1, min(poll, pause - now)))
                continue
            async with semaphore:
                await self._drain_chat(chat_id)
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
                except _ChatPaused:
                    return
                except Exception as exc:
                    logging.warning("mind delivery failed chat=%s delivery=%s error=%s",
                                    chat_id, item.get("id"), type(exc).__name__)

    # ── 撤回重试（inbox.retract_state='retry' 持久化） ──────────────────────
    async def _retract_retry_loop(self) -> None:
        while not self.stopping:
            try:
                await self._retry_retracts_once()
            except Exception as exc:
                logging.warning("mind retract retry sweep failed error=%s", type(exc).__name__)
            await asyncio.sleep(10.0)

    async def _retry_retracts_once(self) -> None:
        rows = self.outbox.store.all(
            "SELECT key, trace_id FROM inbox WHERE retract_state='retry' AND trace_id IS NOT NULL"
        )
        for row in rows:
            try:
                await self.gateway.core.mind_retract(str(row["trace_id"]))
                self.outbox.store.write(
                    "UPDATE inbox SET retract_state='done',error=NULL,updated_at=? WHERE key=?",
                    (time.time(), row["key"]),
                )
            except Exception as exc:
                logging.warning("retract retry failed key=%s error=%s", row["key"], type(exc).__name__)

    # ── 单项执行与恢复 ──────────────────────────────────────────────────────
    async def _execute(self, chat_id: int, item: dict[str, Any]) -> None:
        delivery_id = int(item["id"])
        kind = str(item.get("kind") or "")
        payload = item.get("payload") or {}
        payload["send_at"] = payload.get("send_at") or item.get("send_at") or 0.0
        local = self.outbox.begin(
            delivery_id, str(item.get("session_id") or ""), kind,
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        if local is not None:
            # 已经执行/执行过：只补 ack、核对或按状态推进
            await self._resume(chat_id, local, payload)
            return
        await self._presence_on()
        await self._do_kind(chat_id, delivery_id, kind, payload)

    async def _do_kind(self, chat_id: int, delivery_id: int, kind: str,
                       payload: dict[str, Any]) -> None:
        if kind in ("text", "sticker"):
            # 交给 TDLib 之前先落 submitting（attempts+1）；崩溃恢复按 uncertain 处理
            self.outbox.mark_submitting(delivery_id)
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
        except ChatFloodWait as exc:
            # 尚未确认交给 TDLib：退回 pending，暂停结束后由独立循环重新执行
            self.outbox.mark_pending(delivery_id, error="chat flood wait")
            self._pause_until[chat_id] = time.time() + float(exc.retry_after)
            raise
        except _ChatPaused:
            raise
        except Exception as exc:
            self.outbox.mark_failed(delivery_id, str(exc)[:250])
            await self._ack(delivery_id, "failed", error=str(exc)[:200])

    async def _resume(self, chat_id: int, local: dict[str, Any],
                      payload: dict[str, Any]) -> None:
        delivery_id = int(local["delivery_id"])
        if int(local.get("acked") or 0):
            return
        kind = str(local.get("kind") or "")
        state = str(local.get("state") or "")
        if state == "sent":
            await self._ack(delivery_id, "sent")
        elif state == "failed":
            await self._ack(delivery_id, "failed", error=str(local.get("error") or "")[:200])
        elif state in ("uncertain", "submitting"):
            # 崩在发送途中/结果不确定：getMessage 核对，不重发
            await self._reconcile_uncertain(chat_id, local)
        elif state == "needs_attention":
            return
        elif state == "pending":
            saved = json.loads(local.get("payload_json") or "{}")
            merged = {**saved, **payload} if payload else saved
            # mark_read/typing 重复执行无副作用；text/sticker 是 429 退回的未提交项
            await self._presence_on()
            await self._do_kind(chat_id, delivery_id, kind, merged)
        else:
            logging.info("mind delivery in progress delivery=%s state=%s", delivery_id, state)

    # ── 回执：与旧 outbox 账本并行同步 ────────────────────────────────────
    def receipt(self, update: dict[str, Any]) -> None:
        kind = str(update.get("@type") or "")
        if kind not in {"updateMessageSendSucceeded", "updateMessageSendFailed"}:
            return
        old_message_id = update.get("old_message_id")
        if old_message_id is None:
            return
        row = self.outbox.find_by_local_id(int(old_message_id))
        if row is None:
            return
        delivery_id = int(row["delivery_id"])
        if delivery_id in self._inflight:
            # _do_text/_do_sticker 正在 wait_message_send_result，结果由它们处理
            return
        message = update.get("message") or {}
        chat_id = int(message.get("chat_id") or 0)
        if kind == "updateMessageSendSucceeded":
            self.outbox.mark_sent(delivery_id, final_id=message.get("id"))
            self._schedule_ack(delivery_id, "sent")
            return
        error = update.get("error") or {}
        flood = _flood_from_error(error)
        if flood is not None:
            self.outbox.mark_pending(delivery_id, error="chat flood wait")
            if chat_id:
                self._pause_until[chat_id] = time.time() + flood.retry_after
            return
        self.outbox.mark_failed(delivery_id, json.dumps(error)[:250])
        self._schedule_ack(delivery_id, "failed", error=str(error.get("message") or "")[:200])

    def _schedule_ack(self, delivery_id: int, status: str, error: str | None = None) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._ack(delivery_id, status, error=error))

    # ── uncertain/submitting 核对 ──────────────────────────────────────────
    async def _reconcile_uncertain(self, chat_id: int, local: dict[str, Any]) -> None:
        delivery_id = int(local["delivery_id"])
        kind = str(local.get("kind") or "")
        message_id = local.get("final_id") or local.get("local_id")
        if not message_id:
            self.outbox.mark_needs_attention(delivery_id, "uncertain without local_id")
            logging.error("mind delivery needs_attention delivery=%s", delivery_id)
            return
        max_sec = float(getattr(self.gateway.config, "mind_uncertain_max_sec", 600))
        age = time.time() - float(local.get("updated_at") or 0)
        if age > max_sec:
            self.outbox.mark_needs_attention(delivery_id, "uncertain unresolved")
            logging.error("mind delivery needs_attention delivery=%s", delivery_id)
            return
        try:
            response = await self.gateway.tdlib.request(
                {"@type": "getMessage", "chat_id": chat_id, "message_id": int(message_id)},
                timeout=30,
            )
            message = response if str(response.get("@type") or "") != "error" else None
        except Exception:
            message = None
        if message is not None and message.get("id"):
            sending_state = str((message.get("sending_state") or {}).get("@type") or "")
            if sending_state == "messageSendingStateFailed":
                self.outbox.mark_failed(delivery_id, "confirmed failed by getMessage")
                await self._ack(delivery_id, "failed", error="confirmed failed by getMessage")
                return
            if sending_state:
                return  # 仍在 sending/pending，下一轮再查
            if message.get("is_outgoing") is not True:
                return
            if self._message_matches(kind, message):
                self.outbox.mark_sent(delivery_id, final_id=message.get("id"))
                await self._ack(delivery_id, "sent")
                return
        # getMessage 报错 / local_id 已失效：按提交时间+内容在历史里兜底匹配
        matched = await self._match_history(chat_id, local)
        if matched is not None:
            self.outbox.mark_sent(delivery_id, final_id=matched)
            await self._ack(delivery_id, "sent")

    async def _match_history(self, chat_id: int, local: dict[str, Any]) -> int | None:
        kind = str(local.get("kind") or "")
        payload = json.loads(local.get("payload_json") or "{}")
        submitted_at = float(local.get("submitted_at") or 0.0)
        if submitted_at <= 0:
            return None
        floor = submitted_at - 30
        try:
            response = await self.gateway.tdlib.request({
                "@type": "getChatHistory",
                "chat_id": chat_id,
                "from_message_id": 0,
                "offset": 0,
                "limit": 30,
            }, timeout=30)
        except Exception as exc:
            logging.warning("mind reconcile getChatHistory failed delivery=%s error=%s",
                            local.get("delivery_id"), type(exc).__name__)
            return None
        taken = self.outbox.final_ids()
        expected = ""
        if kind == "text":
            chunks = text_chunks(str(payload.get("text") or ""))
            expected = chunks[-1]
        for message in (response.get("messages") or []):
            try:
                message_id = int(message.get("id"))
            except (TypeError, ValueError):
                continue
            if message_id in taken:
                continue
            if message.get("is_outgoing") is not True:
                continue
            if int(message.get("date") or 0) < floor:
                continue
            if message.get("sending_state"):
                continue
            if not self._message_matches(kind, message):
                continue
            if kind == "text":
                content = message.get("content") or {}
                text = ((content.get("text") or {}).get("text") or "")
                if str(text) != expected:
                    continue
            return message_id
        return None

    @staticmethod
    def _message_matches(kind: str, message: dict[str, Any]) -> bool:
        content = message.get("content") or {}
        content_type = str(content.get("@type") or "")
        if kind == "sticker":
            return content_type in {"messageSticker", "messagePhoto"}
        if kind == "text":
            return content_type == "messageText"
        return True

    async def _ack(self, delivery_id: int, status: str, error: str | None = None) -> None:
        try:
            await self.gateway.core.mind_ack(delivery_id, status, error=error)
            self.outbox.mark_ack(delivery_id)
        except Exception as exc:
            # ack 失败保持 acked=0，下一轮重试
            logging.warning("mind ack deferred delivery=%s error=%s", delivery_id, type(exc).__name__)

    # ── mark_read ─────────────────────────────────────────────────────────
    async def _do_mark_read(self, chat_id: int, delivery_id: int,
                            payload: dict[str, Any]) -> None:
        trace_ids = [str(t) for t in payload.get("trace_ids") or []]
        send_at = float(payload.get("send_at") or time.time())
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
            # 兜底锚 send_at：只标已读时点之前到达的消息，不看当前时刻
            row = self.outbox.store.one(
                """SELECT message_id FROM inbox WHERE chat_id=? AND created_at<=?
                   ORDER BY message_id DESC LIMIT 1""",
                (chat_id, send_at),
            )
            logging.warning(
                "mark_read trace mapping missing chat=%s missing=%d fallback=%s anchor=%s",
                chat_id, len(missing), row["message_id"] if row else None, send_at,
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
    async def _do_typing(self, chat_id: int, delivery_id: int,
                         payload: dict[str, Any]) -> None:
        duration = max(1.0, float(payload.get("duration") or 1.0))
        action = dict(_TYPING_ACTION)
        action["chat_id"] = chat_id
        deadline = time.monotonic() + duration
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
            await asyncio.sleep(min(_TYPING_INTERVAL, max(0.1, remaining)))
        self.outbox.mark_sent(delivery_id)
        await self._ack(delivery_id, "sent")

    # ── text ──────────────────────────────────────────────────────────────
    async def _do_text(self, chat_id: int, delivery_id: int,
                       payload: dict[str, Any]) -> None:
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
            self.outbox.set_local_id(delivery_id, local_id)
            self.outbox.mark_uncertain(delivery_id, local_id=local_id)
            self._inflight.add(delivery_id)
            try:
                status = await self.gateway.tdlib.wait_message_send_result(local_id, timeout=8)
            except asyncio.TimeoutError:
                logging.warning("mind text uncertain chat=%s delivery=%s local_id=%s",
                                chat_id, delivery_id, local_id)
                return
            finally:
                self._inflight.discard(delivery_id)
            self._apply_send_result(delivery_id, status)
            if str(status.get("@type") or "") == "updateMessageSendFailed":
                return
        # 所有分块都确认成功才置 sent 并 ack
        self.outbox.mark_sent(delivery_id)
        await self._ack(delivery_id, "sent")

    def _apply_send_result(self, delivery_id: int, status: dict[str, Any]) -> None:
        status_type = str(status.get("@type") or "")
        if status_type == "updateMessageSendSucceeded":
            message = status.get("message") or {}
            self.outbox.mark_sent(delivery_id, final_id=message.get("id"))
        elif status_type == "updateMessageSendFailed":
            error = status.get("error") or {}
            flood = _flood_from_error(error)
            if flood is not None:
                # 限流：退回 pending，暂停会话，结束后重新执行
                self.outbox.mark_pending(delivery_id, error="chat flood wait")
                raise flood
            self.outbox.mark_failed(delivery_id, json.dumps(error)[:250])
            self._schedule_ack(delivery_id, "failed", error=str(error.get("message") or "")[:200])

    # ── sticker ───────────────────────────────────────────────────────────
    async def _do_sticker(self, chat_id: int, delivery_id: int,
                          payload: dict[str, Any]) -> None:
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
        self._inflight.add(delivery_id)
        try:
            status = await self.gateway.tdlib.wait_message_send_result(local_id, timeout=8)
        except asyncio.TimeoutError:
            logging.warning("mind sticker uncertain chat=%s delivery=%s", chat_id, delivery_id)
            return
        finally:
            self._inflight.discard(delivery_id)
        self._apply_send_result(delivery_id, status)
        if str(status.get("@type") or "") == "updateMessageSendFailed":
            return
        self.outbox.mark_sent(delivery_id)
        await self._ack(delivery_id, "sent")

