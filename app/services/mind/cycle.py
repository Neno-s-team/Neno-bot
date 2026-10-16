from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app import config
from app.services.chat import llm_gateway as _llm_gateway
from app.services.mind import (
    appraisal,
    consistency,
    deliberation,
    expression,
    state,
)
from app.services.mind.memory import commit as memory_commit
from app.services.mind.memory import wm_builder
from app.services.mind.deliberation import L2Result
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


def _build_e1_plan_from_l3(l1: L1Appraisal, l3: L2Result) -> E1Plan:
    """有 L3 时直接用 L3 的输出构造 E1；gut 永远来自 L1。"""
    return E1Plan(
        say_points=[item[:200] for item in l3.say_points],
        gut=l1.gut,
        leak=l3.leak,
        length=l3.length,
        n_messages=min(max(1, l3.n_messages), max(1, int(config.MIND_E3_MAX_MESSAGES))),
        tone=l3.tone,
    )


def _run_l2(
    *,
    session_id: str,
    cycle_id: str,
    l1: L1Appraisal,
    message: str,
    history: list[dict],
    self_card: str,
    mind_state: dict[str, Any],
    inhibition_base: float,
    sticker_names: set[str],
    wm_json: str | None,
    existing: dict[str, Any],
    trace_id: str | None,
) -> tuple[L2Result | None, dict[str, Any] | None, dict[str, Any] | None]:
    """L2/L3 + 一致性检查。返回 (结果, wm, 失败信息)。

    返回 (None, wm, failure) 表示应走保守路径：失败信息含 degraded/check_json/attempts。
    """
    wm: dict[str, Any] | None = None
    if wm_json:
        wm = json.loads(wm_json)
    else:
        wm = wm_builder.build_working_memory(
            session_id, l1=l1, message=message, history=history, self_card=self_card,
        )
        update_cycle(cycle_id, wm_json=json.dumps(wm, ensure_ascii=False, default=str))

    leak_cap = state.compute_leak(
        arousal=mind_state["arousal"], base_inhibition=inhibition_base
    )
    attempts = int(existing.get("l2_attempts") or 0)
    checks: list[dict[str, Any]] = []
    if existing.get("check_json"):
        try:
            loaded = json.loads(existing["check_json"])
            checks = loaded if isinstance(loaded, list) else []
        except ValueError:
            checks = []
    # 续跑时把上次检查的错误带回重试请求
    errors: list[str] = list(checks[-1]["errors"]) if checks and not checks[-1].get("ok") else []
    last_error = ""
    result: L2Result | None = None

    while attempts < 2:
        attempts += 1
        update_cycle(cycle_id, l2_attempts=attempts)
        try:
            result, model_name = deliberation.run_deliberation(
                wm_text=wm["text"],
                l1=l1,
                message=message,
                self_card=self_card,
                leak_cap=leak_cap,
                sticker_names=sticker_names,
                extra_errors=errors or None,
                trace_id=trace_id,
                session_id=session_id,
            )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log_event(
                "mind", "l2_failed", trace_id=trace_id, session_id=session_id,
                attempt=attempts, error_type=type(exc).__name__,
            )
            if attempts >= 2:
                break
            errors = [f"上次输出无法解析：{type(exc).__name__}"]
            continue

        ok, errors, warnings = consistency.check_result(
            session_id, result, trace_id=trace_id
        )
        checks.append({"attempt": attempts, "ok": ok, "errors": errors, "warnings": warnings})
        update_cycle(cycle_id, check_json=json.dumps(checks, ensure_ascii=False))
        if ok:
            update_cycle(
                cycle_id,
                l2_json=json.dumps(result.to_dict(), ensure_ascii=False),
            )
            return result, wm, None
        log_event(
            "mind", "l2_check_failed", trace_id=trace_id, session_id=session_id,
            attempt=attempts, error_count=len(errors),
        )
        if attempts >= 2:
            last_error = "check: " + "; ".join(errors)
            break

    failure = {
        "degraded": "L2",
        "check_json": json.dumps(checks, ensure_ascii=False),
        "attempts": attempts,
        "error": last_error,
    }
    return None, wm, failure


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

    # ── L2 深思 + L3 自我 + 一致性检查 ─────────────────────────────────────────
    l2: L2Result | None = None
    wm: dict[str, Any] | None = None
    degraded: str | None = None
    if existing.get("l2_json"):
        l2 = L2Result.from_dict(
            json.loads(existing["l2_json"]),
            leak_cap=None,  # 回放已存结果，不重算上限
            max_messages=int(config.MIND_E3_MAX_MESSAGES),
            sticker_names=None,
        )
    else:
        skip_l2 = l1.depth == "fast" and config.MIND_FAST_SKIP_L2
        stored_wm = existing.get("wm_json")
        if skip_l2:
            log_event("mind", "l2_skipped_fast", trace_id=trace_id, session_id=session_id)
        elif existing.get("degraded") == "L2":
            # 续跑：上一轮 L2 已连续失败两次，沿用降级结果，不再重复调用
            degraded = "L2"
            log_event(
                "mind", "l2_degraded_resumed", trace_id=trace_id,
                session_id=session_id,
            )
        else:
            l2, wm, failure = _run_l2(
                session_id=session_id,
                cycle_id=cycle_id,
                l1=l1,
                message=message,
                history=history,
                self_card=self_card,
                mind_state=mind_state,
                inhibition_base=inhibition_base,
                sticker_names=sticker_names,
                wm_json=stored_wm,
                existing=existing,
                trace_id=trace_id,
            )
            if failure is not None:
                degraded = failure["degraded"]
                # 失败细节留在 check_json；completed 的 cycle 不写 error 字段
                update_cycle(cycle_id, degraded=degraded)

    # ── E1 规划 ───────────────────────────────────────────────────────────────
    if not existing.get("plan_json"):
        if l2 is not None:
            plan = _build_e1_plan_from_l3(l1, l2)
        else:
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
                hidden=l2.hidden if l2 is not None else None,
                sticker_hint=l2.sticker if l2 is not None else None,
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

    # ── 提交记忆 ──────────────────────────────────────────────────────────────
    # 只在 E3 成功、L2 有效（非降级）、且未提交过时执行
    latest = get_cycle(cycle_id) or existing
    committed = int(latest.get("memory_committed") or 0)
    if l2 is not None and not degraded and not committed:
        try:
            memory_commit.commit_memory(
                session_id=session_id,
                cycle_id=cycle_id,
                result=l2,
                trace_id=trace_id,
            )
            update_cycle(cycle_id, memory_committed=1)
        except Exception as exc:
            log_event(
                "mind", "memory_commit_failed", trace_id=trace_id,
                session_id=session_id, error_type=type(exc).__name__,
            )

    return final_text
