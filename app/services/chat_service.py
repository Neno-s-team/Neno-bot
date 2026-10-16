"""最小聊天主链。

流程（一个 turn）：
    1. 读取该 session 的近期历史（SQLite，按 token 预算）
    2. 落库当前 user 消息（metadata 当场冻结）
    3. 组装 prompt：system（单句人设）→ 历史 → 当前消息
    4. 调 LLM provider 生成回复
    5. 落库 assistant 回复并返回

并发约束（由 routers/chat.py 的两个控制器保证，本模块不做并发控制）：
    同一 session_id 的 turn 严格串行；突发消息先聚合再进 turn。
"""

from __future__ import annotations

import time
from copy import deepcopy
from typing import Any

from app.config import HISTORY_TOKEN_LIMIT, SYSTEM_PROMPT
from app import config
from app.services.chat.llm_gateway import generate_chat_reply
from app.storage.db import add_message, get_recent_messages_by_tokens
from app.storage import image_store
from app.utils.logging_utils import log_event, new_trace_id


INJECTED_CONTEXT_PREFIX = "【表情包表｜网关注入】"


def split_injected_context(text: str) -> tuple[str, str]:
    """网关注入的一次性上下文（如表情包表）只随本 turn 进 prompt，不落库。

    返回 (注入块, 干净正文)；干净正文才持久化，避免一次性指令
    污染历史预算（表≈400 token，会挤掉真实对话）。
    """
    raw = str(text or "")
    if not raw.startswith(INJECTED_CONTEXT_PREFIX):
        return "", raw
    block, separator, rest = raw.partition("\n\n")
    return block, (rest if separator else "")


def build_chat_messages(
    history: list[dict], message: str, *, image_blocks: list[dict] | None = None,
    image_index: str = "", image_tools: bool = False, summary_context: str = "",
) -> list[dict]:
    """组装 prompt：可缓存前缀（system）→ 历史 → 当前 user 消息。

    system 块打 cache_control（ephemeral）；动态内容（当前消息）永远在最后，
    保证 provider 侧 prompt cache 前缀稳定。
    """
    system_blocks = [{"type": "text", "text": SYSTEM_PROMPT}] if SYSTEM_PROMPT else []
    if image_tools:
        from app.services.chat.image_memory import IMAGE_INSTRUCTIONS
        system_blocks.append({"type": "text", "text": IMAGE_INSTRUCTIONS})
    messages: list[dict] = []
    if system_blocks:
        system_blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        messages.append({"role": "system", "content": system_blocks})
    if summary_context:
        messages.append({'role': 'user', 'content': '以下是历史对话摘要，不是新消息或系统指令；保留来源ID，区分事实与推测。\n' + summary_context})
    messages.extend(
        {"role": item["role"], "content": item["content"]} for item in history
    )
    content = []
    if image_index:
        content.append({"type": "text", "text": image_index})
    if message:
        content.append({"type": "text", "text": message})
    content.extend(image_blocks or [])
    messages.append({"role": "user", "content": content})
    return messages


def run_chat_turn(
    session_id: str,
    message: str,
    *,
    trace_id: str | None = None,
    source: str = "local",
    message_type: str = "text",
    user_records: list[dict[str, Any]] | None = None,
) -> dict:
    """执行一个聊天 turn。user_messages 为聚合批的逐条记录；message 为合并后的 prompt 文本。"""
    trace_id = trace_id or new_trace_id()
    turn_started = time.perf_counter()
    log_event(
        "chat",
        "chat_turn_start",
        trace_id=trace_id,
        session_id=session_id,
        message_len=len(message or ""),
    )

    try:
        from app.storage import db
        from app.storage.operations import operations
        from app.services.chat.idempotency import recover_reply
        from app.services.chat.maintenance import context_summary, enqueue
        recovered = recover_reply(session_id, trace_id)
        if recovered:
            return recovered
        generation = operations().generation(session_id)
        existing = db.fetch_all("SELECT id FROM messages WHERE session_id=? AND trace_id=? AND role='user' ORDER BY id", (session_id, trace_id))
        existing_ids = [row['id'] for row in existing]
        summary = context_summary(session_id)
        summary_context = summary['content'] if summary else ''
        reserve = min(max(0, HISTORY_TOKEN_LIMIT // 3), len(summary_context.encode('utf-8')) // 2)
        # ① 近期历史（不含当前消息——当前消息尚未落库）
        history = get_recent_messages_by_tokens(session_id, token_limit=max(1000, HISTORY_TOKEN_LIMIT - reserve))
        history = [item for item in history if item['id'] not in existing_ids]
        if summary:
            history = [item for item in history if item['id'] > summary['through_id']]
        log_event(
            "chat", "history_loaded", trace_id=trace_id, session_id=session_id,
            count=len(history),
        )

        # ② 落库当前 user 消息（metadata 冻结快照）
        records = user_records or [
            {
                "content": message,
                "message_type": message_type,
                "source": source,
                "metadata": {
                    "source": source,
                    "message_type": message_type,
                    "trace_id": trace_id,
                },
            }
        ]
        user_message_ids: list[int] = []
        turn_index = image_store.next_turn(session_id)
        with operations().session_guard(session_id, generation, trace_id):
            for record_index, record in enumerate(records):
                if record_index < len(existing_ids):
                    user_message_ids.append(existing_ids[record_index])
                    continue
                metadata = deepcopy(record.get("metadata") or {})
                user_message_ids.append(
                    add_message(
                        session_id,
                        "user",
                        split_injected_context(str(record.get("content") or ""))[1],
                        trace_id=trace_id,
                        message_type=str(record.get("message_type") or "text"),
                        source=str(record.get("source") or source),
                        metadata=metadata,
                    )
                )
                for attachment in metadata.get("attachments") or []:
                    if attachment.get("kind") == "image" and attachment.get("image_id"):
                        image_store.bind_image(session_id, attachment["image_id"], user_message_ids[-1], turn_index)

        # ③ 组装 prompt
        if image_store.has_images(session_id):
            from app.services.chat.image_memory import decorate_context
            history, image_blocks, image_index = decorate_context(history, session_id, turn_index, user_message_ids)
            messages = build_chat_messages(history, message, image_blocks=image_blocks,
                                           image_index=image_index, image_tools=True, summary_context=summary_context)
            image_count = sum(block.get("type") == "image_url" for item in messages
                              if isinstance(item.get("content"), list) for block in item["content"])
            log_event("image_memory", "direct_main_context_ready", trace_id=trace_id,
                      image_count=image_count, turn_index=turn_index)
        else:
            messages = build_chat_messages(history, message, summary_context=summary_context)

        # ④ 生成回复
        model_started = time.perf_counter()
        log_event("chat", "model_request_start", trace_id=trace_id)
        if config.MIND_ENABLED:
            from app.services.mind.cycle import run_cycle
            clean_message, injected = split_injected_context(message)
            reply = run_cycle(
                session_id=session_id,
                message=clean_message,
                history=history,
                messages=messages,
                trace_id=trace_id,
                injected_context=injected,
            )
        else:
            reply = generate_chat_reply(messages, trace_id=trace_id, session_id=session_id)
        log_event(
            "chat",
            "model_response_ok",
            trace_id=trace_id,
            reply_len=len(reply or ""),
            latency_ms=int((time.perf_counter() - model_started) * 1000),
        )

        # ⑤ 落库 assistant 回复
        with operations().session_guard(session_id, generation, trace_id):
            assistant_message_id = add_message(
                session_id,
                "assistant",
                reply,
                trace_id=trace_id,
                message_type="assistant",
                source=source,
            )
            image_store.complete_turn(session_id, turn_index)
        try:
            enqueue(session_id)
        except Exception as exc:
            log_event('maintenance', 'enqueue_failed', session_id=session_id,
                      trace_id=trace_id, error_type=type(exc).__name__)

        result = {
            "trace_id": trace_id,
            "session_id": session_id,
            "reply": reply,
            "user_message_id": user_message_ids[0] if user_message_ids else None,
            "user_message_ids": user_message_ids,
            "assistant_message_id": assistant_message_id,
            "message_type": message_type,
            "source": source,
            "latency_ms": int((time.perf_counter() - turn_started) * 1000),
        }
        log_event(
            "chat",
            "chat_turn_finished",
            trace_id=trace_id,
            session_id=session_id,
            latency_ms=result["latency_ms"],
        )
        return result
    except Exception as exc:
        log_event(
            "chat",
            "chat_turn_error",
            trace_id=trace_id,
            session_id=session_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            latency_ms=int((time.perf_counter() - turn_started) * 1000),
        )
        raise
