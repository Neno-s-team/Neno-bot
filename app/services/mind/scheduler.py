"""P3a 后台调度器：SQL 取到期项，线程池执行，不阻塞事件循环。

同一时刻只有一个批次在跑；上一批没跑完就跳过本次 tick。
"""

from __future__ import annotations

import asyncio
import threading

from app import config
from app.services.mind import clock, perception, store
from app.storage.db import fetch_all
from app.utils.logging_utils import log_event

_session_locks: dict[str, threading.Lock] = {}
_batch_lock = threading.Lock()
_batch_running = False


def _lock(session_id: str) -> threading.Lock:
    lock = _session_locks.get(session_id)
    if lock is None:
        lock = threading.Lock()
        _session_locks[session_id] = lock
    return lock


def process_glance(session_id: str, trace_id: str) -> None:
    """到期的 glance → L0（预览/打开）。打开后交给 handle_opened 写作业。"""
    with _lock(session_id):
        record = store.get_perception_by_trace(trace_id)
        if record is None or record["retracted"]:
            return
        if record["status"] not in ("pending", "previewed"):
            return
        after = perception.glance(session_id, trace_id)
        if after and after["status"] == "opened" and after["seen"] == "opened":
            trace_ids = _open_batch(session_id)
            if trace_ids:
                from app.services.mind import orchestration

                # handle_opened 只做感知/决策/写作业，不跑周期
                orchestration.handle_opened(session_id, trace_ids)


def _open_batch(session_id: str) -> list[str]:
    """取出该 session 全部 opened 未决策的 trace（真人一次看到全部未读）。"""
    rows = fetch_all(
        """SELECT trace_id FROM mind_perception
           WHERE session_id = ? AND status = 'opened' AND seen = 'opened' AND retracted = 0
           ORDER BY received_at ASC""",
        (session_id,),
    )
    return [str(row["trace_id"]) for row in rows]


def process_deferred(session_id: str, deferred_id: int) -> None:
    """到期的回复作业：租约领取 → run_async_cycle → 成功后 perception=decided。"""
    with _lock(session_id):
        from app.services.mind import orchestration

        try:
            orchestration.run_reply_job(deferred_id)
        except Exception as exc:
            job = store.get_deferred(deferred_id) or {}
            attempts = int(job.get("attempts") or 0)
            if attempts >= max(1, int(config.MIND_JOB_MAX_ATTEMPTS)):
                store.update_deferred(deferred_id, status="failed")
                log_event("mind", "mind_job_failed", session_id=session_id,
                          candidate_id=deferred_id, attempts=attempts,
                          error_type=type(exc).__name__)
            else:
                # 退回 pending，租约过期后由下一个 tick 重领
                store.update_deferred(deferred_id, status="pending", lease_until=None)
                log_event("mind", "mind_job_retry", session_id=session_id,
                          candidate_id=deferred_id, attempts=attempts,
                          error_type=type(exc).__name__)


def process_due_batch(limit: int = 50) -> dict[str, int]:
    """串行处理所有到期项（同 session 串行；先串行实现，写进文档）。"""
    ts = clock.now()
    done = {"glances": 0, "deferred": 0}
    recover_stale_jobs()
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


def _try_run_batch() -> None:
    """批次互斥：上一批没跑完直接跳过（不排队堆积）。"""
    global _batch_running
    with _batch_lock:
        if _batch_running:
            return
        _batch_running = True
    try:
        process_due_batch()
    finally:
        with _batch_lock:
            _batch_running = False


async def _tick_loop() -> None:
    while True:
        try:
            # to_thread：LLM/DB 都在线程池里跑，不阻塞事件循环
            await asyncio.to_thread(_try_run_batch)
        except Exception as exc:
            log_event("mind", "scheduler_tick_failed", error_type=type(exc).__name__)
        await asyncio.sleep(max(0.5, float(config.MIND_SCHEDULER_TICK_SEC)))


_task: asyncio.Task | None = None


def start() -> None:
    """启动 tick 循环；恢复逻辑由每个 tick 的 recover_stale_jobs 完成。"""
    global _task
    if _task is None:
        _task = asyncio.create_task(_tick_loop(), name="mind-scheduler")


def recover_stale_jobs() -> None:
    """running 但租约过期 → pending；attempts 超限 → failed。"""
    ts = clock.now()
    max_attempts = max(1, int(config.MIND_JOB_MAX_ATTEMPTS))
    for record in store.stale_running_deferred(ts, limit=100):
        attempts = int(record.get("attempts") or 0)
        if attempts >= max_attempts:
            store.update_deferred(record["id"], status="failed")
            log_event("mind", "mind_job_failed", session_id=record["session_id"],
                      candidate_id=record["id"], attempts=attempts, reason="lease_expired")
        elif (record["lease_until"] or 0) < ts:
            store.update_deferred(record["id"], status="pending", lease_until=None)
            log_event("mind", "scheduler_deferred_recovered",
                      session_id=record["session_id"], candidate_id=record["id"])


def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
