from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.utils.logging_utils import log_event


BatchHandler = Callable[["AggregatedSubmitItem"], object]
TERMINAL_BATCH_STATES = {"batch_completed", "batch_failed"}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


class _RealTimerScheduler:
    """Production window-seal scheduler: fires the callback on a real daemon
    ``threading.Timer`` after ``delay`` seconds of wall-clock time.

    The scheduler is pluggable (see ``SessionAggregationController.__init__``)
    purely so tests can drive the aggregation window deterministically instead
    of racing a real timer against thread scheduling and ``time.sleep``. The
    returned handle only needs a ``cancel()`` method.
    """

    def call_later(self, delay: float, callback: Callable[[], None]) -> threading.Timer:
        timer = threading.Timer(delay, callback)
        timer.daemon = True
        timer.start()
        return timer


class AggregationFuture:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._result: object | None = None
        self._exception: BaseException | None = None

    def set_result(self, result: object) -> None:
        self._result = result
        self._event.set()

    def set_exception(self, exc: BaseException) -> None:
        self._exception = exc
        self._event.set()

    def wait(self) -> object:
        self._event.wait()
        if self._exception is not None:
            raise self._exception
        return self._result


@dataclass(frozen=True)
class AggregationTicket:
    session_id: str
    arrival_seq: int
    batch_id: str
    trace_id: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    received_at: str = field(default_factory=_now_iso)


@dataclass(frozen=True)
class AggregatedSourceMessage:
    ticket: AggregationTicket
    trace_id: str | None
    message: str
    input_record: dict[str, Any]


@dataclass(frozen=True)
class AggregatedSubmitItem:
    batch_id: str
    session_id: str
    trace_id: str | None
    source_messages: list[AggregatedSourceMessage]
    opened_at: str
    deadline_at: str
    sealed_at: str


@dataclass
class _SourceItem:
    ticket: AggregationTicket
    future: AggregationFuture = field(default_factory=AggregationFuture)
    included_in_batch: bool = True
    ready_message: str | None = None
    input_record: dict[str, Any] | None = None
    source_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass
class _BatchState:
    session_id: str
    batch_id: str
    batch_seq: int
    opened_at: str
    deadline_at: str
    opened_monotonic: float
    timer: Any = None
    sealed_at: str | None = None
    batch_snapshot: dict[str, Any] = field(default_factory=dict)
    sources: dict[int, _SourceItem] = field(default_factory=dict)
    handler: BatchHandler | None = None
    submitting: bool = False
    completed: bool = False


@dataclass
class _SessionAggregationState:
    open_batch_id: str | None = None
    batches: dict[str, _BatchState] = field(default_factory=dict)


class SessionAggregationController:
    def __init__(
        self,
        *,
        window_seconds: float,
        recent_limit: int = 100,
        scheduler: Any = None,
    ) -> None:
        self.window_seconds = max(0.01, float(window_seconds))
        self._scheduler = scheduler if scheduler is not None else _RealTimerScheduler()
        self._lock = threading.RLock()
        self._sessions: dict[str, _SessionAggregationState] = {}
        self._next_arrival_seq = 1
        self._next_batch_seq = 1
        self._recent_limit = max(20, recent_limit)
        self._recent_batches: list[dict[str, Any]] = []
        self._recent_sources: list[dict[str, Any]] = []

    def _session_state(self, session_id: str) -> _SessionAggregationState:
        return self._sessions.setdefault(session_id, _SessionAggregationState())

    def _copy(self, value: dict[str, Any]) -> dict[str, Any]:
        copied = dict(value)
        arrival_seqs = copied.get("source_arrival_seqs")
        if isinstance(arrival_seqs, list):
            copied["source_arrival_seqs"] = list(arrival_seqs)
        trace_ids = copied.get("source_trace_ids")
        if isinstance(trace_ids, list):
            copied["source_trace_ids"] = list(trace_ids)
        return copied

    def _append_recent_batch(self, snapshot: dict[str, Any]) -> None:
        self._recent_batches.append(self._copy(snapshot))
        if len(self._recent_batches) > self._recent_limit:
            self._recent_batches = self._recent_batches[-self._recent_limit :]

    def _append_recent_source(self, snapshot: dict[str, Any]) -> None:
        self._recent_sources.append(self._copy(snapshot))
        if len(self._recent_sources) > self._recent_limit * 4:
            self._recent_sources = self._recent_sources[-self._recent_limit * 4 :]

    def _refresh_batch_snapshot(self, batch: _BatchState) -> None:
        arrival_seqs = sorted(batch.sources)
        trace_ids = [
            item.ticket.trace_id
            for _, item in sorted(batch.sources.items())
            if item.ticket.trace_id
        ]
        ready_count = sum(1 for item in batch.sources.values() if item.ready_message is not None)
        included_count = sum(1 for item in batch.sources.values() if item.included_in_batch)
        terminal_count = sum(
            1
            for item in batch.sources.values()
            if item.source_snapshot.get("source_state") in {"source_completed", "source_failed", "source_excluded"}
        )
        batch.batch_snapshot.update(
            {
                "session_id": batch.session_id,
                "batch_id": batch.batch_id,
                "batch_state": batch.batch_snapshot.get("batch_state") or "batch_open",
                "opened_at": batch.opened_at,
                "deadline_at": batch.deadline_at,
                "sealed_at": batch.sealed_at,
                "source_arrival_seqs": arrival_seqs,
                "source_trace_ids": trace_ids,
                "source_count": len(arrival_seqs),
                "included_count": included_count,
                "ready_count": ready_count,
                "terminal_count": terminal_count,
                "updated_at": _now_iso(),
            }
        )
        for item in batch.sources.values():
            item.source_snapshot.update(
                {
                    "batch_id": batch.batch_id,
                    "batch_state": batch.batch_snapshot.get("batch_state"),
                    "opened_at": batch.opened_at,
                    "deadline_at": batch.deadline_at,
                    "sealed_at": batch.sealed_at,
                    "source_arrival_seqs": arrival_seqs,
                    "source_trace_ids": trace_ids,
                    "source_count": len(arrival_seqs),
                    "included_in_batch": item.included_in_batch,
                    "updated_at": batch.batch_snapshot.get("updated_at"),
                }
            )

    def _new_batch(self, *, session_id: str, state: _SessionAggregationState) -> _BatchState:
        now = _now_iso()
        opened_monotonic = time.monotonic()
        deadline_iso = datetime.fromtimestamp(
            time.time() + self.window_seconds,
        ).isoformat(timespec="milliseconds")
        batch_id = f"{session_id}#batch-{self._next_batch_seq}"
        self._next_batch_seq += 1
        batch = _BatchState(
            session_id=session_id,
            batch_id=batch_id,
            batch_seq=self._next_batch_seq - 1,
            opened_at=now,
            deadline_at=deadline_iso,
            opened_monotonic=opened_monotonic,
            batch_snapshot={
                "session_id": session_id,
                "batch_id": batch_id,
                "batch_state": "batch_open",
                "opened_at": now,
                "deadline_at": deadline_iso,
                "sealed_at": None,
                "source_arrival_seqs": [],
                "source_trace_ids": [],
                "source_count": 0,
                "included_count": 0,
                "ready_count": 0,
                "terminal_count": 0,
                "updated_at": now,
            },
        )
        state.batches[batch_id] = batch
        state.open_batch_id = batch_id
        batch.timer = self._scheduler.call_later(
            self.window_seconds,
            lambda: self.seal_batch(
                session_id=session_id,
                batch_id=batch_id,
                reason="window_elapsed",
            ),
        )
        return batch

    def allocate_ticket(self, *, session_id: str, trace_id: str | None = None) -> AggregationTicket:
        with self._lock:
            state = self._session_state(session_id)
            batch = None
            if state.open_batch_id is not None:
                batch = state.batches.get(state.open_batch_id)
            if batch is None or batch.sealed_at is not None:
                batch = self._new_batch(session_id=session_id, state=state)
                log_event(
                    "platform",
                    "session_aggregation_batch_opened",
                    session_id=session_id,
                    batch_id=batch.batch_id,
                    aggregate_window_seconds=self.window_seconds,
                )

            arrival_seq = self._next_arrival_seq
            self._next_arrival_seq += 1
            ticket = AggregationTicket(
                session_id=session_id,
                arrival_seq=arrival_seq,
                batch_id=batch.batch_id,
                trace_id=trace_id,
            )
            source_snapshot = {
                "session_id": session_id,
                "batch_id": batch.batch_id,
                "arrival_seq": arrival_seq,
                "trace_id": trace_id,
                "source_state": "batch_collecting",
                "message_type": None,
                "received_at": ticket.received_at,
                "ready_at": None,
                "completed_at": None,
                "failed_at": None,
                "error_type": None,
                "error_message": None,
                "included_in_batch": True,
                "updated_at": ticket.received_at,
            }
            batch.sources[arrival_seq] = _SourceItem(
                ticket=ticket,
                source_snapshot=source_snapshot,
            )
            batch.batch_snapshot["batch_state"] = "batch_collecting"
            self._refresh_batch_snapshot(batch)

        log_event(
            "platform",
            "session_aggregation_ingress_allocated",
            trace_id=trace_id,
            session_id=session_id,
            batch_id=ticket.batch_id,
            arrival_seq=arrival_seq,
            batch_state="batch_collecting",
        )
        return ticket

    def mark_ready(
        self,
        *,
        ticket: AggregationTicket,
        message: str,
        input_record: dict[str, Any],
        handler: BatchHandler,
    ) -> AggregationFuture:
        future: AggregationFuture
        with self._lock:
            batch, source = self._require_source(ticket)
            source.ready_message = message
            source.input_record = input_record
            if batch.handler is None:
                batch.handler = handler
            source.source_snapshot.update(
                {
                    "source_state": "source_ready",
                    "message_type": str(input_record.get("message_type") or "text"),
                    "ready_at": _now_iso(),
                    "updated_at": _now_iso(),
                }
            )
            if batch.sealed_at is not None:
                batch.batch_snapshot["batch_state"] = "batch_waiting_ready"
            self._refresh_batch_snapshot(batch)
            future = source.future
            self._maybe_start_submit_locked(batch)

        log_event(
            "platform",
            "session_aggregation_source_ready",
            trace_id=ticket.trace_id,
            session_id=ticket.session_id,
            batch_id=ticket.batch_id,
            arrival_seq=ticket.arrival_seq,
        )
        return future

    def complete_response(
        self,
        *,
        ticket: AggregationTicket,
        response: object,
        source_state: str,
        message_type: str | None = None,
    ) -> object:
        with self._lock:
            batch, source = self._require_source(ticket)
            source.included_in_batch = False
            source.source_snapshot.update(
                {
                    "source_state": source_state,
                    "message_type": message_type,
                    "completed_at": _now_iso(),
                    "included_in_batch": False,
                    "updated_at": _now_iso(),
                }
            )
            self._refresh_batch_snapshot(batch)
            source.future.set_result(response)
            self._append_recent_source(source.source_snapshot)
            self._maybe_finalize_empty_batch_locked(batch)
        return response

    def complete_exception(
        self,
        *,
        ticket: AggregationTicket,
        exc: BaseException,
        message_type: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._lock:
            batch, source = self._require_source(ticket)
            source.included_in_batch = False
            source.source_snapshot.update(
                {
                    "source_state": "source_failed",
                    "message_type": message_type,
                    "failed_at": _now_iso(),
                    "included_in_batch": False,
                    "error_type": error_type,
                    "error_message": error_message,
                    "updated_at": _now_iso(),
                }
            )
            self._refresh_batch_snapshot(batch)
            source.future.set_exception(exc)
            self._append_recent_source(source.source_snapshot)
            self._maybe_finalize_empty_batch_locked(batch)

    def _maybe_finalize_empty_batch_locked(self, batch: _BatchState) -> None:
        included = [item for item in batch.sources.values() if item.included_in_batch]
        if included:
            self._maybe_start_submit_locked(batch)
            return
        if batch.sealed_at is None:
            return
        batch.batch_snapshot["batch_state"] = "batch_completed"
        batch.batch_snapshot["completed_at"] = _now_iso()
        self._refresh_batch_snapshot(batch)
        self._close_batch_locked(batch)

    def seal_batch(self, *, session_id: str, batch_id: str, reason: str) -> None:
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return
            batch = state.batches.get(batch_id)
            if batch is None or batch.sealed_at is not None:
                return
            batch.sealed_at = _now_iso()
            batch.batch_snapshot["batch_state"] = "batch_sealed"
            batch.batch_snapshot["sealed_reason"] = reason
            if state.open_batch_id == batch_id:
                state.open_batch_id = None
            self._refresh_batch_snapshot(batch)
            self._maybe_start_submit_locked(batch)

        log_event(
            "platform",
            "session_aggregation_batch_sealed",
            session_id=session_id,
            batch_id=batch_id,
            reason=reason,
        )

    def _maybe_start_submit_locked(self, batch: _BatchState) -> None:
        if batch.completed or batch.submitting or batch.sealed_at is None:
            return
        included_sources = [
            item
            for _, item in sorted(batch.sources.items())
            if item.included_in_batch
        ]
        if not included_sources:
            self._maybe_finalize_empty_batch_locked(batch)
            return
        if any(item.ready_message is None for item in included_sources):
            batch.batch_snapshot["batch_state"] = "batch_waiting_ready"
            self._refresh_batch_snapshot(batch)
            return
        if batch.handler is None:
            return
        batch.submitting = True
        batch.batch_snapshot["batch_state"] = "batch_ready_to_submit"
        self._refresh_batch_snapshot(batch)
        thread = threading.Thread(
            target=self._run_batch_handler,
            kwargs={"session_id": batch.session_id, "batch_id": batch.batch_id},
            daemon=True,
        )
        thread.start()

    def _run_batch_handler(self, *, session_id: str, batch_id: str) -> None:
        with self._lock:
            state = self._sessions.get(session_id)
            batch = None if state is None else state.batches.get(batch_id)
            if batch is None or batch.handler is None:
                return
            batch.batch_snapshot["batch_state"] = "batch_submitting"
            batch.batch_snapshot["submit_started_at"] = _now_iso()
            self._refresh_batch_snapshot(batch)
            included_sources = [
                item
                for _, item in sorted(batch.sources.items())
                if item.included_in_batch and item.ready_message is not None and item.input_record is not None
            ]
            aggregated = AggregatedSubmitItem(
                batch_id=batch.batch_id,
                session_id=batch.session_id,
                trace_id=included_sources[0].ticket.trace_id if included_sources else None,
                source_messages=[
                    AggregatedSourceMessage(
                        ticket=item.ticket,
                        trace_id=item.ticket.trace_id,
                        message=str(item.ready_message or ""),
                        input_record=dict(item.input_record or {}),
                    )
                    for item in included_sources
                ],
                opened_at=batch.opened_at,
                deadline_at=batch.deadline_at,
                sealed_at=str(batch.sealed_at or _now_iso()),
            )
            handler = batch.handler

        try:
            result = handler(aggregated)
        except BaseException as exc:
            with self._lock:
                state = self._sessions.get(session_id)
                batch = None if state is None else state.batches.get(batch_id)
                if batch is None:
                    return
                batch.completed = True
                batch.batch_snapshot["batch_state"] = "batch_failed"
                batch.batch_snapshot["failed_at"] = _now_iso()
                batch.batch_snapshot["error_type"] = type(exc).__name__
                batch.batch_snapshot["error_message"] = str(exc)
                for item in batch.sources.values():
                    if item.included_in_batch:
                        item.source_snapshot.update(
                            {
                                "source_state": "source_failed",
                                "failed_at": _now_iso(),
                                "error_type": type(exc).__name__,
                                "error_message": str(exc),
                            }
                        )
                        item.future.set_exception(exc)
                        self._append_recent_source(item.source_snapshot)
                self._refresh_batch_snapshot(batch)
                self._close_batch_locked(batch)
            return

        with self._lock:
            state = self._sessions.get(session_id)
            batch = None if state is None else state.batches.get(batch_id)
            if batch is None:
                return
            batch.completed = True
            batch.batch_snapshot["batch_state"] = "batch_completed"
            batch.batch_snapshot["completed_at"] = _now_iso()
            for item in batch.sources.values():
                if item.included_in_batch:
                    item.source_snapshot.update(
                        {
                            "source_state": "source_completed",
                            "completed_at": _now_iso(),
                        }
                    )
                    item.future.set_result(result)
                    self._append_recent_source(item.source_snapshot)
            self._refresh_batch_snapshot(batch)
            self._close_batch_locked(batch)

    def _close_batch_locked(self, batch: _BatchState) -> None:
        state = self._sessions.get(batch.session_id)
        if state is not None:
            if state.open_batch_id == batch.batch_id:
                state.open_batch_id = None
            state.batches.pop(batch.batch_id, None)
            if state.open_batch_id is None and not state.batches:
                self._sessions.pop(batch.session_id, None)
        if batch.timer is not None:
            batch.timer.cancel()
        self._append_recent_batch(batch.batch_snapshot)

    def _require_source(self, ticket: AggregationTicket) -> tuple[_BatchState, _SourceItem]:
        state = self._sessions.get(ticket.session_id)
        if state is None:
            raise KeyError(f"unknown session_id: {ticket.session_id}")
        batch = state.batches.get(ticket.batch_id)
        if batch is None:
            raise KeyError(f"unknown batch_id: {ticket.batch_id}")
        source = batch.sources.get(ticket.arrival_seq)
        if source is None:
            raise KeyError(f"unknown arrival_seq: {ticket.arrival_seq}")
        return batch, source

    def get_ticket_snapshot(self, *, ticket: AggregationTicket) -> dict[str, Any] | None:
        with self._lock:
            state = self._sessions.get(ticket.session_id)
            if state is not None:
                batch = state.batches.get(ticket.batch_id)
                if batch is not None:
                    source = batch.sources.get(ticket.arrival_seq)
                    if source is not None:
                        return self._copy(source.source_snapshot)
            for snapshot in reversed(self._recent_sources):
                if snapshot.get("session_id") == ticket.session_id and snapshot.get("arrival_seq") == ticket.arrival_seq:
                    return self._copy(snapshot)
        return None

    def get_session_snapshot(self, *, session_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._sessions.get(session_id)
            active_batches = []
            active_sources = []
            if state is not None:
                for batch_id in sorted(state.batches, key=lambda item: state.batches[item].batch_seq):
                    batch = state.batches[batch_id]
                    active_batches.append(self._copy(batch.batch_snapshot))
                    for arrival_seq in sorted(batch.sources):
                        active_sources.append(self._copy(batch.sources[arrival_seq].source_snapshot))
            recent_batches = [
                self._copy(item)
                for item in self._recent_batches
                if item.get("session_id") == session_id
            ]
            recent_sources = [
                self._copy(item)
                for item in self._recent_sources
                if item.get("session_id") == session_id
            ]
        return {
            "session_id": session_id,
            "active_batches": active_batches,
            "recent_batches": recent_batches,
            "active_sources": active_sources,
            "recent_sources": recent_sources,
        }
