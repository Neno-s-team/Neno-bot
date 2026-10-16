"""Offline integration checks: temporary databases, fake providers and fake TDLib."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['CONTEXT_SUMMARY_ENABLED'] = 'true'
os.environ['WORLD_ENABLED'] = 'false'


def run():
    checks = []
    with tempfile.TemporaryDirectory(prefix='neno-ops-') as temp:
        base = Path(temp)
        from app.storage import db, image_store
        from app.storage import operations as ops
        from app.services import chat_service
        from app.services.chat import maintenance, idempotency, llm_gateway
        from app.schemas import ChatRequest
        from app import config
        from app.routers import chat as chat_router
        from app.services import usage
        from telegram_gateway.delivery import Delivery, chunks
        from telegram_gateway.normalize import InboundMessage
        from telegram_gateway.tdlib_client import TdlibError

        db.DB_DIR = base / 'data'
        db.DB_DIR.mkdir()
        db.DB_PATH = db.DB_DIR / 'bot.db'
        ops._instance = ops.Operations(db.DB_DIR / 'operations.db')
        store = ops.operations()
        db.init_db()

        def generate(messages, **kwargs):
            return 'offline reply'

        req = dict(session_id='verify', message='hello', request_id='verify-1')
        with patch.object(chat_service, 'generate_chat_reply', side_effect=generate) as model:
            first = chat_router.chat(ChatRequest(**req))
            again = chat_router.chat(ChatRequest(**req))
            assert first['assistant_message_id'] == again['assistant_message_id'] and model.call_count == 1
            assert db.fetch_one("SELECT COUNT(*) AS n FROM messages WHERE session_id='verify'")['n'] == 2
            store.write("UPDATE requests SET status='processing',result=NULL WHERE request_id='verify-1'")
            recovered = chat_router.chat(ChatRequest(**req))
            assert recovered['reply'] == first['reply'] and model.call_count == 1
        checks.append('request retry and lost HTTP result reuse one persisted reply')

        trace = 'req_' + hashlib.sha256(b'verify-partial').hexdigest()[:28]
        old_user = db.add_message('partial', 'user', 'partial input', trace_id=trace)
        with patch.object(chat_service, 'generate_chat_reply', side_effect=generate):
            result = chat_router.chat(ChatRequest(session_id='partial', message='partial input', request_id='verify-partial'))
            assert result['user_message_id'] == old_user
            assert db.fetch_one("SELECT COUNT(*) AS n FROM messages WHERE session_id='partial'")['n'] == 2
        checks.append('interrupted turn reuses its user row')

        def clear_during_generation(messages, **kwargs):
            db.clear_session_messages('clear-inflight')
            return 'must not be saved'

        with patch.object(chat_service, 'generate_chat_reply', side_effect=clear_during_generation):
            try:
                chat_service.run_chat_turn('clear-inflight', 'temporary input', trace_id='clear-inflight')
            except RuntimeError:
                pass
            else:
                raise AssertionError('Cleared turn was revived')
        assert db.fetch_one("SELECT COUNT(*) AS n FROM messages WHERE session_id='clear-inflight'")['n'] == 0
        checks.append('clearing an in-flight turn prevents reply resurrection')

        async def delivery_checks():
            class FakeTD:
                def __init__(self):
                    self.calls, self.mode, self.responses = [], 'ok', {}

                async def request(self, request, **kwargs):
                    if request['@type'] == 'getMessage':
                        return self.responses[request['message_id']]
                    self.calls.append(request)
                    if self.mode == 'reject':
                        raise TdlibError({'code': 429, 'message': 'retry after 2'})
                    if self.mode == 'unknown':
                        raise TimeoutError('response lost')
                    return {'id': -len(self.calls)}

                async def wait_message_send_result(self, local_id, **kwargs):
                    return {'@type': 'updateMessageSendSucceeded', 'old_message_id': local_id,
                            'message': {'id': 1000 - local_id, 'chat_id': 77}}

            td = FakeTD()
            gateway = SimpleNamespace(tdlib=td, stickers=None)
            delivery = Delivery(gateway)
            message = InboundMessage(77, 10, 'hello', 'text')
            delivery.accept(message)
            delivery.accept(message)
            key = 'tg:77:10'
            assert store.one('SELECT COUNT(*) AS n FROM inbox')['n'] == 1
            store.write("UPDATE inbox SET reply=?,state='retryable' WHERE key=?", ('x' * 4097, key))
            original_send = delivery.send

            async def send_and_stop(row):
                await original_send(row)
                delivery.stopping = True

            with patch.object(delivery, 'send', side_effect=send_and_stop):
                await asyncio.wait_for(delivery.run(), 10)
            parts = store.all('SELECT * FROM outbox WHERE inbox_key=? ORDER BY seq', (key,))
            assert len(parts) == 2 and parts[0]['state'] == 'confirmed' and parts[1]['state'] == 'pending'
            restarted = Delivery(gateway)
            await restarted.send(parts[1])
            await restarted.send(store.one('SELECT * FROM outbox WHERE key=?', (parts[0]['key'],)))
            assert len(td.calls) == 2
            checks.append('durable input dedup, failed plan recovery and partial send resume')

            for mid, mode in [(11, 'unknown'), (12, 'reject')]:
                restarted.accept(InboundMessage(77, mid, 'hello', 'text'))
                parent = f'tg:77:{mid}'
                store.write("UPDATE inbox SET reply='reply',state='ready' WHERE key=?", (parent,))
                restarted.plan(store.one('SELECT * FROM inbox WHERE key=?', (parent,)))
                row = store.one('SELECT * FROM outbox WHERE inbox_key=?', (parent,))
                td.mode = mode
                await restarted.send(row)
                latest = store.one('SELECT * FROM outbox WHERE key=?', (row['key'],))
                assert latest['state'] == ('uncertain' if mode == 'unknown' else 'failed')
                count = len(td.calls)
                if mode == 'unknown':
                    await Delivery(gateway).send(latest)
                    assert len(td.calls) == count
                else:
                    td.mode = 'ok'
                    await restarted.send(latest)
                    assert store.one('SELECT state FROM outbox WHERE key=?', (row['key'],))['state'] == 'confirmed'
            assert all(len(x.encode('utf-16-le')) // 2 <= 4096 for x in chunks('🙂' * 3000))
            checks.append('uncertain sends never blindly replay; explicit rejection retries; UTF16 limit')

        asyncio.run(delivery_checks())

        # Deleting one old turn must not cancel another queued Telegram input.
        deleted = db.add_message('tg:77', 'assistant', 'old', trace_id='old-trace')
        store.write("UPDATE inbox SET trace_id='old-trace' WHERE key='tg:77:10'")
        store.write("UPDATE inbox SET trace_id='other-trace' WHERE key='tg:77:12'")
        db.delete_message_turn(deleted)
        assert store.one("SELECT state FROM inbox WHERE key='tg:77:10'")['state'] == 'cancelled'
        assert store.one("SELECT state FROM inbox WHERE key='tg:77:12'")['state'] != 'cancelled'
        checks.append('turn deletion cancels only its delivery and invalidates summary')

        session = 'summary'
        ids = [db.add_message(session, 'user' if i % 2 == 0 else 'assistant', 'history ' * 160, trace_id=f's-{i}') for i in range(24)]
        image_store.add_image({'image_id': 'image-verify', 'session_id': session, 'file_path': str(base / 'image.png'),
                               'mime_type': 'image/png', 'sha256': 'fixture', 'caption': 'fixture caption'})
        image_store.bind_image(session, 'image-verify', ids[0], 1)
        image_store.save_description(session, 'image-verify', 'manual correction')
        image_store.save_description(session, 'image-verify', 'stale worker result', only_pending=True)
        assert image_store.get_image(session, 'image-verify')['description'] == 'manual correction'
        generation = store.generation(session)
        calls = []

        def summarize(**kwargs):
            payload = json.loads(kwargs['messages'][-1]['content'])
            calls.append(payload)
            assert kwargs['purpose'] == 'context_summary'
            return json.dumps({'facts': [{'text': 'fixture fact', 'source_ids': [payload['messages'][0]['id']]}], 'open_items': [], 'image_ids': ['invented']})

        with patch.object(config, 'HISTORY_TOKEN_LIMIT', 1000), patch.object(llm_gateway, 'request_model_response', side_effect=summarize):
            for _ in range(10):
                if not maintenance.compact(session, generation):
                    break
            else:
                raise AssertionError('Summary failed to catch up')
        summary = maintenance.context_summary(session)
        assert json.loads(summary['content'])['image_ids'] == ['image-verify'] and len(calls) > 1
        assert db.fetch_one('SELECT COUNT(*) AS n FROM messages WHERE session_id=?', (session,))['n'] == 24
        assert store.one("SELECT 1 FROM jobs WHERE kind='context_summary'")
        with patch('app.services.chat.image_memory.archive_older_images') as archive, patch.object(chat_service, 'generate_chat_reply', side_effect=generate), patch('app.services.chat.image_memory.decorate_context', return_value=([], [], '')):
            chat_service.run_chat_turn(session, 'new input', trace_id='summary-new')
            archive.assert_not_called()
        db.clear_session_messages(session)
        assert maintenance.context_summary(session) is None
        assert store.one('SELECT state FROM jobs WHERE key=?', ('image_archive:' + session,))['state'] == 'cancelled'
        checks.append('async archival, authoritative image IDs, incremental summary and clear invalidation')

        class Response:
            ok, status_code = True, 200
            def __init__(self, cost): self.cost = cost
            def json(self):
                usage = {'prompt_tokens': 100, 'completion_tokens': 20, 'prompt_tokens_details': {'cached_tokens': 10}}
                if self.cost is not None: usage['cost'] = self.cost
                return {'id': 'fixture', 'provider': 'fake', 'usage': usage}

        with patch('requests.post', side_effect=[Response(.001), Response(None)]), patch.dict(os.environ, {'LLM_DAILY_BUDGET_USD': ''}):
            for _ in range(2): usage.measured_post('https://invalid.test', headers={}, json={'model': 'fake'}, timeout=1, proxies={}, purpose='image_archive')
        totals = usage.usage_summary()[0]
        assert totals['reported_cost_usd'] == .001 and totals['cost_unknown_requests'] == 1
        checks.append('reported billing preserved; unknown cost remains unknown')

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routers import operations as router
        from app import security
        app = FastAPI()
        app.include_router(router.router)
        with patch.object(security, 'ADMIN_TOKEN', 'offline-token'), TestClient(app) as client:
            assert client.get('/ops/status').status_code == 403
            for endpoint in ['/ops/status', '/ops/usage', '/ops/delivery', '/ops/jobs', '/ops/context?session_id=verify']:
                assert client.get(endpoint, headers={'x-admin-token': 'offline-token'}).status_code == 200
        checks.append('diagnostic endpoints require admin token')

        spec = importlib.util.spec_from_file_location('backup_test', ROOT / 'scripts/backup_backend.py')
        backup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backup)
        fixture = base / 'fixture'
        (fixture / 'data/media').mkdir(parents=True)
        (fixture / 'app').mkdir()
        (fixture / 'app/__init__.py').write_text('# fixture', encoding='utf-8')
        (fixture / 'data/media/test.bin').write_bytes(b'fixture')
        with closing(sqlite3.connect(fixture / 'data/bot.db')) as conn:
            conn.execute('CREATE TABLE fixture(value TEXT)')
            conn.execute("INSERT INTO fixture VALUES ('ok')")
            conn.commit()
        backup.ROOT = fixture
        status = backup.create_backup(base / 'snapshots')
        snapshot = Path(status['directory'])
        backup.verify(snapshot)
        backup.restore(snapshot, base / 'restored')
        with closing(sqlite3.connect(base / 'restored/data/bot.db')) as conn:
            assert conn.execute('SELECT value FROM fixture').fetchone()[0] == 'ok'
        assert (base / 'restored/data/media/test.bin').read_bytes() == b'fixture'
        checks.append('SQLite backup, checksums and empty-directory restoration')
    return checks


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    with patch('requests.post', side_effect=AssertionError('Live network forbidden in verification')):
        checks = run()
    print(json.dumps({'ok': True, 'checks': checks, 'paid_requests': 0, 'real_telegram_sends': 0}, ensure_ascii=False, indent=2))
