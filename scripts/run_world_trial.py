"""Run an isolated, deadline-bounded world trial with no chat integration."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def snapshot(engine, cognition, manifest):
    wid = manifest['world_id']
    view = engine.snapshot(wid)
    with engine.store.transaction() as db:
        counts = {}
        for key, table in [('calls', 'world_llm_calls'), ('actions', 'world_actions'), ('objectives', 'world_threads')]:
            counts[key] = dict(db.execute(
                f'SELECT status, COUNT(*) FROM {table} WHERE world_id=? GROUP BY status', (wid,)).fetchall())
        recent = [dict(row) for row in db.execute(
            'SELECT id,kind,subject,status,created_at,result FROM world_llm_calls '
            'WHERE world_id=? ORDER BY id DESC LIMIT 5', (wid,))]
    return {
        'recorded_at': utc_now(), 'trial_status': manifest['status'],
        'started_at': manifest['started_at'], 'deadline': manifest['deadline'],
        'world': view['world'], 'event_cursor': view['event_cursor'], 'entity_count': view['total'],
        'actors': [x for x in view['items'] if x['kind'] == 'actor'],
        'counts': counts, 'recent_decisions': recent,
        'cognition_budget': cognition.status(), 'chat_connected': False,
    }


async def run(directory):
    import uvicorn
    from fastapi import FastAPI
    from app.routers.world import router
    from app.services.world.cognition_models import CognitionSettings, CognitionUpdate
    from app.services.world.models import Control, WorldCreate
    from app.services.world.runtime import start_world, stop_world

    state_path = directory / 'trial.json'
    manifest = json.loads(state_path.read_text(encoding='utf-8'))
    if manifest['status'] == 'completed':
        return
    if manifest.get('started_at') is None:
        now = time.time()
        manifest.update(started_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                        deadline=datetime.fromtimestamp(now + manifest['duration_seconds'], timezone.utc).isoformat())
        save(state_path, manifest)
    deadline = datetime.fromisoformat(manifest['deadline']).timestamp()
    os.environ.update(WORLD_LLM_ENABLED='true', WORLD_LLM_MAX_CALLS_PER_DAY='48',
                      WORLD_LLM_MIN_CALL_INTERVAL_SECONDS='1800')
    if not os.getenv('ADMIN_TOKEN', '').strip():
        raise RuntimeError('ADMIN_TOKEN is required for the private world API')
    if os.getenv('WORLD_LLM_MODEL') != 'openai/gpt-6-luna':
        raise RuntimeError('Trial model changed; review configuration before resuming')
    server = None
    monitor = None
    deadline_guard = None
    finalized = False
    finalize_lock = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal monitor, deadline_guard, finalized, finalize_lock
        await start_world(app, directory / 'world.db', enabled=True)
        engine, cognition = app.state.world_engine, app.state.world_cognition
        if engine is None or cognition is None or not cognition.config.public_status()['configured']:
            await stop_world(app)
            raise RuntimeError('World or model configuration failed')
        wid = manifest['world_id']
        underlying_call = cognition.model_call

        def bounded_call(messages):
            if time.time() >= deadline:
                raise RuntimeError('Trial deadline reached')
            with engine.store.transaction() as db:
                calls = db.execute('SELECT COUNT(*) FROM world_llm_calls').fetchone()[0]
            # The current request has already reserved its ledger row.
            if calls > manifest['max_total_calls']:
                raise RuntimeError('Trial lifetime call budget exhausted')
            return underlying_call(messages)

        cognition.model_call = bounded_call
        with engine.store.transaction() as db:
            exists = db.execute('SELECT 1 FROM world_worlds WHERE world_id=?', (wid,)).fetchone()
        if not exists:
            engine.create(WorldCreate(world_id=wid, name='Neno 48-hour observation trial',
                                     seed=manifest['seed'], running=False, rate=1,
                                     start_at=datetime.fromisoformat(manifest['started_at'])))
            save(directory / 'baseline.json', snapshot(engine, cognition, manifest))

        finalize_lock = asyncio.Lock()

        def finalize_completed():
            nonlocal finalized
            if finalized:
                return
            current = engine.snapshot(wid)['world']['settings'].get('cognition', {})
            settings = CognitionSettings.model_validate(current).model_copy(update={'enabled': False})
            cognition.configure(wid, CognitionUpdate(request_id='trial-deadline-disable', settings=settings))
            engine.control(wid, Control(request_id='trial-deadline-pause', operation='pause'))
            manifest.update(status='completed', finished_at=utc_now())
            result = snapshot(engine, cognition, manifest)
            save(directory / 'final.json', result)
            save(directory / 'latest.json', result)
            finalized = True
            logging.info('Trial completed; world paused and model decisions disabled')

        async def finalize_at_deadline():
            async with finalize_lock:
                if not finalized:
                    await asyncio.to_thread(finalize_completed)
            app.state.world_stop.set()
            server.should_exit = True

        if time.time() >= deadline:
            await finalize_at_deadline()
            await stop_world(app)
            yield
            return

        cognition.configure(wid, CognitionUpdate(request_id='trial-enable-cognition',
                                                settings=CognitionSettings(enabled=True)))
        engine.control(wid, Control(request_id='trial-start-clock', operation='resume'))
        manifest.update(status='running', pid=os.getpid(), last_started_at=utc_now())
        save(state_path, manifest)

        async def observe():
            next_snapshot = 0.0
            try:
                while time.time() < deadline:
                    if time.time() >= next_snapshot:
                        report = await asyncio.to_thread(snapshot, engine, cognition, manifest)
                        if sum(report['counts']['calls'].values()) >= manifest['max_total_calls']:
                            cognition.config = replace(cognition.config, enabled=False)
                        save(directory / 'latest.json', report)
                        with (directory / 'checkpoints.jsonl').open('a', encoding='utf-8') as file:
                            file.write(json.dumps(report, ensure_ascii=False) + '\n')
                        next_snapshot = time.time() + 300
                    await asyncio.sleep(min(1, max(0, deadline - time.time())))
            except Exception:
                logging.exception('Trial observer failed; stopping instead of running unobserved')
            finally:
                server.should_exit = True

        monitor = asyncio.create_task(observe())
        async def deadline_watchdog():
            await asyncio.sleep(max(0, deadline - time.time()))
            await finalize_at_deadline()

        deadline_guard = asyncio.create_task(deadline_watchdog(), name='world-trial-deadline-watchdog')
        try:
            yield
        finally:
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass
            if deadline_guard is not None:
                deadline_guard.cancel()
                try:
                    await deadline_guard
                except asyncio.CancelledError:
                    pass
            await stop_world(app)
            if time.time() >= deadline:
                await finalize_at_deadline()
            else:
                manifest.update(status='interrupted', last_stopped_at=utc_now())
                save(directory / 'latest.json', snapshot(engine, cognition, manifest))
            save(state_path, manifest)

    app = FastAPI(title='Neno bounded world trial', lifespan=lifespan)
    app.include_router(router)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=manifest['port'],
                                         log_config=None, access_log=False))
    await server.serve()
    if manifest['status'] != 'completed':
        raise RuntimeError('Trial interrupted before its deadline')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trial-dir', type=Path, required=True)
    args = parser.parse_args()
    directory = args.trial_dir.resolve()
    if not directory.is_relative_to((ROOT / 'data/world-trials').resolve()):
        parser.error('Trial directory must be inside data/world-trials')
    load_dotenv(ROOT / '.env')
    logging.basicConfig(filename=directory / 'runner.log', level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s', encoding='utf-8')
    try:
        asyncio.run(run(directory))
    except Exception:
        logging.exception('Trial runner stopped')
        raise
