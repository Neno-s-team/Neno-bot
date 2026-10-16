from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
import requests
from app.storage.operations import operations


def measured_post(url, *, headers, json: dict, timeout, proxies, purpose='chat', trace_id=None, session_id=None):
    store = operations()
    limit = os.getenv('LLM_DAILY_BUDGET_USD', '').strip()
    if limit:
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        spent = store.one('SELECT COALESCE(SUM(cost_usd),0) AS amount FROM llm_usage WHERE created_at>=?', (midnight,))['amount']
        if spent >= float(limit):
            raise RuntimeError('Configured UTC-day LLM spend threshold reached')
    started = time.perf_counter()
    row = dict(id=uuid.uuid4().hex, created_at=time.time(), purpose=purpose, model=json.get('model', ''),
               trace_id=trace_id, session_id=session_id, provider=None, provider_request_id=None,
               status='network_error', input_tokens=None, output_tokens=None, cache_read_tokens=None,
               cache_write_tokens=None, reasoning_tokens=None, cost_usd=None, latency_ms=None, error=None)
    try:
        response = requests.post(url, headers=headers, json=json, timeout=timeout, proxies=proxies)
        row['status'] = 'ok' if response.ok else f'http_{response.status_code}'
        try:
            data = response.json()
            usage = data.get('usage') or {}
            details = usage.get('prompt_tokens_details') or usage.get('input_tokens_details') or {}
            row.update(provider=data.get('provider'), provider_request_id=data.get('id'),
                       input_tokens=usage.get('prompt_tokens', usage.get('input_tokens')),
                       output_tokens=usage.get('completion_tokens', usage.get('output_tokens')),
                       cache_read_tokens=details.get('cached_tokens', usage.get('cache_read_input_tokens')),
                       cache_write_tokens=details.get('cache_write_tokens', usage.get('cache_creation_input_tokens')),
                       reasoning_tokens=(usage.get('completion_tokens_details') or {}).get('reasoning_tokens'),
                       cost_usd=usage.get('cost'))
            if not response.ok:
                row['error'] = str((data.get('error') or {}).get('code') or row['status'])[:120]
        except (ValueError, AttributeError, TypeError):
            row['error'] = 'Unparseable provider response or usage'
        return response
    except requests.RequestException as exc:
        row['error'] = type(exc).__name__
        raise
    finally:
        row['latency_ms'] = int((time.perf_counter() - started) * 1000)
        try:
            columns = ','.join(row)
            store.write(f'INSERT INTO llm_usage({columns}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
        except Exception:
            logging.exception('Usage persistence failed; do not retry a completed provider call')


def usage_summary(days=1):
    since = time.time() - max(1, min(days, 90)) * 86400
    return operations().all('''SELECT purpose,model,COUNT(*) AS requests,
      SUM(status='ok') AS successful,SUM(cost_usd) AS reported_cost_usd,
      SUM(cost_usd IS NULL) AS cost_unknown_requests,SUM(input_tokens) AS input_tokens,
      SUM(output_tokens) AS output_tokens,SUM(cache_read_tokens) AS cache_read_tokens,
      SUM(cache_write_tokens) AS cache_write_tokens,SUM(reasoning_tokens) AS reasoning_tokens,
      ROUND(AVG(latency_ms)) AS avg_latency_ms FROM llm_usage WHERE created_at>=? GROUP BY purpose,model''', (since,))
