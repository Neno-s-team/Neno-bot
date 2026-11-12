from __future__ import annotations

import hashlib
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
    style_examples: str = "",
    line_lengths: list[int] | None = None,
    doing: str = "",
    initiative: bool = False,
) -> list[dict]:
    from app.services.mind import timeline
    recent_text = "\n".join(timeline.turns_with_time(recent_turns[-config.MIND_RECENT_TURNS:]))
    # 真实想法：只在漏得明显时才给，而且只说"心里是这么想的"，不要求"漏出来"
    inner = ""
    if plan.gut and plan.leak >= 0.35:
        inner = f"\n（你心里其实在想：{plan.gut}。说不说、说多少随你。）"
    hidden_line = ""
    if hidden:
        hidden_line = "\n（这些不打算说：" + "；".join(hidden) + "）"
    points = "；".join(plan.say_points) if plan.say_points else "随便回一下"
    lengths = "、".join(str(n) for n in (line_lengths or []))
    length_line = f"\n每条大概多长（字）：{lengths}。这是上限，更短也行。" if lengths else ""
    sticker_line = ""
    if sticker_hint:
        sticker_line = f"\n想发表情包就单独一行写 [表情包：{sticker_hint}]。"

    if initiative:
        # 主动找他：没有要回的消息，是她自己想说
        lead = f"（他这会儿没发消息，是你想主动找他。）\n\n你想说的意思：{points}"
        manner = "像平时手机上随手发消息那样打字"
    else:
        lead = f"对方：{message[:800]}\n\n你想回的意思：{points}"
        manner = "像平时手机上随手回那样打字"
    prompt = f"""{recent_text}
{lead}{inner}{hidden_line}
发 {plan.n_messages} 条。{length_line}{sticker_line}
{manner}，别解释、别总结、别为了显得自然而加戏。
{f"（你此刻：{doing}。被问到可以照实说，没问就不用提。）" if doing else "不知道自己此刻具体在干嘛就别编具体的事。"}一条一行，只输出要发的内容。"""

    system_blocks = [
        {"type": "text", "text": style_card[:1200]},
    ]
    if sticker_table_text:
        system_blocks.append({"type": "text", "text": sticker_table_text[:2000]})
    system_blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    if style_examples:
        # 每轮挑的片段不同，放在缓存块之后，不破坏风格卡/表情包表的缓存
        system_blocks.append({"type": "text", "text": style_examples[:4000]})
    return [
        {"role": "system", "content": system_blocks},
        {"role": "user", "content": prompt},
    ]


def draw_line_lengths(seed: str, n: int, length: str = "short") -> list[int]:
    """每条多长：按真实分布逐条抽档（上限字数），想说得多（length=long）时往长档偏。可重放。"""
    bands = [(int(cap), max(0.0, float(w))) for cap, w in (config.MIND_LINE_LENGTH_BANDS or [])]
    if not bands:
        return []
    lean = {"short": 0.0, "medium": 0.5, "long": 1.0}.get(length, 0.0)
    weights = [w * (1 + lean) ** k for k, (_, w) in enumerate(bands)]
    result = []
    for i in range(max(1, n)):
        draw = int.from_bytes(hashlib.sha256(f"{seed}:len:{i}".encode()).digest()[:8], "big") / 2**64
        cursor = draw * sum(weights)
        cap = bands[-1][0]
        for (band_cap, _), weight in zip(bands, weights):
            cursor -= weight
            if cursor < 0:
                cap = band_cap
                break
        result.append(cap)
    return result


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
    style_seed: str | None = None,
    doing: str = "",
    initiative: bool = False,
) -> str:
    from app.services.mind import style_corpus

    seed = style_seed or trace_id or ""
    situation = " ".join([message, *plan.say_points, plan.tone])
    examples = style_corpus.render(style_corpus.pick(situation, seed))
    messages = _build_e2_messages(
        plan, style_card, sticker_table_text, recent_turns, message,
        hidden=hidden, sticker_hint=sticker_hint, style_examples=examples,
        line_lengths=draw_line_lengths(seed, plan.n_messages, plan.length), doing=doing,
        initiative=initiative,
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


def run_e3(text: str, sticker_names: set[str] | None = None, max_messages: int | None = None) -> str:
    sticker_names = sticker_names or set()
    text = _replace_sticker_marks(text, sticker_names)
    text = _strip_markdown(text)
    limit = config.MIND_E3_MAX_MESSAGES if max_messages is None else max(1, min(max_messages, config.MIND_E3_MAX_MESSAGES))
    messages = _split_messages(
        text,
        max_total=limit,
        max_single=config.MIND_E3_MAX_SINGLE_CHARS,
    )
    # 去掉空行
    messages = [msg for msg in messages if msg.strip()]
    if not messages:
        raise ValueError("E3 produced empty messages")
    return "\n".join(messages)
