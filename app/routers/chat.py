"""本地 HTTP 聊天入口：输入归一化 → 聚合窗口 → 串行提交 → 聊天 turn。

并发模型（外部异步，内部串行）：
    - SessionAggregationController：同一 session 的突发消息在窗口内合并为一个批次
    - SessionSubmitController：同一 session 的批次严格串行执行，绝不并发跑 turn
"""

from __future__ import annotations

import time
from copy import deepcopy
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app import config
from app.schemas import ChatRequest, ChatResponse
from app.security import require_internal_token
from app.session_identity import SessionIdentityError, canonical_chat_session_id
from app.services.chat.image_memory import prepare_attachments
from app.services.chat_service import run_chat_turn
from app.services.session_aggregation_controller import (
    AggregatedSubmitItem,
    AggregatedSourceMessage,
    SessionAggregationController,
)
from app.services.session_submit_controller import SessionSubmitController
from app.services.stats_service import record_chat_stat
from app.utils.logging_utils import log_event, new_trace_id

router = APIRouter(tags=["chat"], dependencies=[Depends(require_internal_token)])

session_submit_controller = SessionSubmitController()
session_aggregation_controller = SessionAggregationController(
    window_seconds=config.SESSION_AGGREGATION_WINDOW_SECONDS,
)

LOCAL_SOURCE = "local"


def _batch_trace_id(batch: AggregatedSubmitItem) -> str:
    for source in batch.source_messages:
        if source.trace_id:
            return source.trace_id
    return new_trace_id()


def _build_merged_message(source_messages: list[AggregatedSourceMessage]) -> str:
    if len(source_messages) == 1:
        return source_messages[0].message
    parts = [f"[用户在短时间内连续发送了 {len(source_messages)} 条内容]"]
    for index, source in enumerate(source_messages, start=1):
        message_type = str(source.input_record.get("message_type") or "text")
        parts.append(f"[第{index}条][{message_type}]\n{source.message}")
    return "\n\n".join(parts)


def _build_user_records(
    batch: AggregatedSubmitItem,
    merged_message: str,
    trace_id: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for source in batch.source_messages:
        metadata = deepcopy(source.input_record)
        metadata["aggregation"] = {
            "batch_id": batch.batch_id,
            "is_aggregated": len(batch.source_messages) > 1,
            "source_count": len(batch.source_messages),
            "source_trace_id": source.trace_id,
            "source_arrival_seq": source.ticket.arrival_seq,
            "batch_trace_id": trace_id,
        }
        records.append(
            {
                "content": source.message,
                "message_type": str(source.input_record.get("message_type") or "text"),
                "source": str(source.input_record.get("source") or LOCAL_SOURCE),
                "metadata": metadata,
            }
        )
    return records


def _submit_aggregated_batch(batch: AggregatedSubmitItem) -> dict:
    """聚合批次 → 提交锁（同 session 串行）→ run_chat_turn。"""
    trace_id = _batch_trace_id(batch)
    session_id = batch.session_id
    merged_message = _build_merged_message(batch.source_messages)
    message_type = (
        "aggregated"
        if len(batch.source_messages) > 1
        else str(batch.source_messages[0].input_record.get("message_type") or "text")
    )

    submit_ticket = session_submit_controller.allocate_ticket(
        session_id=session_id,
        trace_id=trace_id,
    )
    user_records = _build_user_records(batch, merged_message, trace_id)

    future = session_submit_controller.submit_ready(
        ticket=submit_ticket,
        handler=lambda: run_chat_turn(
            session_id,
            merged_message,
            trace_id=trace_id,
            source=LOCAL_SOURCE,
            message_type=message_type,
            user_records=user_records,
        ),
    )
    return future.wait()


def _normalize_input(req: ChatRequest, trace_id: str) -> tuple[str, str]:
    """Persist attachments before gateway temp cleanup; never pre-call a vision model."""
    attachments = req.attachments or []
    has_image_attachment = any(item.kind == "image" for item in attachments)
    base_message = (req.message or "").strip()

    if not base_message and not has_image_attachment:
        raise HTTPException(status_code=400, detail="message must not be blank")

    if not has_image_attachment:
        return base_message, "text"

    try:
        from app.services.chat_service import split_injected_context
        req.attachments = prepare_attachments(
            req.session_id.strip(), attachments, split_injected_context(base_message)[1]
        )
    except Exception as exc:
        log_event("chat", "image_persist_failed", trace_id=trace_id,
                  session_id=req.session_id, error_type=type(exc).__name__)
        raise HTTPException(status_code=400, detail="图片无法读取或保存，请发送有效图片（每张不超过 20 MiB）。") from exc
    log_event("chat", "image_direct_input_ready", trace_id=trace_id,
              session_id=req.session_id, image_count=sum(item.kind == "image" for item in req.attachments))
    return base_message or "[用户发送了图片]", "image"


def _validate_session_id(session_id: str) -> str:
    try:
        return canonical_chat_session_id(session_id)
    except SessionIdentityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    from app.services.chat.idempotency import execute
    canonical = _validate_session_id(req.session_id)
    return execute(req.model_copy(update={"session_id": canonical}), _chat)


def _chat(req: ChatRequest, trace_id: str | None = None):
    started = time.perf_counter()
    trace_id = trace_id or new_trace_id()
    session_id = _validate_session_id(req.session_id)
    source = (req.source or LOCAL_SOURCE).strip()[:64] or LOCAL_SOURCE

    # ① 聚合窗口：同 session 突发消息合并为一个批次
    aggregation_ticket = session_aggregation_controller.allocate_ticket(
        session_id=session_id,
        trace_id=trace_id,
    )

    # ② 输入归一化（请求线程内完成，不占 session 串行窗口）
    try:
        message, message_type = _normalize_input(req, trace_id)
    except HTTPException as exc:
        session_aggregation_controller.complete_exception(
            ticket=aggregation_ticket,
            exc=exc,
            message_type="text",
            error_type=type(exc).__name__,
            error_message=str(exc.detail),
        )
        raise

    input_record: dict[str, Any] = {
        "source": source,
        "message_type": message_type,
        "raw_input": req.message,
        "normalized_input": message,
        "attachments": [item.model_dump() for item in (req.attachments or [])],
        "trace_id": trace_id,
    }

    log_event(
        "chat",
        "chat_request_received",
        trace_id=trace_id,
        session_id=session_id,
        message_len=len(message or ""),
        message_type=message_type,
        attachment_count=len(req.attachments or []),
    )

    # ③ 批次就绪 → 窗口关闭后由 handler 提交（同 session 串行）
    aggregation_future = session_aggregation_controller.mark_ready(
        ticket=aggregation_ticket,
        message=message,
        input_record=input_record,
        handler=_submit_aggregated_batch,
    )

    try:
        result = aggregation_future.wait()
    except BaseException as exc:
        # 批次已在控制器内部结算（future 携带异常），这里只做统计与对外映射
        record_chat_stat(
            source=source,
            session_id=session_id,
            message=message,
            reply="",
            success=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail="chat failed") from exc

    record_chat_stat(
        source=source,
        session_id=session_id,
        message=message,
        reply=result.get("reply"),
        success=True,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )

    return ChatResponse(
        reply=result["reply"],
        trace_id=result.get("trace_id"),
        session_id=session_id,
        user_message_id=result.get("user_message_id"),
        user_message_ids=result.get("user_message_ids") or [],
        assistant_message_id=result.get("assistant_message_id"),
        message_type=result.get("message_type"),
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
