from __future__ import annotations

import hashlib
import json
from typing import Any

from app import config
from app.services.chat import llm_gateway as _llm_gateway
from app.services.mind import appraisal, expression, state
from app.services.mind.schemas import E1Plan, L1Appraisal
from app.services.mind.store import create_cycle, get_cycle, update_cycle
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


def _build_e1_plan(l1: L1Appraisal) -> E1Plan:
    """从 L1 推导 E1 规划（P1 无 L2/L3）"""
    length_map = {"fast": "short", "normal": "medium", "deep": "long"}
    n_messages = min(4, max(1, int(l1.urgency * 3) + 1))
    if l1.depth == "fast":
        n_messages = min(n_messages, 2)
    return E1Plan(
        say_points=[],
        gut=l1.gut,
        leak=0.0,
        length=length_map.get(l1.depth, "medium"),
        n_messages=n_messages,
        tone="中性",
    )


def _parse_sticker_table(injected_context: str) -> dict[str, Any]:
    """从注入块解析表情包表"""
    if not injected_context:
        return {}
    table: dict[str, Any] = {}
    for line in injected_context.split("\n"):
        line = line.strip()
        if not line or line.startswith("【"):
            continue
        if ":" in line:
            key, val = line.split(":", 1)
            table[key.strip()] = val.strip()
    return table


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

    cycle_id = _derive_cycle_id(session_id, trace_id or "")
    existing = get_cycle(cycle_id)

    if existing is None:
        create_cycle(cycle_id, session_id, trigger_type="message", trigger_ref=trace_id)
        existing = get_cycle(cycle_id)

    self_card = _load_self_card()
    style_card = _load_style_card()
    mind_state = state.read_state(session_id)

    # ── L1 ────────────────────────────────────────────────────────────────────
    if not existing.get("l1_json"):
        try:
            recent_turns = [item for item in history if item.get("role") in ("user", "assistant")]
            l1 = appraisal.run_l1(
                message=message,
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
        plan.leak = state.compute_leak(arousal=mind_state["arousal"], base_inhibition=None)
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
                recent_turns=recent_turns,
                message=message,
                injected_context=injected_context,
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
        sticker_table = _parse_sticker_table(injected_context)
        final_text = expression.run_e3(expr_text, sticker_table=sticker_table)
        update_cycle(cycle_id, final_text=final_text, status="completed")
    else:
        final_text = existing["final_text"]
        update_cycle(cycle_id, status="completed")

    return final_text
