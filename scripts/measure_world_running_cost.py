"""Measure one world decision and one synthetic image archive, without live state."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import runpy
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(image_only=False):
    load_dotenv(ROOT / '.env')
    from app import config
    from app.services.chat import image_memory
    from app.services.world.cognition_config import WorldLLMConfig

    world_config = WorldLLMConfig.from_env()
    expected = 'openai/gpt-6-luna'
    if world_config.model != expected or config.IMAGE_ARCHIVE_MODEL_NAME != expected:
        raise RuntimeError('Both workload models must be openai/gpt-6-luna')
    records, outcomes = [], {}
    original_post = requests.post
    current_workload = 'world'
    attempts = 0
    max_requests = 1 if image_only else 2

    def measured_post(url, *args, **kwargs):
        nonlocal attempts
        payload = kwargs.get('json') or {}
        if payload.get('model') != expected or url not in {config.OPENROUTER_URL, world_config.url}:
            raise RuntimeError('Probe blocked an unexpected provider request')
        if attempts >= max_requests:
            raise RuntimeError('Probe paid-request limit reached')
        attempts += 1
        row = {'workload': current_workload, 'requested_model': expected}
        records.append(row)
        response = original_post(url, *args, **kwargs)
        row['http_status'] = response.status_code
        try:
            data = response.json()
        except ValueError:
            row['error'] = 'Non-JSON provider response'
            return response
        row.update({key: data.get(key) for key in ['id', 'model', 'provider', 'usage']})
        row['finish_reason'] = (data.get('choices') or [{}])[0].get('finish_reason')
        if not response.ok:
            row['error'] = str((data.get('error') or {}).get('message', 'Provider failure'))[:300]
        return response

    with patch('requests.post', measured_post):
        if not image_only:
            output = io.StringIO()
            try:
                with contextlib.redirect_stdout(output):
                    runpy.run_path(str(ROOT / 'scripts/smoke_world_cognition.py'))['run']()
                outcomes['world'] = {'passed': True}
            except Exception as exc:
                outcomes['world'] = {'passed': False, 'error_type': type(exc).__name__}
            for line in output.getvalue().splitlines():
                if line.startswith('Audit: '):
                    audit_path = Path(line.removeprefix('Audit: '))
                    audit = json.loads(audit_path.read_text(encoding='utf-8'))
                    outcomes['world'].update({
                        'audit': str(audit_path), 'context_characters': audit['context_characters'],
                        'actions': audit['actions'], 'book': audit['final_book_state'],
                    })

        current_workload = 'image_archive'
        with TemporaryDirectory(prefix='neno-luna-image-') as directory:
            base = Path(directory)
            image_path = base / 'media/images/cost-probe.png'
            image_path.parent.mkdir(parents=True)
            image = Image.new('RGB', (1024, 1024), 'white')
            draw = ImageDraw.Draw(image)
            font_path = Path('C:/Windows/Fonts/arial.ttf')
            font = ImageFont.truetype(str(font_path), 56) if font_path.exists() else ImageFont.load_default(size=56)
            draw.text((70, 70), 'NENO ARCHIVE 314', fill='black', font=font)
            draw.ellipse((100, 280, 450, 630), fill='red')
            draw.rectangle((590, 280, 940, 630), fill='blue')
            image.save(image_path)
            asset = {'image_id': 'cost_probe', 'caption': '', 'file_path': str(image_path)}
            saved, deferred = {}, []
            with patch.object(image_memory.db, 'DB_DIR', base), \
                 patch.object(image_memory.store, 'archive_candidates', return_value=[asset]), \
                 patch.object(image_memory.store, 'save_description', side_effect=lambda _s, key, text: saved.update({key: text})), \
                 patch.object(image_memory.store, 'defer_description', side_effect=lambda _s, key: deferred.append(key)):
                image_memory.archive_older_images('isolated-cost-probe', 10, 'luna-cost-probe')
            description = saved.get('cost_probe', '')
            outcomes['image_archive'] = {
                'passed': bool(description) and not deferred,
                'quality_warning': None if '314' in description else 'Synthetic OCR missed the exact number 314',
                'dimensions': [1024, 1024], 'description_characters': len(description),
                'synthetic_image_description': description,
                'ocr_number_matched': '314' in description, 'deferred': deferred,
            }

    metadata = requests.get('https://openrouter.ai/api/v1/models/openai/gpt-6-luna/endpoints', timeout=30)
    metadata.raise_for_status()
    endpoints = [{k: e.get(k) for k in ['provider_name', 'name', 'pricing', 'supported_parameters']}
                 for e in metadata.json().get('data', {}).get('endpoints', [])]
    report = {
        'checked_at': datetime.now(timezone.utc).isoformat(), 'model': expected,
        'world_calls_per_day': world_config.max_calls_per_day,
        'world_max_output_tokens': world_config.max_output_tokens,
        'requests': records, 'outcomes': outcomes, 'pricing_endpoints': endpoints,
        'live_database_touched': False, 'telegram_messages_sent': 0,
        'always_on_started': False,
    }
    target = ROOT / 'data/model-cost-probes' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'pricing_endpoints'}, ensure_ascii=False, indent=2))
    print('Cost audit:', target)
    if attempts != max_requests or not all(x['passed'] for x in outcomes.values()):
        raise SystemExit('A workload probe failed; inspect the cost audit before using its estimate')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Allow exactly two paid fixture requests')
    parser.add_argument('--image-only', action='store_true', help='Only make one archive request')
    args = parser.parse_args()
    if not args.live:
        parser.error('Pass --live to authorize two API requests')
    run(image_only=args.image_only)
