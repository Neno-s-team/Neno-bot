"""Durable delivery, maintenance and billing records, separate from chat/world data."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from app.session_identity import canonical_chat_session_id, telegram_chat_id

ROOT = Path(__file__).resolve().parents[2]


class Operations:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv('NENO_OPERATIONS_DB') or ROOT / 'data/operations.db')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS requests (
              request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
              trace_id TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT,
              lease_until REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox (
              key TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
              data TEXT NOT NULL, payload TEXT, reply TEXT, trace_id TEXT,
              state TEXT NOT NULL DEFAULT 'received', attempts INTEGER NOT NULL DEFAULT 0,
              next_at REAL NOT NULL DEFAULT 0, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
              key TEXT PRIMARY KEY, inbox_key TEXT NOT NULL, seq INTEGER NOT NULL,
              chat_id INTEGER NOT NULL, request TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
              local_id INTEGER, final_id INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
              baseline TEXT, submitted_at REAL, next_at REAL NOT NULL DEFAULT 0,
              error TEXT, updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS outbox_parent ON outbox(inbox_key,seq);
            CREATE TABLE IF NOT EXISTS jobs (
              key TEXT PRIMARY KEY, kind TEXT NOT NULL, session_id TEXT NOT NULL,
              generation INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
              lease_until REAL NOT NULL DEFAULT 0, error TEXT, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS summaries (
              session_id TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0,
              through_id INTEGER NOT NULL DEFAULT 0, content TEXT NOT NULL DEFAULT '',
              source_ids TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS llm_usage (
              id TEXT PRIMARY KEY, created_at REAL NOT NULL, purpose TEXT NOT NULL, model TEXT NOT NULL,
              trace_id TEXT, session_id TEXT, provider TEXT, provider_request_id TEXT,
              status TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
              cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
              cost_usd REAL, latency_ms INTEGER, error TEXT);
            CREATE INDEX IF NOT EXISTS usage_time_purpose ON llm_usage(created_at,purpose);
            ''')
            for row in c.execute("SELECT request_id,session_id FROM requests WHERE status<>'cancelled'").fetchall():
                try:
                    canonical = canonical_chat_session_id(row['session_id'])
                except ValueError:
                    canonical = None
                if canonical != row['session_id']:
                    c.execute("UPDATE requests SET status='cancelled',result=NULL,error='noncanonical_session',lease_until=0,updated_at=? WHERE request_id=?",
                              (time.time(), row['request_id']))

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA busy_timeout=15000')
        c.execute('PRAGMA journal_mode=WAL')
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def one(self, sql, args=()):
        with self.connect() as c:
            r = c.execute(sql, args).fetchone()
            return dict(r) if r else None

    def all(self, sql, args=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def write(self, sql, args=()):
        with self.connect() as c:
            return c.execute(sql, args).rowcount

    def generation(self, session_id):
        self.write('INSERT OR IGNORE INTO summaries(session_id,updated_at) VALUES (?,?)', (session_id, time.time()))
        return self.one('SELECT generation FROM summaries WHERE session_id=?', (session_id,))['generation']

    @contextmanager
    def session_guard(self, session_id, generation, trace_id=None):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current = c.execute('SELECT generation FROM summaries WHERE session_id=?', (session_id,)).fetchone()
            cancelled = c.execute("SELECT 1 FROM requests WHERE session_id=? AND trace_id=? AND status='cancelled' LIMIT 1", (session_id, trace_id)).fetchone()
            if not current or current['generation'] != generation or cancelled:
                raise RuntimeError('Session changed while generating; discarded stale turn')
            yield

    @contextmanager
    def invalidate(self, session_id, *, trace_id=None, cancel_delivery=True):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT OR IGNORE INTO summaries(session_id,updated_at) VALUES (?,?)', (session_id, time.time()))
            c.execute("UPDATE summaries SET generation=generation+1,through_id=0,content='',source_ids='[]',updated_at=? WHERE session_id=?", (time.time(), session_id))
            c.execute("UPDATE jobs SET state='cancelled' WHERE session_id=?", (session_id,))
            if not cancel_delivery:
                yield
                return
            clause = 'session_id=?' + (' AND trace_id=?' if trace_id else '')
            args = (session_id, trace_id) if trace_id else (session_id,)
            c.execute("UPDATE requests SET status='cancelled',result=NULL WHERE " + clause, args)
            chat_id = telegram_chat_id(session_id)
            if chat_id is not None:
                clause = 'chat_id=?' + (' AND trace_id=?' if trace_id else '')
                args = (chat_id, trace_id) if trace_id else (chat_id,)
                c.execute("UPDATE outbox SET state='cancelled',request='{}' WHERE inbox_key IN (SELECT key FROM inbox WHERE " + clause + ')', args)
                c.execute("UPDATE inbox SET state='cancelled',data='{}',payload=NULL,reply=NULL WHERE " + clause, args)
            yield

    def enqueue(self, kind, session_id):
        generation = self.generation(session_id)
        self.write('''INSERT INTO jobs(key,kind,session_id,generation,updated_at) VALUES (?,?,?,?,?)
          ON CONFLICT(key) DO UPDATE SET
          state=CASE WHEN jobs.generation<>excluded.generation THEN 'pending' WHEN jobs.state IN ('running','needs_attention') THEN jobs.state ELSE 'pending' END,
          attempts=CASE WHEN jobs.generation<>excluded.generation THEN 0 ELSE jobs.attempts END,
          next_at=CASE WHEN jobs.generation<>excluded.generation THEN 0 ELSE jobs.next_at END,
          generation=excluded.generation,updated_at=excluded.updated_at''',
          (kind + ':' + session_id, kind, session_id, generation, time.time()))


_instance = None


def operations():
    global _instance
    if _instance is None:
        _instance = Operations()
    return _instance
