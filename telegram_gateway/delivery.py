"""Persistent inbox/outbox. Unknown acceptance is reconciled, never blindly resent."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict
from pathlib import Path

from app.storage.operations import operations
from telegram_gateway.normalize import InboundMessage, project_non_image_message
from telegram_gateway.stickerkit import build_send_photo_request, build_send_sticker_request
from telegram_gateway.tdlib_client import TdlibError


def chunks(text, limit=4096):
    part, size = [], 0
    for char in text:
        width = len(char.encode('utf-16-le')) // 2
        if size + width > limit:
            yield ''.join(part)
            part, size = [], 0
        part.append(char)
        size += width
    if part:
        yield ''.join(part)


class Delivery:
    def __init__(self, gateway):
        self.gateway, self.store = gateway, operations()
        self.stopping = False
        self.store.write("UPDATE outbox SET state='uncertain' WHERE state='submitting'")

    def accept(self, message):
        key = f'tg:{message.chat_id}:{message.message_id}'
        self.store.write('INSERT OR IGNORE INTO inbox(key,chat_id,message_id,data,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                         (key, message.chat_id, message.message_id, json.dumps(asdict(message), ensure_ascii=False), time.time(), time.time()))

    def receipt(self, update):
        kind = update.get('@type')
        if kind not in {'updateMessageSendSucceeded', 'updateMessageSendFailed'}:
            return
        message = update.get('message') or {}
        old_id = update.get('old_message_id')
        if old_id is None:
            return
        error = update.get('error') or {}
        state = 'confirmed' if kind.endswith('Succeeded') else 'failed'
        wait = re.search(r'retry after (\d+)', str(error.get('message', '')), re.I)
        delay = int(wait.group(1)) if wait else 60
        self.store.write('''UPDATE outbox SET state=?,final_id=?,error=?,next_at=?,updated_at=?
          WHERE chat_id=? AND local_id=? AND state NOT IN ('cancelled','confirmed')''',
          (state, message.get('id'), json.dumps(error) if error else None,
           time.time() + delay, time.time(), message.get('chat_id'), old_id))

    async def prepare(self, row):
        payload = json.loads(row['payload']) if row['payload'] else None
        if payload is None:
            msg = InboundMessage(**json.loads(row['data']))
            attachments, text = [], msg.text
            if msg.content_type == 'image' and msg.file_id:
                path = await self.gateway._download_file(msg.file_id, self.gateway._media_suffix(msg))
                attachments.append({'kind': 'image', 'media_path': str(path), 'source': 'telegram', 'text_hint': msg.caption or None})
            elif msg.content_type != 'text':
                text = project_non_image_message(msg)
            session_id = f'tg:{msg.chat_id}'
            payload = {'session_id': session_id, 'message': self.gateway._sticker_injector.decorate(session_id, text),
                       'attachments': attachments, 'request_id': row['key']}
            self.store.write("UPDATE inbox SET payload=?,state='generating',updated_at=? WHERE key=? AND state<>'cancelled'", (json.dumps(payload, ensure_ascii=False), time.time(), row['key']))
        result = await self.gateway.core.chat(**payload)
        if result.get('status') == 'accepted':
            # 异步模式：core 已受理，投递由 mind 轮询执行器接管；不规划旧 outbox
            self.store.write("UPDATE inbox SET trace_id=?,state='done',error=NULL,updated_at=? WHERE key=? AND state<>'cancelled'",
                             (result.get('trace_id'), time.time(), row['key']))
            await self._retract_if_pending(row['key'], result.get('trace_id'))
            for attachment in payload['attachments']:
                if attachment.get('media_path'):
                    Path(attachment['media_path']).unlink(missing_ok=True)
            return
        self.store.write("UPDATE inbox SET reply=?,trace_id=?,state='ready',error=NULL,updated_at=? WHERE key=? AND state<>'cancelled'",
                         (result['reply'], result.get('trace_id'), time.time(), row['key']))
        await self._retract_if_pending(row['key'], result.get('trace_id'))
        # Core has copied originals into the durable media store at this point.
        for attachment in payload['attachments']:
            if attachment.get('media_path'):
                Path(attachment['media_path']).unlink(missing_ok=True)

    def plan(self, row):
        parts = self.gateway.stickers.segment(row['reply']) if self.gateway.stickers else []
        text = '\n'.join(p.text.strip() for p in parts if p.kind == 'text' and p.text.strip()) if parts else row['reply'].strip()
        requests = [{'primary': {'@type': 'sendMessage', 'chat_id': row['chat_id'], 'input_message_content': {
            '@type': 'inputMessageText', 'text': {'@type': 'formattedText', 'text': chunk}}}} for chunk in chunks(text)]
        for part in parts:
            if part.kind != 'sticker' or part.entry is None:
                continue
            entry = part.entry
            fallback = build_send_photo_request(row['chat_id'], Path(entry.file_path))
            try:
                primary = build_send_sticker_request(row['chat_id'], Path(entry.file_path), entry.emoji)
            except Exception:
                primary, fallback = fallback, None
            requests.append({'primary': primary, 'fallback': fallback})
        if not requests:
            raise ValueError('Reply has no deliverable text or media')
        with self.store.connect() as c:
            current = c.execute('SELECT state FROM inbox WHERE key=?', (row['key'],)).fetchone()
            if not current or current[0] == 'cancelled':
                return
            for seq, request in enumerate(requests):
                c.execute('INSERT OR IGNORE INTO outbox(key,inbox_key,seq,chat_id,request,updated_at) VALUES (?,?,?,?,?,?)',
                          (f'{row["key"]}:{seq}', row['key'], seq, row['chat_id'], json.dumps(request, ensure_ascii=False), time.time()))
            c.execute("UPDATE inbox SET state='sending',updated_at=? WHERE key=?", (time.time(), row['key']))

    async def reconcile(self, row):
        message_id = row['final_id'] or row['local_id']
        if message_id is None:
            self.store.write("UPDATE outbox SET state='uncertain',next_at=?,error='Acceptance unknown; inspect Telegram before resolving' WHERE key=?", (time.time() + 300, row['key']))
            self.store.write("UPDATE inbox SET state='needs_attention',error='Unknown send acceptance: inspect Telegram and resolve',updated_at=? WHERE key=? AND state<>'cancelled'", (time.time(), row['inbox_key']))
            return
        try:
            message = await self.gateway.tdlib.request({'@type': 'getMessage', 'chat_id': row['chat_id'], 'message_id': message_id}, timeout=15)
            sending = message.get('sending_state')
            if not sending and message.get('is_outgoing') and message.get('chat_id') == row['chat_id']:
                expected = json.loads(row['request'])['primary']['input_message_content']
                if expected['@type'] == 'inputMessageText':
                    actual = (message.get('content', {}).get('text') or {}).get('text')
                    if actual != expected['text']['text']:
                        raise ValueError('Resolved message content does not match this outbox item')
                elif row['final_id'] and not row['local_id']:
                    confirmation = json.loads(row['baseline'] or '{}')
                    actual_kind = (message.get('content') or {}).get('@type')
                    expected_kind = {'inputMessageSticker': 'messageSticker', 'inputMessagePhoto': 'messagePhoto'}.get(expected['@type'])
                    if not confirmation.get('manual_media_confirmation') or actual_kind != expected_kind:
                        raise ValueError('Media without a known local ID needs explicit manual confirmation and matching media type')
                if row['final_id'] and not row['local_id'] and message.get('date', 0) < (row['submitted_at'] or 0) - 30:
                    raise ValueError('Resolved message predates this send attempt')
                self.store.write("UPDATE outbox SET state='confirmed',final_id=?,error=NULL,updated_at=? WHERE key=? AND state<>'cancelled'", (message['id'], time.time(), row['key']))
                return
            if sending and sending.get('@type') == 'messageSendingStateFailed':
                self.store.write("UPDATE outbox SET state='failed',error=?,next_at=?,updated_at=? WHERE key=? AND state<>'cancelled'", (json.dumps(sending), time.time() + 60, time.time(), row['key']))
                return
        except Exception as exc:
            self.store.write('UPDATE outbox SET error=? WHERE key=?', (str(exc)[:250], row['key']))
        self.store.write('UPDATE outbox SET next_at=? WHERE key=?', (time.time() + 30, row['key']))

    async def send(self, row):
        if row['state'] in {'uncertain', 'accepted'}:
            return await self.reconcile(row)
        if row['attempts'] >= 6:
            self.store.write("UPDATE outbox SET state='needs_attention' WHERE key=?", (row['key'],))
            return
        envelope = json.loads(row['request'])
        if not self.store.write("UPDATE outbox SET state='submitting',attempts=attempts+1,submitted_at=?,updated_at=? WHERE key=? AND state IN ('pending','failed')", (time.time(), time.time(), row['key'])):
            return
        try:
            response = await self.gateway.tdlib.request(envelope['primary'], timeout=60)
        except TdlibError as exc:
            # An explicit error response means TDLib did not accept this request.
            wait = re.search(r'retry after (\d+)', str(exc), re.I)
            if envelope.get('fallback') and '429' not in str(exc):
                envelope = {'primary': envelope['fallback']}
            self.store.write("UPDATE outbox SET state='failed',request=?,error=?,next_at=?,updated_at=? WHERE key=? AND state<>'cancelled'",
                             (json.dumps(envelope), str(exc)[:250], time.time() + (int(wait.group(1)) if wait else min(300, 15 * 2 ** row['attempts'])), time.time(), row['key']))
            return
        except Exception as exc:
            self.store.write("UPDATE outbox SET state='uncertain',error=?,next_at=? WHERE key=? AND state<>'cancelled'", (type(exc).__name__, time.time() + 30, row['key']))
            return
        local_id = response.get('id')
        if local_id is None:
            self.store.write("UPDATE outbox SET state='uncertain',error='TDLib accepted without message ID',next_at=? WHERE key=?", (time.time() + 300, row['key']))
            return
        self.store.write("UPDATE outbox SET state='accepted',local_id=?,final_id=NULL,updated_at=? WHERE key=? AND state<>'cancelled'", (local_id, time.time(), row['key']))
        try:
            update = await self.gateway.tdlib.wait_message_send_result(local_id, timeout=8)
            self.receipt(update)
        except asyncio.TimeoutError:
            self.store.write("UPDATE outbox SET state='uncertain',next_at=? WHERE key=? AND state='accepted'", (time.time() + 30, row['key']))

    async def run(self):
        while not self.stopping:
            delay = 1
            rows = self.store.all("""SELECT i.* FROM inbox i WHERE i.state NOT IN ('done','cancelled','needs_attention') AND i.next_at<=?
              AND NOT EXISTS (SELECT 1 FROM inbox older WHERE older.chat_id=i.chat_id AND older.created_at<i.created_at
                AND older.state NOT IN ('done','cancelled','needs_attention')) ORDER BY i.created_at LIMIT 20""", (time.time(),))
            for row in rows:
                try:
                    if not row['reply']:
                        await self.prepare(row)
                        row = self.store.one('SELECT * FROM inbox WHERE key=?', (row['key'],))
                    if row['state'] == 'cancelled':
                        continue
                    if row['reply'] and not self.store.one('SELECT 1 FROM outbox WHERE inbox_key=?', (row['key'],)):
                        self.plan(row)
                    part = self.store.one("SELECT * FROM outbox WHERE inbox_key=? AND state NOT IN ('confirmed','cancelled') ORDER BY seq LIMIT 1", (row['key'],))
                    if part and part['next_at'] <= time.time() and part['state'] != 'needs_attention':
                        await self.send(part)
                        await asyncio.sleep(2)
                        delay = 0
                    if part and part['state'] == 'needs_attention':
                        self.store.write("UPDATE inbox SET state='needs_attention',error='Delivery needs operator attention',updated_at=? WHERE key=? AND state<>'cancelled'", (time.time(), row['key']))
                    if not part and self.store.one('SELECT 1 FROM outbox WHERE inbox_key=?', (row['key'],)):
                        self.store.write("UPDATE inbox SET state='done',error=NULL,updated_at=? WHERE key=? AND state<>'cancelled'", (time.time(), row['key']))
                except Exception as exc:
                    attempts = row['attempts'] + 1
                    self.store.write('UPDATE inbox SET state=?,attempts=?,error=?,next_at=?,updated_at=? WHERE key=? AND state NOT IN (\'cancelled\',\'done\')',
                                     ('needs_attention' if attempts >= 10 else 'retryable', attempts, str(exc)[:250], time.time() + min(300, 15 * 2 ** min(attempts, 5)), time.time(), row['key']))
                    logging.warning('Delivery deferred key=%s error=%s', row['key'], type(exc).__name__)
            await asyncio.sleep(delay)

    async def _retract_if_pending(self, key, trace_id):
        """generating 中被标记撤回的消息：拿到 trace_id 后立刻补调 retract。"""
        row = self.store.one('SELECT retract_state FROM inbox WHERE key=?', (key,))
        if not row or row.get('retract_state') != 'pending' or not trace_id:
            return
        try:
            await self.gateway.core.mind_retract(str(trace_id))
            self.store.write("UPDATE inbox SET retract_state='done',updated_at=? WHERE key=?",
                             (time.time(), key))
        except Exception as exc:
            self.store.write("UPDATE inbox SET retract_state='retry',error=?,updated_at=? WHERE key=?",
                             (str(exc)[:250], time.time(), key))
