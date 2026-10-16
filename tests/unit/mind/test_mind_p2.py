from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from app import config
from app.services.chat import llm_gateway
from app.services.chat_service import run_chat_turn
from app.services.mind import consistency, cycle
from app.services.mind.deliberation import BeliefUpdate, Intent, L2Result
from app.services.mind.memory import commit as memory_commit
from app.services.mind.memory import store as memory_store
from app.services.mind.memory import tokenize, wm_builder
from app.services.mind.memory.retrieval import retrieve_episodes
from app.storage.db import fetch_one


@pytest.fixture()
def mind_db(isolated_db: Path):
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
    monkeypatch.setattr(config, "MIND_E3_MAX_MESSAGES", 4)
    monkeypatch.setattr(config, "MIND_E3_MAX_SINGLE_CHARS", 200)
    monkeypatch.setattr(config, "MIND_THINK_MODEL", "fake-think")
    monkeypatch.setattr(config, "MIND_THINK_EXTRA_BODY", {"reasoning": {"effort": "medium"}})
    monkeypatch.setattr(config, "MIND_THINK_MAX_TOKENS", 4000)
    monkeypatch.setattr(config, "MIND_FAST_SKIP_L2", True)
    monkeypatch.setattr(config, "MIND_WM_MAX_CHARS", 6000)
    monkeypatch.setattr(config, "MIND_WM_EPISODES", 5)
    monkeypatch.setattr(config, "MIND_WM_BELIEFS", 5)
    monkeypatch.setattr(config, "MIND_RECENCY_HALF_LIFE_DAYS", 7.0)
    monkeypatch.setattr(config, "MIND_MEMORY_HALF_LIFE_DAYS", 14.0)
    monkeypatch.setattr(
        config, "MIND_RETRIEVAL_WEIGHTS",
        {"rel": 1, "rec": 1, "imp": 1, "emo": 1, "str": 1},
    )
    monkeypatch.setattr(config, "MIND_MEMORY_SCOPE", "session")


def _l1_payload(depth: str = "normal", gut: str = "他想聊聊", cues=None):
    return json.dumps({
        "category": "分享",
        "gut": gut,
        "affect_delta": {"valence": 0.1, "arousal": 0.2},
        "urgency": 0.3,
        "cues": cues if cues is not None else ["测试"],
        "depth": depth,
        "reply_will": "now",
    }, ensure_ascii=False)


def _l2_payload(*, say_points=None, hidden=None, intents=None, memory_notes=None,
                belief_updates=None, state_delta=None, sticker=None, length="short",
                n_messages=1, leak=0.2, interpretation="普通解读", feeling="平静"):
    return json.dumps({
        "l2": {
            "interpretation": interpretation,
            "feeling": feeling,
            "decision": {"reply": True, "world_action": False},
            "intents": intents or [],
            "state_delta": state_delta or {"valence": 0.0, "arousal": 0.0},
            "memory_notes": memory_notes or [],
            "belief_updates": belief_updates or [],
        },
        "l3": {
            "say_points": say_points if say_points is not None else ["回应"],
            "hidden": hidden or [],
            "leak": leak,
            "tone": "平静",
            "length": length,
            "n_messages": n_messages,
            "hesitate": False,
            "sticker": sticker,
        },
    }, ensure_ascii=False)


def _patch_providers(monkeypatch, *, l1=None, think=None, expr=None, recorder=None):
    """统一假 provider：按 purpose 分发；recorder 收集 (purpose, kwargs)。"""
    def fake_request(**kwargs):
        purpose = kwargs.get("purpose", "")
        if recorder is not None:
            recorder.append((purpose, kwargs))
        if purpose == "mind_l1":
            if callable(l1):
                return l1(kwargs)
            return l1 if l1 is not None else _l1_payload()
        if purpose == "mind_think":
            if callable(think):
                return think(kwargs)
            return think if think is not None else _l2_payload()
        if purpose == "mind_expr":
            if callable(expr):
                return expr(kwargs)
            return expr if expr is not None else "好的"
        raise AssertionError(f"unexpected purpose: {purpose}")

    monkeypatch.setattr("app.services.mind.appraisal.request_model_response", fake_request)
    monkeypatch.setattr("app.services.mind.deliberation.request_model_response", fake_request)
    monkeypatch.setattr("app.services.mind.expression.request_model_response", fake_request)
    return fake_request


# ── 测试 1：fast 路径跳过 L2 ───────────────────────────────────────────────────
def test_mind_p2_fast_skips_l2(mind_db, fake_mind_env, monkeypatch):
    calls = []

    def think(kwargs):
        raise AssertionError("L2 不应被调用")

    _patch_providers(
        monkeypatch, l1=_l1_payload(depth="fast"), think=think, expr="好的", recorder=calls,
    )
    result = run_chat_turn("p2-fast", "你好", trace_id="tp2fast")
    assert result["reply"] == "好的"
    purposes = [purpose for purpose, _ in calls]
    assert "mind_think" not in purposes
    assert purposes.count("mind_l1") == 1
    row = fetch_one("SELECT * FROM mind_cycle WHERE session_id='p2-fast'")
    assert row["status"] == "completed"
    assert row["l2_json"] is None
    assert row["wm_json"] is None


# ── 测试 2：normal 路径，L1/think/expr 各 1 次，请求体正确 ──────────────────────
def test_mind_p2_normal_path(mind_db, fake_mind_env, monkeypatch):
    calls = []
    _patch_providers(
        monkeypatch,
        l1=_l1_payload(depth="normal"),
        think=_l2_payload(say_points=["回应一下"]),
        expr="收到啦",
        recorder=calls,
    )
    result = run_chat_turn("p2-normal", "今天怎么样", trace_id="tp2normal")
    assert result["reply"] == "收到啦"
    purposes = [purpose for purpose, _ in calls]
    assert purposes == ["mind_l1", "mind_think", "mind_expr"]

    think_kwargs = next(kwargs for purpose, kwargs in calls if purpose == "mind_think")
    assert think_kwargs["model_name"] == "fake-think"
    assert think_kwargs["max_tokens"] == 4000
    extra = think_kwargs["extra_body"]
    assert extra.get("response_format", {}).get("type") == "json_schema"
    assert extra.get("provider", {}).get("require_parameters") is True
    assert extra.get("reasoning") == {"effort": "medium"}

    row = fetch_one("SELECT * FROM mind_cycle WHERE session_id='p2-normal'")
    assert row["status"] == "completed"
    assert row["l2_json"] is not None
    assert row["wm_json"] is not None
    plan = json.loads(row["plan_json"])
    assert plan["say_points"] == ["回应一下"]
    assert json.loads(row["l2_json"])["l3"]["say_points"] == ["回应一下"]


# ── 测试 3：E2 隔离（L2 私有字段与工作记忆经历/认识不进 E2）─────────────────────
def test_mind_p2_e2_isolation(mind_db, fake_mind_env, monkeypatch):
    memory_store.insert_episode(
        "p2-isolation", "old-cycle", 0,
        summary="EP_SUMMARY_MARK 的事情", participants=["user"], importance=0.9,
    )
    memory_store.upsert_belief(
        "p2-isolation", "BEL_SUBJECT_MARK", "BEL_CONTENT_MARK", 0.9, [],
    )
    calls = []
    _patch_providers(
        monkeypatch,
        l1=_l1_payload(depth="normal", cues=["EP_SUMMARY_MARK"]),
        think=_l2_payload(
            interpretation="INT_MARK 解读",
            feeling="FEE_MARK 感受",
            intents=[{"action": "INTENT_MARK 提议", "target": None, "note": ""}],
            memory_notes=[{"summary": "NOTE_MARK", "participants": ["user"], "tags": []}],
            belief_updates=[
                {"subject": "BEL_UPDATE_SUBJECT", "content": "BEL_UPDATE_MARK",
                 "confidence": 0.5, "evidence": []},
            ],
            say_points=["SAY_MARK 回应"],
        ),
        expr="好",
        recorder=calls,
    )
    run_chat_turn("p2-isolation", "聊聊 EP_SUMMARY_MARK", trace_id="tp2iso")

    expr_kwargs = next(kwargs for purpose, kwargs in calls if purpose == "mind_expr")
    texts = []
    for message in expr_kwargs["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            texts.extend(block.get("text", "") for block in content)
        else:
            texts.append(str(content))
    joined = "\n".join(texts)
    for marker in ("INT_MARK", "FEE_MARK", "INTENT_MARK", "NOTE_MARK",
                   "BEL_UPDATE_MARK", "BEL_CONTENT_MARK", "EP_SUMMARY_MARK"):
        assert marker not in joined, f"E2 不应看到 {marker}"
    assert "SAY_MARK" in joined  # say_points 可见


# ── 测试 4：凌晨 1 点说「早安」→ 打回一次；第二次通过 ──────────────────────────
def test_mind_p2_time_line_retry(mind_db, fake_mind_env, monkeypatch):
    monkeypatch.setattr(consistency, "_local_hour", lambda: 1)
    think_calls = {"count": 0}

    def think(kwargs):
        think_calls["count"] += 1
        if think_calls["count"] == 1:
            return _l2_payload(say_points=["早安"], hidden=[])
        joined = "\n".join(
            str(block.get("text", "")) if isinstance(message.get("content"), list)
            else str(message.get("content"))
            for message in kwargs["messages"]
            for block in ([message["content"]] if not isinstance(message.get("content"), list)
                          else message["content"])
        )
        assert "时间线" in joined, "重试请求要带上检查错误"
        return _l2_payload(say_points=["这么晚还没睡?"], hidden=[])

    _patch_providers(monkeypatch, l1=_l1_payload(depth="normal"), think=think, expr="好的")
    result = run_chat_turn("p2-time", "睡了吗", trace_id="tp2time")
    assert result["reply"] == "好的"
    assert think_calls["count"] == 2
    row = fetch_one("SELECT * FROM mind_cycle WHERE session_id='p2-time'")
    assert row["status"] == "completed"
    assert row["degraded"] is None
    plan = json.loads(row["plan_json"])
    assert plan["say_points"] == ["这么晚还没睡?"]
    checks = json.loads(row["check_json"])
    assert checks[0]["ok"] is False
    assert checks[1]["ok"] is True


# ── 测试 5：一致性连续失败 → 保守路径，check_json 含两次错误 ────────────────────
def test_mind_p2_double_check_fail_conservative(mind_db, fake_mind_env, monkeypatch):
    monkeypatch.setattr(consistency, "_local_hour", lambda: 1)
    calls = []
    _patch_providers(
        monkeypatch,
        l1=_l1_payload(depth="normal"),
        think=_l2_payload(say_points=["早安"], hidden=[]),
        expr="保守回复",
        recorder=calls,
    )
    old_called = {"count": 0}

    def fake_old(messages, **kwargs):
        old_called["count"] += 1
        return "不该走到这里"

    monkeypatch.setattr(llm_gateway, "generate_chat_reply", fake_old)
    result = run_chat_turn("p2-check", "早上好", trace_id="tp2check")
    # 保守路径 = L1 + E2，不调旧路径
    assert result["reply"] == "保守回复"
    assert old_called["count"] == 0
    assert [p for p, _ in calls].count("mind_think") == 2
    row = fetch_one("SELECT * FROM mind_cycle WHERE session_id='p2-check'")
    assert row["status"] == "completed"
    assert row["degraded"] == "L2"
    assert row["l2_json"] is None
    checks = json.loads(row["check_json"])
    assert len(checks) == 2 and all(item["ok"] is False for item in checks)
    assert all(item["errors"] for item in checks)


# ── 测试 6：hidden 原文出现在 say_points 里 → 判失败 ───────────────────────────
def test_mind_p2_hidden_in_say_points_fails():
    result = L2Result(say_points=["其实有点吃醋"], hidden=["有点吃醋"])
    ok, errors, _ = consistency.check_result("p2-h", result)
    assert ok is False
    assert any("隐藏与表达隔离" in item for item in errors)


# ── 测试 7：intents 是完成态 / 主语不是 Neno → 判失败 ──────────────────────────
def test_mind_p2_intent_agency_fails():
    finished = L2Result(intents=[Intent(action="已经去过公园", note="")])
    ok, errors, _ = consistency.check_result("p2-i", finished)
    assert ok is False and any("完成态" in item for item in errors)

    foreign = L2Result(intents=[Intent(action="用户要出门", note="")])
    ok2, errors2, _ = consistency.check_result("p2-i", foreign)
    assert ok2 is False and any("主语" in item for item in errors2)

    proposal = L2Result(intents=[Intent(action="提议明天去公园", note="")])
    ok3, errors3, _ = consistency.check_result("p2-i", proposal)
    assert ok3 is True and not errors3


# ── 测试 8：L2 连续两次非法 JSON → 降级 L1+E2，degraded="L2" ───────────────────
def test_mind_p2_l2_invalid_twice_degraded(mind_db, fake_mind_env, monkeypatch):
    calls = []
    _patch_providers(
        monkeypatch,
        l1=_l1_payload(depth="normal"),
        think="{still invalid",
        expr="降级回复",
        recorder=calls,
    )
    result = run_chat_turn("p2-degrade", "测试", trace_id="tp2degrade")
    assert result["reply"] == "降级回复"
    assert [p for p, _ in calls].count("mind_think") == 2
    row = fetch_one("SELECT * FROM mind_cycle WHERE session_id='p2-degrade'")
    assert row["status"] == "completed"
    assert row["degraded"] == "L2"
    assert row["l2_json"] is None
    assert row["memory_committed"] == 0
    assert int(row["l2_attempts"]) == 2


# ── 测试 9：分词与检索（两个字的线索能命中）────────────────────────────────────
def test_mind_p2_tokenize_and_search(mind_db, fake_mind_env):
    assert tokenize.to_index_text("吃饭了") == "吃饭 饭了"
    assert tokenize.to_index_text("Hello 123 世界") == "hello 123 世界"
    assert tokenize.to_query_text("吃饭") == '"吃饭"'

    memory_store.insert_episode(
        "p2-tok", "c1", 0, summary="今天和小雨吃饭了", participants=["user"],
        importance=0.5, strength=1.0,
    )
    hits = memory_store.search_episodes("p2-tok", tokenize.to_query_text("吃饭"), limit=10)
    assert any("吃饭" in item["summary"] for item in hits)

    selected = retrieve_episodes("p2-tok", ["吃饭"], "吃了什么", apply_recall_boost=False)
    assert selected and selected[0]["summary"] == "今天和小雨吃饭了"


# ── 测试 10：检索打分（重要性/新近）+ 回忆增强 + 回放不增强 ─────────────────────
def test_mind_p2_retrieval_scoring_and_recall_boost(mind_db, fake_mind_env):
    now = time.time()
    memory_store.insert_episode("p2-rank", "c-old", 0, summary="周末去看电影",
                                importance=0.5, strength=1.0, ts=now - 20 * 86400)
    old_id = fetch_one(
        "SELECT id FROM mind_episode WHERE session_id='p2-rank' AND cycle_id='c-old'"
    )["id"]
    memory_store.insert_episode("p2-rank", "c-new", 0, summary="周末去看电影",
                                importance=0.5, strength=0.5, ts=now)
    new_id = fetch_one(
        "SELECT id FROM mind_episode WHERE session_id='p2-rank' AND cycle_id='c-new'"
    )["id"]
    selected = retrieve_episodes("p2-rank", ["电影"], "看电影", limit=2,
                                 apply_recall_boost=False)
    assert selected[0]["id"] == new_id

    memory_store.insert_episode("p2-rank2", "c-low", 0, summary="项目上线",
                                importance=0.2, strength=1.0, ts=now)
    memory_store.insert_episode("p2-rank2", "c-high", 0, summary="项目上线",
                                importance=0.9, strength=1.0, ts=now)
    high_id = fetch_one(
        "SELECT id FROM mind_episode WHERE session_id='p2-rank2' AND cycle_id='c-high'"
    )["id"]
    selected2 = retrieve_episodes("p2-rank2", ["项目"], "上线吗", limit=1,
                                  apply_recall_boost=False)
    assert selected2[0]["id"] == high_id

    boosted = retrieve_episodes("p2-rank", ["电影"], "看电影", limit=1)
    assert boosted[0]["id"] == new_id
    stored = memory_store.get_episode("p2-rank", new_id)
    assert stored["recall_count"] == 1
    assert stored["strength"] > 0.5
    assert stored["last_recalled_at"] is not None

    retrieve_episodes("p2-rank", ["电影"], "看电影", limit=1, apply_recall_boost=False)
    stored2 = memory_store.get_episode("p2-rank", new_id)
    assert stored2["recall_count"] == 1

    untouched = memory_store.get_episode("p2-rank", old_id)
    assert untouched["recall_count"] == 0


# ── 测试 11：工作记忆超预算 → 经历 → 认识 → 原话 顺序裁剪；自我卡保留 ────────────
def test_mind_p2_wm_budget_trimming(mind_db, fake_mind_env, monkeypatch):
    monkeypatch.setattr(config, "MIND_WM_MAX_CHARS", 700)
    for index in range(5):
        memory_store.insert_episode(
            "p2-wm", f"c{index}", 0,
            summary=f"经历编号{index}" + "很长的描述文字" * 8,
            importance=0.5 - index * 0.05, strength=1.0,
        )
    for index in range(5):
        memory_store.upsert_belief(
            "p2-wm", f"认识{index}", "内容" + "细节文字" * 8, 0.9 - index * 0.1, [],
        )
    history = []
    for index in range(10):
        history.append({"role": "user", "content": f"原话{index}" + "补充" * 8})
        history.append({"role": "assistant", "content": f"回答{index}" + "补充" * 8})

    class FakeL1:
        cues: list[str] = []

    wm = wm_builder.build_working_memory(
        "p2-wm", l1=FakeL1(), message="你好", history=history,
        self_card="SELF_CARD_MARK 完整自我卡内容",
    )
    text = wm["text"]
    assert "SELF_CARD_MARK" in text  # 自我卡完整保留
    assert wm["trimmed"], "预算内不可能装下全部，必须发生裁剪"
    assert wm["trimmed"][0] == "episodes", f"先裁经历，实际 {wm['trimmed']}"
    assert 2 <= len(wm["episodes"]) < 5
    kept = [item["summary"] for item in wm["episodes"]]
    assert any(summary.startswith("经历编号0") for summary in kept)


# ── 测试 12：提交记忆幂等（同一 cycle 提交两次 episode 数量不变）────────────────
def test_mind_p2_commit_idempotent(mind_db, fake_mind_env):
    result = L2Result.from_dict(
        json.loads(_l2_payload(memory_notes=[
            {"summary": "第一件事", "participants": ["user"], "tags": ["日常"]},
            {"summary": "第二件事", "participants": [], "tags": []},
        ])),
        leak_cap=1.0,
        max_messages=4,
    )
    memory_commit.commit_memory(
        session_id="p2-commit", cycle_id="cycle-x", result=result, trace_id="t",
    )
    memory_commit.commit_memory(
        session_id="p2-commit", cycle_id="cycle-x", result=result, trace_id="t",
    )
    assert memory_store.count_episodes("p2-commit") == 2
    hits = memory_store.search_episodes(
        "p2-commit", tokenize.to_query_text("第一件事"), limit=10,
    )
    assert len([item for item in hits if item["summary"] == "第一件事"]) == 1


# ── 测试 13：认识更新 confidence ≤ 0.3；高可信度认识被无证据修改 → 拦下 ─────────
def test_mind_p2_belief_confidence_and_evidence(mind_db, fake_mind_env):
    memory_store.upsert_belief("p2-belief", "小雨", "同组同学", 0.5, [])
    updated = memory_store.upsert_belief("p2-belief", "小雨", "同组同学，经常一起吃饭", 1.0, [])
    assert updated["confidence"] == pytest.approx(0.8)
    assert updated["version"] == 2

    missing = L2Result(belief_updates=[
        BeliefUpdate(subject="小雨", content="变了", confidence=0.5, evidence=[99999]),
    ])
    ok, errors, _ = consistency.check_result("p2-belief", missing)
    assert ok is False and any("认识冲突" in item for item in errors)

    empty_evidence = L2Result(belief_updates=[
        BeliefUpdate(subject="小雨", content="变了", confidence=0.5, evidence=[]),
    ])
    ok2, errors2, _ = consistency.check_result("p2-belief", empty_evidence)
    assert ok2 is False and any("缺少 evidence" in item for item in errors2)

    episode_id = memory_store.insert_episode("p2-belief", "c-ev", 0, summary="一起吃饭")
    with_evidence = L2Result(belief_updates=[
        BeliefUpdate(subject="小雨", content="经常一起吃饭", confidence=0.6,
                     evidence=[episode_id]),
    ])
    ok3, errors3, _ = consistency.check_result("p2-belief", with_evidence)
    assert ok3 is True and not errors3


# ── 测试 14：验收情境 2（吃醋；工作记忆带入同组同学经历；E2 生成回复）──────────
def test_mind_p2_scenario_jealousy(mind_db, fake_mind_env, monkeypatch):
    memory_store.insert_episode(
        "p2-jealous", "c-past", 0,
        summary="小雨是同组同学，我们经常一起做作业", participants=["user", "小雨"],
        importance=0.9, strength=1.0, valence=0.3, arousal=0.2,
    )
    calls = []
    _patch_providers(
        monkeypatch,
        l1=_l1_payload(depth="normal", gut="吃醋", cues=["小雨", "吃饭"]),
        think=_l2_payload(
            interpretation="他在说和小雨吃饭",
            feeling="有点吃醋但想起小雨是同组同学",
            say_points=["哦", "就你俩啊"],
            hidden=["其实有点在意"],
            length="short",
            n_messages=2,
        ),
        expr="哦\n就你俩啊",
        recorder=calls,
    )

    def fake_old(messages, **kwargs):
        raise AssertionError("不应落到旧路径")

    monkeypatch.setattr(llm_gateway, "generate_chat_reply", fake_old)
    result = run_chat_turn("p2-jealous", "今天和小雨吃饭了", trace_id="tp2j")
    assert result["reply"] == "哦\n就你俩啊"
    think_kwargs = next(kwargs for purpose, kwargs in calls if purpose == "mind_think")
    wm_text = think_kwargs["messages"][1]["content"]
    assert "小雨是同组同学" in wm_text, "工作记忆要把相关经历带进 L2"
    row = fetch_one("SELECT * FROM mind_cycle WHERE session_id='p2-jealous'")
    assert row["status"] == "completed"
    l2 = json.loads(row["l2_json"])
    assert l2["l2"]["feeling"].startswith("有点吃醋")
    assert l2["l3"]["say_points"] == ["哦", "就你俩啊"]
    assert row["memory_committed"] == 1


# ── 测试 15：验收情境 6（删掉经历后，同一句话的工作记忆不同）───────────────────
def test_mind_p2_scenario_memory_changes_wm(mind_db, fake_mind_env, monkeypatch):
    episode_id = memory_store.insert_episode(
        "p2-sc6", "c-past", 0, summary="小雨是同组同学", participants=["小雨"],
        importance=0.9, strength=1.0,
    )
    _patch_providers(
        monkeypatch,
        l1=_l1_payload(depth="normal", cues=["小雨"]),
        think=_l2_payload(say_points=["嗯"]),
        expr="嗯",
    )
    run_chat_turn("p2-sc6", "今天和小雨吃饭了", trace_id="tp2sc6a")
    wm1 = json.loads(fetch_one(
        "SELECT wm_json FROM mind_cycle WHERE trigger_ref='tp2sc6a'"
    )["wm_json"])
    assert "小雨是同组同学" in wm1["text"]

    memory_store.delete_episode(episode_id)
    run_chat_turn("p2-sc6", "今天和小雨吃饭了", trace_id="tp2sc6b")
    wm2 = json.loads(fetch_one(
        "SELECT wm_json FROM mind_cycle WHERE trigger_ref='tp2sc6b'"
    )["wm_json"])
    assert "小雨是同组同学" not in wm2["text"]
    assert wm1["text"] != wm2["text"]


# ── 测试 16：老库升级（旧 mind_cycle 建表后 init_db 补列）──────────────────────
def test_mind_p2_old_db_upgrade(tmp_path, monkeypatch):
    import app.storage.db as db_storage
    import app.storage.operations as ops_storage

    data_dir = tmp_path / "olddata"
    data_dir.mkdir()
    db_file = data_dir / "bot.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        """CREATE TABLE mind_cycle (
             cycle_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
             trigger_type TEXT NOT NULL DEFAULT 'message', trigger_ref TEXT,
             status TEXT NOT NULL DEFAULT 'running', l1_json TEXT, plan_json TEXT,
             expr_text TEXT, final_text TEXT, models_json TEXT DEFAULT '{}',
             error TEXT, created_at REAL NOT NULL DEFAULT 0.0,
             updated_at REAL NOT NULL DEFAULT 0.0)"""
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db_storage, "DB_DIR", data_dir)
    monkeypatch.setattr(db_storage, "DB_PATH", db_file)
    monkeypatch.setenv("NENO_OPERATIONS_DB", str(data_dir / "operations.db"))
    monkeypatch.setattr(ops_storage, "_instance", None)
    db_storage.init_db()

    columns = {row["name"] for row in db_storage.fetch_all("PRAGMA table_info(mind_cycle)")}
    assert {"l2_json", "wm_json", "check_json", "l2_attempts",
            "memory_committed", "degraded"} <= columns
    tables = {
        row["name"] for row in db_storage.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"mind_episode", "mind_belief"} <= tables


# ── 追加：续跑时 L2 已存不再调用；wm_json 复用；重放不重复提交 ─────────────────
def test_mind_p2_resume_skips_l2(mind_db, fake_mind_env, monkeypatch):
    calls = []
    _patch_providers(
        monkeypatch,
        l1=_l1_payload(depth="normal"),
        think=_l2_payload(say_points=["嗯嗯"]),
        expr="嗯嗯",
        recorder=calls,
    )
    messages = [{"role": "user", "content": "同一条消息"}]
    result1 = cycle.run_cycle(
        "p2-resume", "同一条消息", [], messages,
        trace_id="tp2resume", injected_context="",
    )
    assert result1 == "嗯嗯"
    assert [p for p, _ in calls].count("mind_think") == 1
    wm1 = fetch_one("SELECT wm_json FROM mind_cycle WHERE session_id='p2-resume'")["wm_json"]

    calls.clear()
    result2 = cycle.run_cycle(
        "p2-resume", "同一条消息", [], messages,
        trace_id="tp2resume", injected_context="",
    )
    assert result2 == "嗯嗯"
    assert [p for p, _ in calls].count("mind_think") == 0
    wm2 = fetch_one("SELECT wm_json FROM mind_cycle WHERE session_id='p2-resume'")["wm_json"]
    assert wm1 == wm2

    row = fetch_one("SELECT * FROM mind_cycle WHERE session_id='p2-resume'")
    assert row["status"] == "completed"
    assert row["memory_committed"] == 1


# ── 追加：重要性规则 + 续跑回放不重复做回忆增强 ────────────────────────────────
def test_mind_p2_importance_rules(mind_db, fake_mind_env):
    from app.services.mind.memory.importance import score_importance

    # 与用户有关 + 第一次 + 承诺 + 情绪强烈 = 1.0（截断）
    full = score_importance(
        "p2-imp", {"summary": "答应用户明天见面", "participants": ["user"]},
        valence=0.5, arousal=0.3,
    )
    assert full == pytest.approx(1.0)
    # 基础分（与用户无关、非首次时降档）
    base = score_importance(
        "p2-imp", {"summary": "天气不错", "participants": []}, valence=0.0, arousal=0.0,
    )
    assert base == pytest.approx(0.4)  # 0.2 基础 + 0.2 第一次


def test_mind_p2_wm_replay_no_recall_boost(mind_db, fake_mind_env, monkeypatch):
    episode_id = memory_store.insert_episode(
        "p2-replay", "c1", 0, summary="一起看电影", participants=["user"],
        importance=0.5, strength=0.5, ts=time.time(),
    )
    calls = []
    _patch_providers(
        monkeypatch,
        l1=_l1_payload(depth="normal", cues=["电影"]),
        think=_l2_payload(say_points=["好"]),
        expr="好",
        recorder=calls,
    )
    messages = [{"role": "user", "content": "看电影吗"}]
    cycle.run_cycle("p2-replay", "看电影吗", [], messages,
                    trace_id="tp2replay", injected_context="")
    after_first = memory_store.get_episode("p2-replay", episode_id)
    assert after_first["recall_count"] == 1
    assert after_first["strength"] > 0.5

    # 回放同一 cycle：wm_json 复用，不再检索也不再增强
    cycle.run_cycle("p2-replay", "看电影吗", [], messages,
                    trace_id="tp2replay", injected_context="")
    after_replay = memory_store.get_episode("p2-replay", episode_id)
    assert after_replay["recall_count"] == 1
    assert after_replay["strength"] == after_first["strength"]
