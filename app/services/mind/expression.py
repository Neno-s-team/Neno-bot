from __future__ import annotations

import re
from typing import Any

from app import config
from app.services.chat.llm_gateway import request_model_response
from app.services.chat.llm_gateway import _strip_leading_role_label
from app.services.mind.schemas import E1Plan
from app.utils.logging_utils import log_event
import json


def _build_e2_messages(
    plan: E1Plan,
    style_card: str,
    sticker_table_text: str,
    recent_turns: list[dict],
    message: str,
    *,
    hidden: list[str] | None = None,
    sticker_hint: str | None = None,
) -> list[dict]:
    recent_text = "\n".join(
        f"{item['role']}: {str(item.get('content',''))[:300]}"
        for item in recent_turns[-config.MIND_RECENT_TURNS:]
    )
    gut_hint = ""
    if plan.leak > 0.05 and plan.gut:
        gut_hint = f"\n注意：你的真实想法是「{plan.gut}」，可以按 leak={plan.leak:.2f} 的强度不经意地漏一点进回复里。"
    hidden_hint = ""
    if hidden:
        hidden_hint = (
            f"\n没打算直说的话（不要说出口，最多按 leak={plan.leak:.2f} 的强度含蓄流露）："
            + "；".join(hidden)
        )
    say_points_line = ""
    if plan.say_points:
        say_points_line = "\n- 说话要点（按顺序自然表达，不要逐条复述）：" + "；".join(plan.say_points)
    sticker_line = ""
    if sticker_hint:
        sticker_line = (
            f"\n如果你想发表情包，可以单独占一行输出 [表情包：{sticker_hint}]（名字必须精确）。"
        )

    prompt = f"""根据以下规划写出真人口语回复。

规划：{say_points_line}
- 条数：{plan.n_messages} 条
- 长度：{plan.length}
- 语气：{plan.tone}{gut_hint}{hidden_hint}{sticker_line}

最近原话：
{recent_text}

当前消息：
{message[:800]}

请输出多行纯文本，每行一条消息。不要加 markdown 格式。"""

    system_blocks = [
        {"type": "text", "text": style_card[:1200]},
    ]
    if sticker_table_text:
        system_blocks.append({"type": "text", "text": sticker_table_text[:2000]})
    system_blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    return [
        {"role": "system", "content": system_blocks},
        {"role": "user", "content": prompt},
    ]


def run_e2(
    plan: E1Plan,
    style_card: str,
    sticker_table_text: str,
    recent_turns: list[dict],
    message: str,
    *,
    hidden: list[str] | None = None,
    sticker_hint: str | None = None,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> str:
    messages = _build_e2_messages(
        plan, style_card, sticker_table_text, recent_turns, message,
        hidden=hidden, sticker_hint=sticker_hint,
    )
    raw = request_model_response(
        model_name=config.MIND_EXPR_MODEL,
        messages=messages,
        trace_id=trace_id,
        session_id=session_id,
        extra_body=config.MIND_EXPR_EXTRA_BODY,
        timeout=config.LLM_TIMEOUT_SECONDS,
        max_tokens=config.CHAT_MAX_TOKENS,
        stop=config.CHAT_STOP_SEQUENCES,
        purpose="mind_expr",
    )
    raw = _strip_leading_role_label(raw)
    log_event(
        "mind",
        "e2_expression_ok",
        trace_id=trace_id,
        session_id=session_id,
        reply_len=len(raw or ""),
    )
    return raw


# ── E3 渲染（纯代码）─────────────────────────────────────────────────────────

# 表情包标记正则与网关一致
MARK_PATTERN = re.compile(r"\[(表情包[:：]\s*)?([^\]\n]{1,40})\]")

def _replace_sticker_marks(text: str, sticker_names: set[str]) -> str:
    """处理表情包标记；裸 [X] 始终原样保留。"""
    if not sticker_names:
        return MARK_PATTERN.sub(
            lambda m: "" if m.group(1) is not None else m.group(0),
            text,
        )

    def repl(match: re.Match) -> str:
        explicit = match.group(1) is not None
        name = match.group(2).strip()
        if explicit:
            if name in sticker_names:
                return f"\n[表情包：{name}]\n"
            return ""
        return match.group(0)  # 裸 [X] 保留

    return MARK_PATTERN.sub(repl, text)


def _strip_markdown(text: str) -> str:
    """去掉 markdown 标记"""
    # 先替换表情包标记为占位符，避免 _ 等字符被删
    text = re.sub(MARK_PATTERN, lambda m: f"\x00{m.group(0)}\x00", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"```[\s\S]*?```", "\n", text)
    text = re.sub(r"\*\*|__", "", text)
    text = re.sub(r"\*", "", text)
    # 还原表情包标记
    text = re.sub(r"\x00(.+?)\x00", lambda m: m.group(1), text)
    return text.strip()


def _split_messages(text: str, max_total: int = 4, max_single: int = 200) -> list[str]:
    """按行拆条；单条超长时在标点处切开；超出的行合并进最后一条；总条数上限 max_total"""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    # 超长行切开
    expanded: list[str] = []
    for line in lines:
        if len(line) <= max_single:
            expanded.append(line)
            continue
        parts = re.split(r"(?<=。|！|？|\.|\!|\?)\s*", line)
        expanded.extend(p.strip() for p in parts if p.strip())
    lines = expanded
    if len(lines) <= max_total:
        return lines
    # 超出的行合并进最后一条（用空格连接）
    result: list[str] = []
    for line in lines:
        if len(result) < max_total - 1:
            result.append(line)
        else:
            if len(result) == max_total - 1:
                result.append(line)
            else:
                result[-1] += " " + line
    return result


def run_e3(text: str, sticker_names: set[str] | None = None) -> str:
    sticker_names = sticker_names or set()
    text = _replace_sticker_marks(text, sticker_names)
    text = _strip_markdown(text)
    messages = _split_messages(
        text,
        max_total=config.MIND_E3_MAX_MESSAGES,
        max_single=config.MIND_E3_MAX_SINGLE_CHARS,
    )
    # 去掉空行
    messages = [msg for msg in messages if msg.strip()]
    if not messages:
        raise ValueError("E3 produced empty messages")
    return "\n".join(messages)
