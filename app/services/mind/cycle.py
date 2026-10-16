from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app import config
from app.services.chat import llm_gateway as _llm_gateway
from app.services.mind import appraisal, expression, state, store
from app.services.mind.schemas import E1Plan, L1Appraisal
from app.services.mind.store import (
    create_cycle,
    get_cycle,
    update_cycle,
    get_mind_session_context,
    upsert_mind_session_context,
)
from app.utils.logging_utils import log_event


def _derive_cycle_id(session_id: str, trace_id: str) -> str:
    return hashlib.sha256(f"{session_id}:{trace_id}".encode()).hexdigest()[:32]


def _load_self_card() -> str:
    from app.prompt.prompt_loader import load_text
    try:
        return load_text(config.MIND_SELF_CARD_PATH)
    except FileNotFoundError:
        return ""


def _load_style_card() -> str:
    from app.prompt.prompt_loader import load_text
    try:
        return load_text(config.MIND_STYLE_CARD_PATH)
    except FileNotFoundError:
        return ""


def _parse_inhibition_base(self_card: str) -> float:
    """从自我卡解析抑制基线；支持「:」和「：」，失败用 0.7。"""
    for line in self_card.split("\n"):
        if "抑制基线" not in line:
            continue
        match = re.search(r"抑制基线\s*[:：]\s*([^\s]+)", line)
        if not match:
            continue
        try:
            return max(0.0, min(1.0, float(match.group(1))))
        except ValueError:
            continue
    return 0.7


def _build_e1_plan(l1: L1Appraisal) -> E1Plan:
    """从 L1 推导 E1 规划（P1 无 L2/L3）"""
    # 按规格推导 n_messages 和 length
    if l1.category in {"分享", "求安慰", "冲突"} and l1.affect_delta.arousal > 0.4:
        n_messages = 3
        length = "medium"
    elif l1.category in {"确认", "晚回复"}:
        n_messages = 1
        length = "short"
    elif l1.category == "提问":
        n_messages = 2
        length = "medium"
    else:
        n_messages = 2
        length = "medium"
    if l1.depth == "deep":
        length = "long"
    # 上限受 MIND_E3_MAX_MESSAGES 约束
    n_messages = min(n_messages, config.MIND_E3_MAX_MESSAGES)
    return E1Plan(
        say_points=[],
        gut=l1.gut,
        leak=0.0,
        length=length,
        n_messages=n_messages,
        tone="中性",
    )


def _compute_tone(valence: float, arousal: float) -> str:
    """valence/arousal → 5 档中文语气词。"""
    if valence < -0.2 and arousal > 0.4:
        return "烦躁"
    if valence < -0.2:
        return "低落"
    if arousal > 0.6 and valence >= -0.2:
        return "兴奋"
    if valence > 0.3:
        return "轻快"
    return "平静"


def _parse_sticker_names(injected_context: str) -> set[str]:
    """从注入块解析表情包名字集合"""
    if not injected_context:
        return set()
    # 取最后一个「：\n」之后的内容，按「、」切分
    last_colon = injected_context.rfind("：\n")
    if last_colon == -1:
        return set()
    names_text = injected_context[last_colon + 2:]
    return {name.strip() for name in names_text.split("、") if name.strip()}


def run_cycle(
    session_id: str,
    message: str,
    history: list[dict],
    messages: list[dict],
    *,
    trace_id: str | None = None,
    injected_context: str = "",
) -> str:
    if not config.MIND_ENABLED:
        raise RuntimeError("run_cycle called while MIND_ENABLED is false")
    if not config.MIND_L1_MODEL:
        log_event("mind", "mind_l1_model_missing", trace_id=trace_id, session_id=session_id)
        return _llm_gateway.generate_chat_reply(messages, trace_id=trace_id, session_id=session_id)

    cycle_id = _derive_cycle_id(session_id, trace_id or "")
    existing = get_cycle(cycle_id)

    if existing is None:
        create_cycle(cycle_id, session_id, trigger_type="message", trigger_ref=trace_id)
        existing = get_cycle(cycle_id)

    self_card = _load_self_card()
    style_card = _load_style_card()
    mind_state = state.read_state(session_id)
    inhibition_base = _parse_inhibition_base(self_card)

    # ── 表情包表：本轮带注入就覆盖保存；本轮没带就读上次保存的 ─────────────────
    sticker_table_text = ""
    sticker_names: set[str] = set()
    if injected_context:
        sticker_names = _parse_sticker_names(injected_context)
        if sticker_names:
            sticker_table_text = injected_context
            upsert_mind_session_context(
                session_id, injected_context, json.dumps(list(sticker_names), ensure_ascii=False)
            )
    else:
        saved = get_mind_session_context(session_id)
        if saved:
            sticker_table_text = saved["sticker_table_text"]
            sticker_names = set(json.loads(saved["sticker_names_json"]))

    # ── L1 ────────────────────────────────────────────────────────────────────
    if not existing.get("l1_json"):
        try:
            recent_turns = [item for item in history if item.get("role") in ("user", "assistant")]
            l1 = appraisal.run_l1(
                message=message,  # clean_message（无注入块）
                state=mind_state,
                self_card=self_card[:600],
                recent_turns=recent_turns,
                trace_id=trace_id,
                session_id=session_id,
            )
            l1_json = json.dumps(l1.to_dict(), ensure_ascii=False)
            update_cycle(cycle_id, l1_json=l1_json)
            state.apply_affect_delta(session_id, l1.affect_delta.valence, l1.affect_delta.arousal)
            mind_state = state.read_state(session_id)
        except Exception as exc:
            log_event(
                "mind",
                "cycle_failed",
                trace_id=trace_id,
                session_id=session_id,
                error_type=type(exc).__name__,
                reason="L1 failed",
            )
            update_cycle(cycle_id, status="failed", error=f"L1: {type(exc).__name__}")
            return _llm_gateway.generate_chat_reply(messages, trace_id=trace_id, session_id=session_id)
    else:
        l1 = L1Appraisal.from_dict(json.loads(existing["l1_json"]))

    # ── E1 规划 ───────────────────────────────────────────────────────────────
    if not existing.get("plan_json"):
        plan = _build_e1_plan(l1)
        plan.leak = state.compute_leak(arousal=mind_state["arousal"], base_inhibition=inhibition_base)
        plan.tone = _compute_tone(mind_state["valence"], mind_state["arousal"])
        plan_json = json.dumps(plan.to_dict(), ensure_ascii=False)
        update_cycle(cycle_id, plan_json=plan_json)
    else:
        plan = E1Plan.from_dict(json.loads(existing["plan_json"]))

    # ── E2 措辞 ───────────────────────────────────────────────────────────────
    if existing.get("expr_text") is None:
        try:
            recent_turns = [item for item in history if item.get("role") in ("user", "assistant")]
            expr_text = expression.run_e2(
                plan=plan,
                style_card=style_card,
                sticker_table_text=sticker_table_text,
                recent_turns=recent_turns,
                message=message,  # clean_message
                trace_id=trace_id,
                session_id=session_id,
            )
            update_cycle(cycle_id, expr_text=expr_text)
        except Exception as exc:
            log_event(
                "mind",
                "cycle_failed",
                trace_id=trace_id,
                session_id=session_id,
                error_type=type(exc).__name__,
                reason="E2 failed",
            )
            update_cycle(cycle_id, status="failed", error=f"E2: {type(exc).__name__}")
            return _llm_gateway.generate_chat_reply(messages, trace_id=trace_id, session_id=session_id)
    else:
        expr_text = existing["expr_text"]

    # ── E3 渲染 ───────────────────────────────────────────────────────────────
    if existing.get("final_text") is None:
        try:
            final_text = expression.run_e3(expr_text, sticker_names=sticker_names)
            update_cycle(cycle_id, final_text=final_text, status="completed")
        except Exception as exc:
            log_event(
                "mind",
                "cycle_failed",
                trace_id=trace_id,
                session_id=session_id,
                error_type=type(exc).__name__,
                reason="E3 failed",
            )
            update_cycle(cycle_id, status="failed", error=f"E3: {type(exc).__name__}")
            return _llm_gateway.generate_chat_reply(messages, trace_id=trace_id, session_id=session_id)
    else:
        final_text = existing["final_text"]
        update_cycle(cycle_id, status="completed")

    return final_text
