"""One explicitly requested provider call in a temporary world; no chat integration."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.world.cognition import WorldCognition
from app.services.world.cognition_config import WorldLLMConfig
from app.services.world.cognition_models import CognitionSettings, CognitionUpdate
from app.services.world.engine import WorldEngine
from app.services.world.models import ActorControl, Advance, Control, RandomSettings, WorldCreate
from app.storage.world_store import WorldStore, encode


def run():
    load_dotenv(ROOT / '.env')
    config = replace(WorldLLMConfig.from_env(), enabled=True, max_calls_per_day=1)
    if not config.public_status()['configured']:
        raise RuntimeError('Configure WORLD_LLM_MODEL and provider URL/key before the smoke run')
    with TemporaryDirectory(prefix='world-llm-live-') as directory:
        engine = WorldEngine(WorldStore(Path(directory) / 'world.db'))
        engine.create(WorldCreate(world_id='smoke', running=True, random=RandomSettings(probability=0)))
        for aid in ['neighbor', 'shopkeeper']:
            engine.actor_control('smoke', aid, ActorControl(request_id='pause-' + aid, active=False))
        cognition = WorldCognition(engine, config)
        cognition.configure('smoke', CognitionUpdate(request_id='enable', settings=CognitionSettings(
            enabled=True, director_enabled=False, actor_profiles={'neno': '测试场景：此刻想读一会儿桌上的书，读完放回桌子。安排可执行的行动，而不是对话。'})))
        result = cognition.step()
        # Drain only the accepted action plan; the one-call cap prevents additional requests.
        for index in range(6):
            active = [r for r in engine.records('smoke', 'actions')['items'] if r['status'] == 'running']
            if not active:
                break
            clock = engine.snapshot('smoke')['world']['sim_time']
            duration = max(0, active[0]['due_at'] - clock) + 1
            engine.control('smoke', Control(request_id=f'pause-{index}', operation='pause'))
            engine.advance('smoke', Advance(request_id=f'advance-{index}', seconds=duration))
            engine.control('smoke', Control(request_id=f'resume-{index}', operation='resume'))
            cognition.step()
        decisions = cognition.decisions('smoke', include_context=True)['items']
        audit = {
            'checked_at': datetime.now(timezone.utc).isoformat(), 'model': config.model,
            'result': result, 'calls': len(decisions), 'context_characters': len(encode(decisions[0]['context'])) if decisions else 0,
            'decision': json.loads(decisions[0]['response']) if decisions and decisions[0]['response'] and decisions[0]['response'].strip().startswith('{') else None,
            'actions': [{'operation': x['data']['operation'], 'target_id': x['data']['target_id'], 'status': x['status'], 'failure': (x['result'] or {}).get('reason')}
                        for x in reversed(engine.records('smoke', 'actions')['items'])],
            'threads': [{'title': x['title'], 'status': x['status']} for x in engine.records('smoke', 'threads')['items']],
            'final_book_state': engine.get_entity('smoke', 'book_1')['state'],
            'live_database_touched': False, 'telegram_messages_sent': 0,
        }
        output = ROOT / 'data' / 'world-cognition-smoke' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '.json')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k:v for k,v in audit.items() if k != 'decision'}, ensure_ascii=False, indent=2))
        print('Audit:', output)
        if result.get('status') != 'applied' or any(x['status'] == 'failed' for x in audit['actions']):
            raise RuntimeError('Live decision was not fully applied; inspect the bounded audit file')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Allow exactly one provider request')
    if not parser.parse_args().live:
        parser.error('Pass --live to explicitly make one model API request')
    run()
