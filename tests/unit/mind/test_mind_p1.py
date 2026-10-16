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
    result = expression.run_e3(raw, sticker_table={"开心": "desc"})
    assert "#" not in result
    assert "**" not in result
    assert "- " not in result  # 列表符号被去掉
    assert "```" not in result
    assert "正常内容" in result
    assert "print" not in result

    # 测试表情包校验：在表里保留，不在表里删掉
    raw2 = "[表情包：开心]\n[表情包：不存在的表情包]"
    result2 = expression.run_e3(raw2, sticker_table={"开心": "desc"})
    assert "[表情包：开心]" in result2
    assert "[表情包：不存在的表情包]" not in result2

    # 超长消息切开
    long_text = "这是一段很长的话。" * 60 + "结束"
    result3 = expression.run_e3(long_text, sticker_table={})
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
