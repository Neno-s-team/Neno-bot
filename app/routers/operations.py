from __future__ import annotations

import json
import time
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from app.security import require_admin_token
from app.storage.operations import operations

router = APIRouter(prefix='/ops', tags=['operations'], dependencies=[Depends(require_admin_token)])


@router.get('/status')
def status():
    store = operations()
    counts = {table: store.all(f'SELECT state,COUNT(*) AS count FROM {table} GROUP BY state') for table in ['inbox', 'outbox', 'jobs']}
    backup_path = Path(__file__).resolve().parents[2] / 'data/backup_status.json'
    backup = json.loads(backup_path.read_text(encoding='utf-8')) if backup_path.exists() else {'status': 'not_run'}
    return {**counts, 'backup': backup}


@router.get('/usage')
def usage(days: int = Query(1, ge=1, le=90)):
    from app.services.usage import usage_summary
    return {'window_days': days, 'currency': 'USD', 'items': usage_summary(days),
            'cost_basis': 'Provider-reported cost only; NULL means unknown, not free. Earlier unrecorded calls are not reconstructed.'}


@router.get('/delivery')
def delivery(limit: int = Query(30, ge=1, le=100)):
    store = operations()
    return {'inbox': store.all('SELECT key,chat_id,message_id,state,attempts,error,trace_id,next_at,updated_at FROM inbox ORDER BY updated_at DESC LIMIT ?', (limit,)),
            'outbox': store.all('SELECT key,inbox_key,seq,state,local_id,final_id,attempts,error,next_at,updated_at FROM outbox ORDER BY updated_at DESC LIMIT ?', (limit,))}


class Retry(BaseModel):
    inbox_key: str = Field(max_length=180)


@router.post('/delivery/retry')
def retry(req: Retry):
    store = operations()
    row = store.one('SELECT * FROM inbox WHERE key=?', (req.inbox_key,))
    if not row:
        raise HTTPException(404, 'Unknown inbox key')
    if row['state'] in {'cancelled', 'done'}:
        raise HTTPException(409, 'Cancelled or completed input cannot be replayed')
    if store.one("SELECT 1 FROM outbox WHERE inbox_key=? AND state IN ('submitting','accepted','uncertain')", (req.inbox_key,)):
        raise HTTPException(409, 'Resolve uncertain sends first; blind resend is disabled')
    with store.connect() as c:
        c.execute("UPDATE outbox SET state='failed',attempts=0,next_at=0 WHERE inbox_key=? AND state IN ('failed','needs_attention')", (req.inbox_key,))
        c.execute("UPDATE inbox SET state=?,attempts=0,next_at=0,error=NULL WHERE key=?", ('ready' if row['reply'] else 'retryable', req.inbox_key))
    return {'queued': True, 'reuse_saved_reply': bool(row['reply'])}


class Resolve(BaseModel):
    outbox_key: str = Field(max_length=220)
    message_id: int = Field(gt=0)
    confirm_media: bool = False


@router.post('/delivery/resolve')
def resolve(req: Resolve):
    changed = operations().write("UPDATE outbox SET final_id=?,baseline=?,next_at=0,state='uncertain' WHERE key=? AND state IN ('uncertain','accepted','needs_attention')", (req.message_id, json.dumps({'manual_media_confirmation': req.confirm_media}), req.outbox_key))
    if not changed:
        raise HTTPException(409, 'Outbox item is not awaiting reconciliation')
    operations().write("UPDATE inbox SET state='sending',next_at=0 WHERE key=(SELECT inbox_key FROM outbox WHERE key=?) AND state='needs_attention'", (req.outbox_key,))
    return {'queued': True, 'verification': 'Gateway must fetch and verify the outgoing message before confirming delivery'}


@router.get('/jobs')
def jobs():
    return {'items': operations().all('SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 100')}


class JobRetry(BaseModel):
    key: str = Field(min_length=1, max_length=180)


@router.post('/jobs/retry')
def retry_job(req: JobRetry):
    changed = operations().write("UPDATE jobs SET state='pending',attempts=0,next_at=0,error=NULL,updated_at=? WHERE key=? AND state IN ('needs_attention','done','pending')", (time.time(), req.key))
    if not changed:
        raise HTTPException(409, 'Job missing, cancelled or still running')
    return {'queued': True}


@router.get('/context')
def context(session_id: str = Query(max_length=128)):
    from app.services.chat.maintenance import context_summary
    return {'summary': context_summary(session_id)}


class ImageEdit(BaseModel):
    session_id: str = Field(max_length=128)
    image_id: str = Field(max_length=64)
    description: str | None = Field(default=None, min_length=1, max_length=2400)


@router.post('/images/archive')
def image_edit(req: ImageEdit):
    from app.storage import db, image_store
    if not image_store.get_image(req.session_id, req.image_id):
        raise HTTPException(404, 'Image is not in this session')
    if req.description is not None:
        image_store.save_description(req.session_id, req.image_id, req.description)
        with operations().invalidate(req.session_id, cancel_delivery=False):
            pass
        operations().enqueue('context_summary', req.session_id)
        return {'updated': True, 'source': 'manual'}
    db.execute_write("UPDATE image_assets SET description='',archive_state='pending',retry_after=0 WHERE session_id=? AND image_id=?", (req.session_id, req.image_id))
    store = operations()
    store.write("UPDATE jobs SET state='pending',attempts=0,next_at=0 WHERE key=?", ('image_archive:' + req.session_id,))
    store.enqueue('image_archive', req.session_id)
    return {'queued': True}
