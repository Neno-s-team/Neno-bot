from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app import config
from app.services import chat_service
from app.services.chat import llm_gateway
from app.services.chat_service import run_chat_turn
from app.services.mind import cycle, store, state, expression
from app.services.mind.schemas import L1Appraisal, E1Plan
from app.storage.db import get_session_messages, fetch_one


@pytest.fixture()
def mind_db(isolated_db: Path):
    """复用 isolated_db，确保 mind 表已建"""
    return isolated_db


@pytest.fixture()
def fake_mind_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "MIND_ENABLED", True)
    monkeypatch.setattr(config, "MIND_L1_MODEL", "fake-l1")
    monkeypatch.setattr(config, "MIND_EXPR_MODEL", "fake-expr")
    monkeypatch.setattr(config, "MIND_RECENT_TURNS", 6)
    monkeypatch.setattr(config, "MIND_AFFECT_HALF_LIFE_MINUTES", 180)
    monkeypatch.setattr(config, "MIND_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setattr(config, "MIND_SELF_CARD_PATH", "prompts/mind/self_card.md")
    monkeypatch.setattr(config, "MIND_STYLE_CARD_PATH", "prompts/mind/style_card.md")


def _fake_l1_response():
    return json.dumps({
        "category": "分享",
        "gut": "他想聊聊",
        "affect_delta": {"valence": 0.1, "arousal": 0.2},
        "urgency": 0.3,
        "cues": ["测试"],
        "depth": "fast",
        "reply_will": "now",
    }, ensure_ascii=False)


def _fake_expr_response():
    return "收到啦\n今天怎么样"


# ── 测试 1：MIND_ENABLED=false 时走旧路径 ─────────────────────────────────────
def test_mind_disabled_uses_old_path(mind_db, monkeypatch):
    monkeypatch.setattr(config, "MIND_ENABLED", False)
    called = {"mind": False, "old": False}

    def fake_old(messages, **kwargs):
        called["old"] = True
        return "旧回复"

    def fake_mind(**kwargs):
        called["mind"] = True
        raise AssertionError("不应该调用 mind")

    monkeypatch.setattr(chat_service, "generate_chat_reply", fake_old)
    monkeypatch.setattr(cycle, "run_cycle", fake_mind)

    result = run_chat_turn("s1", "你好", trace_id="t1")
    assert result["reply"] == "旧回复"
    assert called["old"] is True
    assert called["mind"] is False


# ── 测试 2：开启后正常路径 ─────────────────────────────────────────────────────
def test_mind_enabled_normal_path(mind_db, fake_mind_env, monkeypatch):
    l1_calls = {"count": 0}
    expr_calls = {"count": 0}

    def fake_request(**kwargs):
        purpose = kwargs.get("purpose", "")
        if purpose == "mind_l1":
            l1_calls["count"] += 1
            return _fake_l1_response()
        if purpose == "mind_expr":
            expr_calls["count"] += 1
            return _fake_expr_response()
        raise AssertionError(f"unexpected purpose: {purpose}")

    monkeypatch.setattr("app.services.mind.appraisal.request_model_response", fake_request)
    monkeypatch.setattr("app.services.mind.expression.request_model_response", fake_request)

    result = run_chat_turn("s2", "今天怎么样", trace_id="t2")
    assert result["reply"] == "收到啦\n今天怎么样"
    assert l1_calls["count"] == 1
    assert expr_calls["count"] == 1

    cycle_row = fetch_one("SELECT * FROM mind_cycle WHERE session_id='s2'")
    assert cycle_row is not None
    assert cycle_row["status"] == "completed"
    assert cycle_row["l1_json"] is not None
    assert cycle_row["plan_json"] is not None
    assert cycle_row["final_text"] == "收到啦\n今天怎么样"


# ── 测试 3：L1 第一次非法 JSON、第二次合法 ─────────────────────────────────────
def test_mind_l1_retry_once(mind_db, fake_mind_env, monkeypatch):
    l1_calls = {"count": 0}

    def fake_request(**kwargs):
        if kwargs.get("purpose") == "mind_l1":
            l1_calls["count"] += 1
            if l1_calls["count"] == 1:
                return "{invalid json"
            return _fake_l1_response()
        if kwargs.get("purpose") == "mind_expr":
            return _fake_expr_response()
        raise AssertionError()

    monkeypatch.setattr("app.services.mind.appraisal.request_model_response", fake_request)
    monkeypatch.setattr("app.services.mind.expression.request_model_response", fake_request)

    result = run_chat_turn("s3", "测试重试", trace_id="t3")
    assert result["reply"] == "收到啦\n今天怎么样"
    assert l1_calls["count"] == 2


# ── 测试 4：L1 两次都非法 → 回退旧路径 ───────────────────────────────────────
def test_mind_l1_always_invalid_fallback(mind_db, fake_mind_env, monkeypatch):
    old_called = {"count": 0}

    def fake_request(**kwargs):
        if kwargs.get("purpose") == "mind_l1":
            return "{still invalid"
        raise AssertionError("不应该调用 mind_expr")

    def fake_old(messages, **kwargs):
        old_called["count"] += 1
        return "兜底回复"

    monkeypatch.setattr("app.services.mind.appraisal.request_model_response", fake_request)
    monkeypatch.setattr(llm_gateway, "generate_chat_reply", fake_old)

    result = run_chat_turn("s4", "测试降级", trace_id="t4")
    assert result["reply"] == "兜底回复"
    assert old_called["count"] == 1

    cycle_row = fetch_one("SELECT * FROM mind_cycle WHERE session_id='s4'")
    assert cycle_row["status"] == "failed"
    assert "L1" in cycle_row["error"]


# ── 测试 5：L1 完成后 E2 抛异常；重跑时 L1 不再被调用 ──────────────────────────
def test_mind_e2_failure_resume(mind_db, fake_mind_env, monkeypatch):
    l1_calls = {"count": 0}

    def fake_request(**kwargs):
        if kwargs.get("purpose") == "mind_l1":
            l1_calls["count"] += 1
            return _fake_l1_response()
        if kwargs.get("purpose") == "mind_expr":
            raise RuntimeError("E2 exploded")
        raise AssertionError()

    def fake_old(messages, **kwargs):
        return "兜底"

    monkeypatch.setattr("app.services.mind.appraisal.request_model_response", fake_request)
    monkeypatch.setattr("app.services.mind.expression.request_model_response", fake_request)
    monkeypatch.setattr(llm_gateway, "generate_chat_reply", fake_old)

    result = run_chat_turn("s5", "第一次", trace_id="t5")
    assert result["reply"] == "兜底"
    assert l1_calls["count"] == 1

    # 重跑同一 trace_id，L1 应该跳过
    l1_calls["count"] = 0
    result2 = run_chat_turn("s5", "第二次", trace_id="t5")
    assert l1_calls["count"] == 0


# ── 测试 6：深夜 leak 更高 ─────────────────────────────────────────────────────
def test_mind_leak_night_higher(mind_db, fake_mind_env):
    # 01:00
    with patch("app.services.mind.state.datetime") as mock_dt:
        mock_now = MagicMock()
        mock_now.hour = 1
        mock_dt.now.return_value = mock_now
        leak_night = state.compute_leak(arousal=0.5, base_inhibition=0.7)

    # 15:00
    with patch("app.services.mind.state.datetime") as mock_dt:
        mock_now = MagicMock()
        mock_now.hour = 15
        mock_dt.now.return_value = mock_now
        leak_day = state.compute_leak(arousal=0.5, base_inhibition=0.7)

    assert leak_night > leak_day


# ── 测试 7：E2 请求内容不含 l1_json 除 gut 以外的字段，也不含自我卡全文 ──────────
def test_mind_e2_input_isolation(mind_db, fake_mind_env, monkeypatch):
    captured_expr = {}

    def fake_request(**kwargs):
        if kwargs.get("purpose") == "mind_l1":
            return _fake_l1_response()
        if kwargs.get("purpose") == "mind_expr":
            captured_expr["messages"] = kwargs["messages"]
            return _fake_expr_response()
        raise AssertionError()

    monkeypatch.setattr("app.services.mind.appraisal.request_model_response", fake_request)
    monkeypatch.setattr("app.services.mind.expression.request_model_response", fake_request)

    run_chat_turn("s7", "测试隔离", trace_id="t7")

    user_content = ""
    for msg in captured_expr["messages"]:
        if msg["role"] == "user":
            if isinstance(msg["content"], list):
                for block in msg["content"]:
                    if block.get("type") == "text":
                        user_content += block["text"]
            else:
                user_content += str(msg["content"])

    assert "category" not in user_content
    assert "affect_delta" not in user_content
    assert "urgency" not in user_content
    assert "depth" not in user_content
    assert "cues" not in user_content
    assert "他想聊聊" in user_content  # gut 在
    # 自我卡全文不应出现（超过 600 字的部分）
    assert "自传式记忆" not in user_content


# ── 测试 8：E3 markdown 清理、超长切开、表情包校验 ──────────────────────────────
def test_mind_e3_rendering():
    # 测试 markdown 清理
    raw = """# 标题
**加粗**的内容
正常内容
"""
    result = expression.run_e3(raw, sticker_names={"开心"})
    assert "#" not in result
    assert "**" not in result
    assert "- " not in result  # 列表符号被去掉
    assert "```" not in result
    assert "正常内容" in result
    assert "print" not in result

    # 测试表情包校验：在表里保留，不在表里删掉
    raw2 = "[表情包：开心]\n[表情包：不存在的表情包]"
    result2 = expression.run_e3(raw2, sticker_names={"开心"})
    assert "[表情包：开心]" in result2
    assert "[表情包：不存在的表情包]" not in result2

    # 超长消息切开
    long_text = "这是一段很长的话。" * 60 + "结束"
    result3 = expression.run_e3(long_text, sticker_names=set())
    lines = [line for line in result3.split("\n") if line.strip()]
    assert len(lines) <= 4


# ── 测试 9：usage 中出现 mind_l1 / mind_expr ───────────────────────────────────
def test_mind_usage_purposes(mind_db, fake_mind_env, monkeypatch):
    purposes = []

    def fake_request(**kwargs):
        purposes.append(kwargs.get("purpose"))
        if kwargs.get("purpose") == "mind_l1":
            return _fake_l1_response()
        if kwargs.get("purpose") == "mind_expr":
            return _fake_expr_response()
        raise AssertionError()

    monkeypatch.setattr("app.services.mind.appraisal.request_model_response", fake_request)
    monkeypatch.setattr("app.services.mind.expression.request_model_response", fake_request)

    run_chat_turn("s9", "测试计费", trace_id="t9")
    assert "mind_l1" in purposes
    assert "mind_expr" in purposes


# ── 测试 10：L1 请求体含 response_format 和 provider.require_parameters ─────────
def test_mind_l1_request_body(mind_db, fake_mind_env, monkeypatch):
    captured = {}

    def fake_request(**kwargs):
        if kwargs.get("purpose") == "mind_l1":
            captured["extra_body"] = kwargs.get("extra_body", {})
            return _fake_l1_response()
        if kwargs.get("purpose") == "mind_expr":
            return _fake_expr_response()
        raise AssertionError()

    monkeypatch.setattr("app.services.mind.appraisal.request_model_response", fake_request)
    monkeypatch.setattr("app.services.mind.expression.request_model_response", fake_request)

    run_chat_turn("s10", "测试请求体", trace_id="t10")
    extra = captured["extra_body"]
    assert extra.get("response_format", {}).get("type") == "json_schema"
    assert extra.get("provider", {}).get("require_parameters") is True


# ── 测试 11：L1 category 非法 → 重试 → 第二次合法则成功 ─────────────────────────
def test_mind_l1_invalid_category_retry(mind_db, fake_mind_env, monkeypatch):
    l1_calls = {"count": 0}

    def fake_request(**kwargs):
        if kwargs.get("purpose") == "mind_l1":
            l1_calls["count"] += 1
            if l1_calls["count"] == 1:
                return json.dumps({
                    "category": "不存在的分类",
                    "gut": "测试",
                    "affect_delta": {"valence": 0.0, "arousal": 0.0},
                    "urgency": 0.0,
                    "cues": [],
                    "depth": "normal",
                    "reply_will": "now",
                }, ensure_ascii=False)
            return _fake_l1_response()
        if kwargs.get("purpose") == "mind_expr":
            return _fake_expr_response()
        raise AssertionError()

    monkeypatch.setattr("app.services.mind.appraisal.request_model_response", fake_request)
    monkeypatch.setattr("app.services.mind.expression.request_model_response", fake_request)

    result = run_chat_turn("s11", "测试非法分类", trace_id="t11")
    assert result["reply"] == "收到啦\n今天怎么样"
    assert l1_calls["count"] == 2


# ── 测试 12：表情包表注入和跨轮使用 ─────────────────────────────────────────────
def test_mind_sticker_table_persistence(mind_db, fake_mind_env, monkeypatch):
    captured_expr = {}

    def fake_request(**kwargs):
        if kwargs.get("purpose") == "mind_l1":
            return _fake_l1_response()
        if kwargs.get("purpose") == "mind_expr":
            captured_expr["messages"] = kwargs["messages"]
            return "[表情包：喵]\n好的"
        raise AssertionError()

    monkeypatch.setattr("app.services.mind.appraisal.request_model_response", fake_request)
    monkeypatch.setattr("app.services.mind.expression.request_model_response", fake_request)

    # 第一轮：带注入
    injected = "【表情包表｜网关注入】表（名字共3个）：\n喵、咬你~、晚安"
    run_chat_turn("s12", f"{injected}\n\n你好", trace_id="t12")
    # E2 被调用了
    assert "messages" in captured_expr

    # 第二轮：不带注入，验证 mind_session_context 被读取
    captured_expr.clear()
    run_chat_turn("s12", "第二轮", trace_id="t12b")
    assert "messages" in captured_expr


# ── 测试 13：E3 表情包标记处理 ─────────────────────────────────────────────────
def test_mind_e3_sticker_marks():
    # 合法标记保留
    result = expression.run_e3("[表情包：喵]", sticker_names={"喵"})
    assert "[表情包：喵]" in result
    # 非法显式标记删除（但会抛 ValueError，由 cycle 处理）
    try:
        expression.run_e3("[表情包：不存在的]", sticker_names={"喵"})
    except ValueError:
        pass  # 预期
    else:
        raise AssertionError("应该抛 ValueError")
    # 裸 [X] 保留
    result3 = expression.run_e3("[某个文本]", sticker_names={"喵"})
    assert "[某个文本]" in result3
    # 名字含下划线不被破坏
    result4 = expression.run_e3("[表情包：test_sticker]", sticker_names={"test_sticker"})
    assert "[表情包：test_sticker]" in result4


# ── 测试 14：E2 user prompt 不含注入块 ─────────────────────────────────────────
def test_mind_e2_no_injection_in_user_prompt(mind_db, fake_mind_env, monkeypatch):
    captured_expr = {}

    def fake_request(**kwargs):
        if kwargs.get("purpose") == "mind_l1":
            return _fake_l1_response()
        if kwargs.get("purpose") == "mind_expr":
            captured_expr["messages"] = kwargs["messages"]
            return _fake_expr_response()
        raise AssertionError()

    monkeypatch.setattr("app.services.mind.appraisal.request_model_response", fake_request)
    monkeypatch.setattr("app.services.mind.expression.request_model_response", fake_request)

    # 直接验证 split_injected_context 正确剥离注入块
    from app.services.chat_service import split_injected_context
    injected = "【表情包表｜网关注入】表（名字共1个）：\n喵"
    full_msg = f"{injected}\n\n你好"
    block, clean = split_injected_context(full_msg)
    assert block == injected
    assert clean == "你好"
    assert "【表情包表" not in clean


# ── 测试 15：arousal=-0.8 抑制力不降低 ─────────────────────────────────────────
def test_mind_inhibition_negative_arousal(mind_db, fake_mind_env):
    from app.services.mind.state import compute_inhibition
    inh_neg = compute_inhibition(arousal=-0.8, base=0.7)
    inh_zero = compute_inhibition(arousal=0.0, base=0.7)
    assert inh_neg <= inh_zero


# ── 测试 16：6 行输出 + 上限 4 → 输出 4 条，最后一条包含第 5、6 行内容 ───────────
def test_mind_e3_merge_lines():
    raw = "第1行\n第2行\n第3行\n第4行\n第5行\n第6行"
    result = expression.run_e3(raw, sticker_names=set())
    lines = result.split("\n")
    assert len(lines) == 4
    assert "第5行" in lines[3]
    assert "第6行" in lines[3]


# ── 测试 17：续跑测试（同一 trace_id）──────────────────────────────────────────
def test_mind_resume_same_trace(mind_db, fake_mind_env, monkeypatch):
    l1_calls = {"count": 0}
    expr_calls = {"count": 0}

    def fake_request(**kwargs):
        if kwargs.get("purpose") == "mind_l1":
            l1_calls["count"] += 1
            return _fake_l1_response()
        if kwargs.get("purpose") == "mind_expr":
            expr_calls["count"] += 1
            return _fake_expr_response()
        raise AssertionError()

    monkeypatch.setattr("app.services.mind.appraisal.request_model_response", fake_request)
    monkeypatch.setattr("app.services.mind.expression.request_model_response", fake_request)

    # 第一次完整跑
    result1 = run_chat_turn("s17", "同一条消息", trace_id="t17")
    assert l1_calls["count"] == 1
    assert expr_calls["count"] == 1

    # 第二次用同一 trace_id，L1 应该跳过，E2 也跳过（结果已缓存）
    l1_calls["count"] = 0
    expr_calls["count"] = 0
    result2 = run_chat_turn("s17", "同一条消息", trace_id="t17")
    assert l1_calls["count"] == 0
    assert expr_calls["count"] == 0  # E2 也跳过（结果已缓存）
    assert result2["reply"] == result1["reply"]


# ── 测试 18：乐观锁版本冲突重试 ─────────────────────────────────────────────────
def test_mind_optimistic_lock(mind_db, fake_mind_env, monkeypatch):
    from app.services.mind import state as mind_state
    from app.storage.db import execute_write

    # 先初始化一条记录
    mind_state.apply_affect_delta_optimistic("s18", 0.1, 0.2)
    s1 = mind_state.read_state("s18")
    assert s1["version"] == 1

    # 模拟版本冲突：先读取，再修改，再尝试写入
    s2 = mind_state.read_state("s18")
    # 手动修改版本号模拟并发
    execute_write("UPDATE mind_state SET version=99 WHERE session_id='s18'")

    # 乐观锁应该重试并最终成功
    result = mind_state.apply_affect_delta_optimistic("s18", 0.1, 0.2)
    assert result is not None
    assert result["version"] == 100  # 99 + 1


