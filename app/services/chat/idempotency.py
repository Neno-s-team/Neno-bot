from __future__ import annotations

import hashlib
import json
import time
from fastapi import HTTPException
from app.storage import db
from app.storage.operations import operations
from app.session_identity import SessionIdentityError, canonical_chat_session_id


def recover_reply(session_id, trace_id):
    # Aggregation may choose the first input's trace for the whole batch.
    user = db.fetch_one('''SELECT trace_id FROM messages WHERE session_id=? AND role='user'
      AND (trace_id=? OR json_extract(metadata_json,'$.aggregation.source_trace_id')=?) ORDER BY id LIMIT 1''',
      (session_id, trace_id, trace_id))
    effective = user['trace_id'] if user else trace_id
    reply = db.fetch_one("SELECT id,content,message_type FROM messages WHERE session_id=? AND trace_id=? AND role='assistant' ORDER BY id DESC LIMIT 1", (session_id, effective))
    if not reply:
        return None
    ids = [r['id'] for r in db.fetch_all("SELECT id FROM messages WHERE session_id=? AND trace_id=? AND role='user' ORDER BY id", (session_id, effective))]
    return {'reply': reply['content'], 'trace_id': effective, 'session_id': session_id,
            'assistant_message_id': reply['id'], 'user_message_ids': ids,
            'user_message_id': ids[0] if ids else None, 'message_type': reply['message_type']}


def execute(req, handler):
    try:
        session_id = canonical_chat_session_id(req.session_id)
    except SessionIdentityError as exc:
        raise HTTPException(400, str(exc)) from exc
    if session_id != req.session_id:
        req = req.model_copy(update={"session_id": session_id})
    if not req.request_id:
        return handler(req)
    store = operations()
    encoded = json.dumps(req.model_dump(), ensure_ascii=False, sort_keys=True)
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    trace = 'req_' + hashlib.sha256(req.request_id.encode()).hexdigest()[:28]
    now = time.time()
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT * FROM requests WHERE request_id=?', (req.request_id,)).fetchone()
        if old:
            if old['fingerprint'] != fingerprint:
                raise HTTPException(409, 'request_id reused with different content')
            if old['status'] == 'cancelled':
                raise HTTPException(410, 'request was cancelled by session deletion')
            if old['status'] == 'completed':
                return json.loads(old['result'])
            recovered = recover_reply(session_id, trace)
            if recovered:
                c.execute("UPDATE requests SET status='completed',result=?,updated_at=? WHERE request_id=?", (json.dumps(recovered, ensure_ascii=False), now, req.request_id))
                return recovered
            if old['status'] == 'processing' and old['lease_until'] > now:
                raise HTTPException(409, 'request is still processing', headers={'Retry-After': '15'})
            c.execute("UPDATE requests SET status='processing',lease_until=?,updated_at=? WHERE request_id=?", (now + 900, now, req.request_id))
        else:
            c.execute('INSERT INTO requests(request_id,session_id,fingerprint,trace_id,status,lease_until,updated_at) VALUES (?,?,?,?,?,?,?)',
                      (req.request_id, session_id, fingerprint, trace, 'processing', now + 900, now))
    try:
        result = handler(req, trace_id=trace)
        value = result.model_dump() if hasattr(result, 'model_dump') else result
        changed = store.write("UPDATE requests SET status='completed',result=?,trace_id=?,lease_until=0,updated_at=? WHERE request_id=? AND status<>'cancelled'", (json.dumps(value, ensure_ascii=False), value.get('trace_id') or trace, time.time(), req.request_id))
        if not changed:
            raise HTTPException(410, 'request cancelled while generating')
        return value
    except BaseException as exc:
        store.write("UPDATE requests SET status='retryable',error=?,lease_until=0,updated_at=? WHERE request_id=? AND status='processing'", (type(exc).__name__, time.time(), req.request_id))
        raise
