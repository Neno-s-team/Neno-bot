"""P3a 后台调度器：按 SQL 到期项驱动 L0 与待想出队，不调模型。"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from app import config
from app.services.mind import clock, perception, store
from app.storage.db import fetch_all
from app.utils.logging_utils import log_event

_session_locks: dict[str, threading.Lock] = {}


def _lock(session_id: str) -> threading.Lock:
    lock = _session_locks.get(session_id)
    if lock is None:
        lock = threading.Lock()
        _session_locks[session_id] = lock
    return lock


def process_glance(session_id: str, trace_id: str) -> None:
    with _lock(session_id):
        log_event("mind", "process_glance", trace_id=trace_id, session_id=session_id)
        before = store.get_perception_by_trace(trace_id)
        if before is None or before["status"] != "pending":
            return
        after = perception.glance(session_id, trace_id)
        if after and after["status"] == "opened" and after["seen"] == "opened":
            trace_ids = _session_opened_traces(session_id)
            if trace_ids:
                from app.services.mind import orchestration

                orchestration.handle_opened(session_id, trace_ids)


def _session_opened_traces(session_id: str) -> list[str]:
    """刚点开、还没进任何周期的感知记录（同一批未读一起看）。"""
    rows = fetch_all(
        """SELECT trace_id FROM mind_perception
           WHERE session_id = ? AND status = 'opened' AND seen = 'opened' AND retracted = 0
           ORDER BY received_at ASC""",
        (session_id,),
    )
    trace_ids = [str(row["trace_id"]) for row in rows]
    for trace_id in trace_ids:
        record = store.get_perception_by_trace(trace_id)
        if record is not None:
            store.update_perception(record["id"], status="decided")
    return trace_ids


def process_deferred(session_id: str, deferred_id: int) -> None:
    with _lock(session_id):
        record = store.get_deferred(deferred_id)
        if record is None or record["status"] in ("done", "cancelled"):
            return
        ts = clock.now()
        if not store.lease_deferred(deferred_id, ts + int(config.MIND_LEASE_SEC)):
            return
        from app.services.mind import deferred as deferred_mod
        from app.services.mind import orchestration

        trace_ids = list(json.loads(record["trace_ids_json"] or "[]"))
        cycle_id = orchestration.run_async_cycle(
            session_id,
            trace_ids,
            trigger_ref=f"deferred:{deferred_id}",
        )
        store.update_deferred(deferred_id, status="done", cycle_id=cycle_id)


def process_due_batch(limit: int = 50) -> dict[str, int]:
    """所有到期项各处理一遍（先串行实现，每 session 内串行）。"""
    ts = clock.now()
    done = {"glances": 0, "deferred": 0}
    for record in store.due_perceptions(ts, limit=limit):
        try:
            process_glance(record["session_id"], record["trace_id"])
            done["glances"] += 1
        except Exception as exc:
            log_event("mind", "scheduler_glance_failed", trace_id=record["trace_id"],
                      session_id=record["session_id"], error_type=type(exc).__name__)
    for record in store.due_deferred(ts, limit=limit):
        try:
            process_deferred(record["session_id"], record["id"])
            done["deferred"] += 1
        except Exception as exc:
            log_event("mind", "scheduler_deferred_failed",
                      session_id=record["session_id"], error_type=type(exc).__name__)
    return done


async def _tick_loop() -> None:
    while True:
        try:
            process_due_batch()
        except Exception as exc:
            log_event("mind", "scheduler_tick_failed", error_type=type(exc).__name__)
        await asyncio.sleep(max(0.5, float(config.MIND_SCHEDULER_TICK_SEC)))


_task: asyncio.Task | None = None


def start() -> None:
    """恢复 running 但租约过期的 deferred（从断点续跑），并启动 tick 循环。"""
    global _task
    ts = clock.now()
    for record in store.due_deferred(ts, limit=100):
        if record["status"] == "running" and (record["lease_until"] or 0) < ts:
            store.update_deferred(record["id"], status="pending", lease_until=None)
            log_event("mind", "scheduler_deferred_recovered",
                      session_id=record["session_id"], candidate_id=record["id"])
    if _task is None:
        _task = asyncio.create_task(_tick_loop(), name="mind-scheduler")


def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
