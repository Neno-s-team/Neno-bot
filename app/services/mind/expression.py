from __future__ import annotations

import re
from typing import Any

from app.config import MIND_EXPR_EXTRA_BODY, MIND_EXPR_MODEL, MIND_RECENT_TURNS
from app.services.chat.llm_gateway import request_model_response
from app.services.mind.schemas import E1Plan
from app.utils.logging_utils import log_event


def _build_e2_messages(
    plan: E1Plan,
    style_card: str,
    recent_turns: list[dict],
    message: str,
    injected_context: str,
) -> list[dict]:
    recent_text = "\n".join(
        f"{item['role']}: {str(item.get('content',''))[:300]}"
        for item in recent_turns[-MIND_RECENT_TURNS:]
    )
    gut_hint = ""
    if plan.leak > 0.05 and plan.gut:
        gut_hint = f"\n注意：你的真实想法是「{plan.gut}」，可以按 leak={plan.leak:.2f} 的强度不经意地漏一点进回复里。"

    prompt = f"""根据以下规划写出真人口语回复。

规划：
- 条数：{plan.n_messages} 条
- 长度：{plan.length}
- 语气：{plan.tone}{gut_hint}

最近原话：
{recent_text}

当前消息：
{message[:800]}

{injected_context if injected_context else ''}

请输出多行纯文本，每行一条消息。不要加 markdown 格式。"""

    system_blocks = [{"type": "text", "text": style_card[:1200]}]
    system_blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    return [
        {"role": "system", "content": system_blocks},
        {"role": "user", "content": prompt},
    ]


def run_e2(
    plan: E1Plan,
    style_card: str,
    recent_turns: list[dict],
    message: str,
    injected_context: str = "",
    *,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> str:
    messages = _build_e2_messages(plan, style_card, recent_turns, message, injected_context)
    raw = request_model_response(
        model_name=MIND_EXPR_MODEL,
        messages=messages,
        trace_id=trace_id,
        session_id=session_id,
        extra_body=MIND_EXPR_EXTRA_BODY,
        purpose="mind_expr",
    )
    log_event(
        "mind",
        "e2_expression_ok",
        trace_id=trace_id,
        session_id=session_id,
        reply_len=len(raw or ""),
    )
    return raw


# ── E3 渲染（纯代码）─────────────────────────────────────────────────────────

def _strip_markdown(text: str) -> str:
    """去掉 markdown 标记"""
    # 先删标题、列表符号，再删代码块（避免代码块内容被后续规则污染）
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"```[\s\S]*?```", "\n", text)
    text = re.sub(r"\*\*|__", "", text)
    text = re.sub(r"[*_`]", "", text)
    return text.strip()


def _split_messages(text: str, max_total: int = 4) -> list[str]:
    """按行拆条；单条超长时在标点处切开；总条数上限 max_total"""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    # 如果行数本身在限制内，直接按行返回，不做切分
    if len(lines) <= max_total:
        return lines
    # 行数超限：按行拆分，超长行再按标点切开
    result: list[str] = []
    for line in lines:
        if len(result) >= max_total:
            break
        if len(line) <= 800:
            result.append(line)
            continue
        parts = re.split(r"(?<=。|！|？|\.|\!|\?)\s+", line)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if len(result) >= max_total:
                break
            result.append(part)
    return result


def _validate_stickers(messages: list[str], sticker_table: dict[str, Any]) -> list[str]:
    """表情包标记 [表情包：名字] 的名字必须在 sticker_table 里，否则删掉"""
    if not sticker_table:
        return messages
    allowed = set(sticker_table.keys()) if isinstance(sticker_table, dict) else set()
    result: list[str] = []
    for msg in messages:
        def repl(match: re.Match) -> str:
            name = match.group(1)
            return match.group(0) if name in allowed else ""
        msg = re.sub(r"\[表情包：([^\]]+)\]", repl, msg)
        result.append(msg)
    return result


def run_e3(text: str, sticker_table: dict[str, Any] | None = None) -> str:
    text = _strip_markdown(text)
    messages = _split_messages(text)
    messages = _validate_stickers(messages, sticker_table or {})
    return "\n".join(messages)
