from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


class WorldError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class WorldStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        # The world scheduler, cognition worker and snapshotter share one
        # SQLite file from different worker threads.  Serialize transactions
        # inside this process so rollback-journal readers cannot race writers.
        self._transaction_lock = threading.RLock()
        self._journal_configured = False

    @contextmanager
    def transaction(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._transaction_lock:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA busy_timeout=30000")
                if not self._journal_configured:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    self._journal_configured = True
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction(write=True) as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS world_catalog (
              version TEXT NOT NULL, type_id TEXT NOT NULL, label TEXT NOT NULL,
              search_text TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(version,type_id));
            CREATE TABLE IF NOT EXISTS world_worlds (
              world_id TEXT PRIMARY KEY, name TEXT NOT NULL, seed TEXT NOT NULL,
              catalog_version TEXT NOT NULL, sim_time REAL NOT NULL, status TEXT NOT NULL,
              rate REAL NOT NULL, last_wall REAL NOT NULL, version INTEGER NOT NULL DEFAULT 0,
              settings TEXT NOT NULL, error TEXT);
            CREATE TABLE IF NOT EXISTS world_entities (
              world_id TEXT NOT NULL REFERENCES world_worlds(world_id), entity_id TEXT NOT NULL,
              kind TEXT NOT NULL, type_id TEXT NOT NULL, label TEXT NOT NULL, parent_id TEXT,
              owner_id TEXT, state TEXT NOT NULL, capabilities TEXT NOT NULL, version INTEGER NOT NULL,
              PRIMARY KEY(world_id,entity_id));
            CREATE INDEX IF NOT EXISTS world_entities_parent ON world_entities(world_id,parent_id);
            CREATE TABLE IF NOT EXISTS world_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, world_id TEXT NOT NULL REFERENCES world_worlds(world_id),
              event_key TEXT NOT NULL, sim_time REAL NOT NULL, kind TEXT NOT NULL, actor_id TEXT,
              payload TEXT NOT NULL, UNIQUE(world_id,event_key));
            CREATE INDEX IF NOT EXISTS world_events_cursor ON world_events(world_id,id);
            CREATE TABLE IF NOT EXISTS world_jobs (
              world_id TEXT NOT NULL REFERENCES world_worlds(world_id), job_id TEXT NOT NULL,
              run_at REAL NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
              PRIMARY KEY(world_id,job_id));
            CREATE INDEX IF NOT EXISTS world_jobs_due ON world_jobs(world_id,status,run_at,job_id);
            CREATE TABLE IF NOT EXISTS world_actions (
              world_id TEXT NOT NULL REFERENCES world_worlds(world_id), action_id TEXT NOT NULL,
              actor_id TEXT NOT NULL, status TEXT NOT NULL, due_at REAL NOT NULL, data TEXT NOT NULL,
              result TEXT, PRIMARY KEY(world_id,action_id));
            CREATE INDEX IF NOT EXISTS world_actions_actor ON world_actions(world_id,actor_id,status);
            CREATE TABLE IF NOT EXISTS world_threads (
              world_id TEXT NOT NULL REFERENCES world_worlds(world_id), thread_id TEXT NOT NULL,
              actor_id TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
              PRIMARY KEY(world_id,thread_id));
            CREATE TABLE IF NOT EXISTS world_knowledge (
              world_id TEXT NOT NULL REFERENCES world_worlds(world_id), actor_id TEXT NOT NULL,
              entity_id TEXT NOT NULL, observed_at REAL NOT NULL, event_id INTEGER NOT NULL,
              channel TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(world_id,actor_id,entity_id));
            CREATE TABLE IF NOT EXISTS world_commands (
              world_id TEXT NOT NULL REFERENCES world_worlds(world_id), request_id TEXT NOT NULL,
              fingerprint TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(world_id,request_id));
            CREATE TABLE IF NOT EXISTS world_minds (
              world_id TEXT NOT NULL REFERENCES world_worlds(world_id), actor_id TEXT NOT NULL,
              next_at REAL NOT NULL, last_call_at REAL NOT NULL DEFAULT 0, data TEXT NOT NULL,
              PRIMARY KEY(world_id,actor_id));
            CREATE TABLE IF NOT EXISTS world_llm_calls (
              id INTEGER PRIMARY KEY AUTOINCREMENT, world_id TEXT NOT NULL REFERENCES world_worlds(world_id),
              kind TEXT NOT NULL, subject TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
              sim_time REAL NOT NULL, finished_at REAL, model TEXT NOT NULL,
              context TEXT NOT NULL, response TEXT, result TEXT);
            CREATE INDEX IF NOT EXISTS world_llm_budget ON world_llm_calls(created_at,status);
            CREATE INDEX IF NOT EXISTS world_llm_history ON world_llm_calls(world_id,id);
            """)

    def import_catalog(self, document: dict) -> dict:
        version = document["version"]
        with self.transaction(write=True) as c:
            existing = c.execute("SELECT count(*) FROM world_catalog WHERE version=?", (version,)).fetchone()[0]
            if existing:
                if existing != len(document["types"]):
                    raise WorldError("Catalog version already exists with a different record count", 409)
                return {"version": version, "types": existing, "changed": False}
            c.executemany("INSERT INTO world_catalog VALUES (?,?,?,?,?)", [
                (version, t["type_id"], t["label"], " ".join([t["type_id"], t["label"], *t.get("aliases", [])]).casefold(), encode(t))
                for t in document["types"]])
        return {"version": version, "types": len(document["types"]), "changed": True}


def world(c: sqlite3.Connection, world_id: str) -> dict:
    row = c.execute("SELECT * FROM world_worlds WHERE world_id=?", (world_id,)).fetchone()
    if not row:
        raise WorldError("World not found", 404)
    value = dict(row)
    value["settings"] = json.loads(value["settings"])
    return value


def entity(c: sqlite3.Connection, world_id: str, entity_id: str) -> dict:
    row = c.execute("SELECT * FROM world_entities WHERE world_id=? AND entity_id=?", (world_id, entity_id)).fetchone()
    if not row:
        raise WorldError(f"Entity not found: {entity_id}", 404)
    return decode_entity(row)


def decode_entity(row) -> dict:
    value = dict(row)
    value["state"] = json.loads(value["state"])
    value["capabilities"] = json.loads(value["capabilities"])
    return value


def save_entity(c: sqlite3.Connection, value: dict) -> None:
    value["version"] = value.get("version", 0) + 1
    c.execute("""INSERT INTO world_entities VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(world_id,entity_id) DO UPDATE SET label=excluded.label,parent_id=excluded.parent_id,
        owner_id=excluded.owner_id,state=excluded.state,capabilities=excluded.capabilities,version=excluded.version""",
        (value["world_id"], value["entity_id"], value["kind"], value["type_id"], value["label"],
         value.get("parent_id"), value.get("owner_id"), encode(value["state"]), encode(value["capabilities"]), value["version"]))


def emit(c: sqlite3.Connection, w: dict, key: str, kind: str, payload: dict, actor_id: str | None = None) -> int:
    result = c.execute("INSERT INTO world_events(world_id,event_key,sim_time,kind,actor_id,payload) VALUES (?,?,?,?,?,?)",
                       (w["world_id"], key, w["sim_time"], kind, actor_id, encode(payload)))
    return int(result.lastrowid)


def job(c: sqlite3.Connection, world_id: str, key: str, when: float, kind: str, payload: dict) -> None:
    c.execute("INSERT INTO world_jobs VALUES (?,?,?,?,?,?) ON CONFLICT(world_id,job_id) DO NOTHING",
              (world_id, key, when, kind, "pending", encode(payload)))
