"""Offline P2 mind checks: temporary DB + fake providers + synthetic data."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["CONTEXT_SUMMARY_ENABLED"] = "true"
os.environ["WORLD_ENABLED"] = "false"


def _l1_payload(depth="normal", gut="他想聊聊", cues=None):
    return json.dumps({
        "category": "分享",
        "gut": gut,
        "affect_delta": {"valence": 0.1, "arousal": 0.2},
        "urgency": 0.3,
        "cues": cues if cues is not None else ["测试"],
        "depth": depth,
        "reply_will": "now",
    }, ensure_ascii=False)


def _l2_payload(*, say_points=None, hidden=None, memory_notes=None,
                belief_updates=None, state_delta=None, sticker=None,
                length="short", n_messages=1, leak=0.2, intents=None):
    return json.dumps({
        "l2": {
            "interpretation": "普通解读",
            "feeling": "平静",
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


def run():
    checks = []
    with tempfile.TemporaryDirectory(prefix="neno-mind-p2-") as temp:
        base = Path(temp)
        from app.storage import db, operations as ops
        from app.services import chat_service
        from app import config
        from app.services.mind import consistency, cycle, state
        from app.services.mind.memory import store as memory_store
        from app.services.mind.memory import tokenize, wm_builder
        from app.services.mind.memory.retrieval import retrieve_episodes

        db.DB_DIR = base / "data"
        db.DB_DIR.mkdir()
        db.DB_PATH = db.DB_DIR / "bot.db"
        ops._instance = ops.Operations(db.DB_DIR / "operations.db")
        db.init_db()

        config.MIND_ENABLED = True
        config.MIND_L1_MODEL = "fake-l1"
        config.MIND_EXPR_MODEL = "fake-expr"
        config.MIND_THINK_MODEL = "fake-think"
        config.MIND_THINK_EXTRA_BODY = {"reasoning": {"effort": "medium"}}
        config.MIND_THINK_MAX_TOKENS = 8000
        config.MIND_FAST_SKIP_L2 = True
        config.MIND_WM_MAX_CHARS = 6000
        config.MIND_WM_EPISODES = 5
        config.MIND_WM_BELIEFS = 5
        config.MIND_RECENCY_HALF_LIFE_DAYS = 7.0
        config.MIND_MEMORY_HALF_LIFE_DAYS = 14.0
        config.MIND_RETRIEVAL_WEIGHTS = {"rel": 1, "rec": 1, "imp": 1, "emo": 1, "str": 1}
        config.MIND_MEMORY_SCOPE = "session"
        config.MIND_RECENT_TURNS = 6
        config.MIND_AFFECT_HALF_LIFE_MINUTES = 180
        config.MIND_TIMEZONE = "Asia/Shanghai"
        config.MIND_E3_MAX_MESSAGES = 4
        config.MIND_E3_MAX_SINGLE_CHARS = 200

        calls = []

        def fake_request(**kwargs):
            purpose = kwargs.get("purpose", "")
            calls.append(purpose)
            if purpose == "mind_l1":
                return _l1_payload(depth="normal", cues=["小雨", "吃饭"])
            if purpose == "mind_think":
                return _l2_payload(
                    say_points=["哦", "就你俩啊"],
                    hidden=["其实有点在意"],
                    memory_notes=[{"summary": "小雨是同组同学", "participants": ["小雨"], "tags": ["学校"]}],
                    belief_updates=[{"subject": "小雨", "content": "同组同学", "confidence": 0.6, "evidence": []}],
                    state_delta={"valence": -0.1, "arousal": 0.2},
                    n_messages=2,
                )
            if purpose == "mind_expr":
                return "哦\n就你俩啊"
            raise AssertionError(f"unexpected purpose: {purpose}")

        # ── 1. normal 路径（L1 → L2/L3 → E2 → 提交记忆）──────────────────────
        with patch("app.services.mind.appraisal.request_model_response", fake_request), \
             patch("app.services.mind.deliberation.request_model_response", fake_request), \
             patch("app.services.mind.expression.request_model_response", fake_request):
            result = chat_service.run_chat_turn("verify-p2", "今天和小雨吃饭了", trace_id="vp2a")
            assert result["reply"] == "哦\n就你俩啊"
            row = db.fetch_one("SELECT * FROM mind_cycle WHERE trigger_ref='vp2a'")
            assert row["status"] == "completed"
            assert row["l2_json"] is not None
            assert row["wm_json"] is not None
            assert row["check_json"] is not None
            assert row["memory_committed"] == 1
            assert row["degraded"] is None
        checks.append("normal path: L2 + WM persisted, memory committed")

        # ── 2. fast 路径跳过 L2 ───────────────────────────────────────────────
        calls.clear()

        def fake_fast(**kwargs):
            purpose = kwargs.get("purpose", "")
            calls.append(purpose)
            if purpose == "mind_l1":
                return _l1_payload(depth="fast")
            if purpose == "mind_expr":
                return "好的"
            raise AssertionError(f"fast 路径不应调用 {purpose}")

        with patch("app.services.mind.appraisal.request_model_response", fake_fast), \
             patch("app.services.mind.deliberation.request_model_response", fake_fast), \
             patch("app.services.mind.expression.request_model_response", fake_fast):
            result = chat_service.run_chat_turn("verify-p2", "在吗", trace_id="vp2fast")
            assert result["reply"] == "好的"
            assert "mind_think" not in calls
        checks.append("fast path: L2 skipped entirely")

        # ── 3. 一致性检查：凌晨说早安 → 打回一次 ──────────────────────────────
        think_calls = {"count": 0}
        with patch("app.services.mind.consistency._local_hour", lambda: 1):
            def fake_time(**kwargs):
                purpose = kwargs.get("purpose", "")
                calls.append(purpose)
                if purpose == "mind_l1":
                    return _l1_payload(depth="normal")
                if purpose == "mind_think":
                    think_calls["count"] += 1
                    if think_calls["count"] == 1:
                        return _l2_payload(say_points=["早安"])
                    return _l2_payload(say_points=["这么晚还没睡?"])
                if purpose == "mind_expr":
                    return "这么晚还没睡?"
                raise AssertionError()

            with patch("app.services.mind.appraisal.request_model_response", fake_time), \
                 patch("app.services.mind.deliberation.request_model_response", fake_time), \
                 patch("app.services.mind.expression.request_model_response", fake_time):
                result = chat_service.run_chat_turn("verify-p2", "睡了吗", trace_id="vp2time")
                assert result["reply"] == "这么晚还没睡?"
                assert think_calls["count"] == 2
                row = db.fetch_one("SELECT * FROM mind_cycle WHERE trigger_ref='vp2time'")
                checks_json = json.loads(row["check_json"])
                assert checks_json[0]["ok"] is False and checks_json[1]["ok"] is True
        checks.append("consistency: 01:00 '早安' rejected once, retry passes")

        # ── 4. L2 两次非法 → 降级 ─────────────────────────────────────────────
        def fake_bad(**kwargs):
            purpose = kwargs.get("purpose", "")
            calls.append(purpose)
            if purpose == "mind_l1":
                return _l1_payload(depth="normal")
            if purpose == "mind_think":
                return "{invalid json"
            if purpose == "mind_expr":
                return "降级回复"
            raise AssertionError()

        with patch("app.services.mind.appraisal.request_model_response", fake_bad), \
             patch("app.services.mind.deliberation.request_model_response", fake_bad), \
             patch("app.services.mind.expression.request_model_response", fake_bad):
            result = chat_service.run_chat_turn("verify-p2", "测试降级", trace_id="vp2bad")
            assert result["reply"] == "降级回复"
            row = db.fetch_one("SELECT * FROM mind_cycle WHERE trigger_ref='vp2bad'")
            assert row["degraded"] == "L2" and row["status"] == "completed"
        checks.append("L2 invalid twice: degraded to P1 path, cycle completed")

        # ── 5. 分词 + 检索 + 回忆增强 ─────────────────────────────────────────
        assert tokenize.to_index_text("吃饭了") == "吃饭 饭了"
        episode_id = memory_store.insert_episode(
            "verify-p2-mem", "c1", 0, summary="今天和小雨吃饭了",
            participants=["user"], importance=0.8, strength=1.0,
        )
        hits = memory_store.search_episodes(
            "verify-p2-mem", tokenize.to_query_text("吃饭"), limit=10
        )
        assert any(item["id"] == episode_id for item in hits)
        boosted = retrieve_episodes("verify-p2-mem", ["吃饭"], "吃饭了吗", limit=1)
        assert boosted and boosted[0]["id"] == episode_id
        stored = memory_store.get_episode("verify-p2-mem", episode_id)
        assert stored["recall_count"] == 1 and stored["strength"] > 1.0 - 1e-9
        checks.append("tokenize/search/recall-boost work on two-char cues")

        # ── 6. 工作记忆裁剪（经历 → 认识 → 原话）─────────────────────────────
        for index in range(5):
            memory_store.insert_episode(
                "verify-p2-wm", f"cw{index}", 0,
                summary=f"经历{index}" + "长文字" * 10, importance=0.5 - index * 0.05,
            )
        for index in range(5):
            memory_store.upsert_belief(
                "verify-p2-wm", f"认识{index}", "内容" * 10, 0.9 - index * 0.1, [],
            )
        history = [{"role": "user", "content": f"原话{index}" + "补充" * 6} for index in range(10)]

        class FakeL1:
            cues: list[str] = []

        config.MIND_WM_MAX_CHARS = 400
        wm = wm_builder.build_working_memory(
            "verify-p2-wm", l1=FakeL1(), message="你好", history=history,
            self_card="SELF_CARD_MARK",
        )
        # 给 L2 的文本不含自我卡（她在 system 里）；槽位保留在 wm_json
        assert "SELF_CARD_MARK" not in wm["text"]
        assert "SELF_CARD_MARK" in wm["self_card"]
        assert wm["trimmed"], "预算很小时必须裁剪"
        assert 2 <= len(wm["episodes"]) < 5
        checks.append("working memory trims by slot priority, self card intact")

        # ── 7. 提交记忆幂等 ───────────────────────────────────────────────────
        from app.services.mind.deliberation import L2Result
        parsed = L2Result.from_dict(
            json.loads(_l2_payload(memory_notes=[
                {"summary": "幂等事件", "participants": ["user"], "tags": []},
            ])),
            leak_cap=1.0, max_messages=4,
        )
        from app.services.mind.memory import commit as memory_commit
        memory_commit.commit_memory(session_id="verify-p2-idem", cycle_id="cx", result=parsed)
        memory_commit.commit_memory(session_id="verify-p2-idem", cycle_id="cx", result=parsed)
        assert memory_store.count_episodes("verify-p2-idem") == 1
        checks.append("memory commit idempotent across repeated calls")

        # ── 8. 一致性：hidden 泄露 / 完成态意图 ───────────────────────────────
        from app.services.mind.deliberation import Intent
        leaked = L2Result(say_points=["其实有点吃醋"], hidden=["有点吃醋"])
        ok, errors, _ = consistency.check_result("verify-p2", leaked)
        assert not ok and any("隐藏与表达隔离" in item for item in errors)
        bad_intent = L2Result(intents=[Intent(action="已经去过公园")])
        ok2, errors2, _ = consistency.check_result("verify-p2", bad_intent)
        assert not ok2 and any("完成态" in item for item in errors2)
        # 修正轮 2：note 里的完成态不判失败；外来主语只看 action 开头
        fine_intent = L2Result(intents=[Intent(action="去睡觉", note="已经很晚了")])
        ok_ok, errors_ok, _ = consistency.check_result("verify-p2", fine_intent)
        assert ok_ok and not errors_ok
        foreign_intent = L2Result(intents=[Intent(action="你去买")])
        ok_f, errors_f, _ = consistency.check_result("verify-p2", foreign_intent)
        assert not ok_f and any("主语" in item for item in errors_f)
        checks.append("consistency catches hidden leak and completed-state intents")

        # ── 8b. 修正轮 1：时间线只查"当前时刻"的说法 ─────────────────────────
        past_mention = L2Result(say_points=["说说今天早上的事"])
        assert consistency.check_time_line(past_mention, hour=22) == []
        assert consistency.check_time_line(L2Result(say_points=["早安"]), hour=22)
        assert consistency.check_time_line(L2Result(say_points=["现在下午了"]), hour=1)
        checks.append("time-line only checks current-moment claims and greetings")

        # ── 8c. 修正轮 3：经历带 #id，evidence 用 #id → 通过 ───────────────────
        id_episode = memory_store.insert_episode(
            "verify-p2-id", "cid", 0, summary="一起吃饭", participants=["user"],
            importance=0.8, strength=1.0,
        )
        memory_store.upsert_belief("verify-p2-id", "小雨", "同学", 0.9, [])

        class _IdL1:
            cues = ["吃饭"]

        wm_id = wm_builder.build_working_memory(
            "verify-p2-id", l1=_IdL1(), message="吃饭吗", history=[], self_card="",
        )
        assert f"#{id_episode}" in wm_id["text"]
        from app.services.mind.deliberation import BeliefUpdate
        ok_ev, errors_ev, _ = consistency.check_result(
            "verify-p2-id",
            L2Result(belief_updates=[
                BeliefUpdate(subject="小雨", content="同学", confidence=1.0,
                             evidence=[id_episode]),
            ]),
        )
        assert ok_ev and not errors_ev
        checks.append("episode #id visible in WM; evidence by #id passes")

        # ── 8d. 修正轮 4：提交幂等（崩溃重跑）────────────────────────────────
        crash_result = L2Result.from_dict(
            json.loads(_l2_payload(
                memory_notes=[{"summary": "崩溃重跑事件", "participants": ["user"], "tags": []}],
                belief_updates=[{"subject": "崩溃主题", "content": "内容",
                                 "confidence": 0.9, "evidence": []}],
                state_delta={"valence": 0.3, "arousal": 0.2},
            )),
            leak_cap=1.0, max_messages=4,
        )
        memory_commit.commit_memory(
            session_id="verify-p2-crash", cycle_id="cy-crash", result=crash_result,
        )
        state_first = state.read_state("verify-p2-crash")
        belief_first = memory_store.get_belief("verify-p2-crash", "崩溃主题")
        # 首插 confidence 截到 MIND_BELIEF_INIT_MAX
        assert abs(belief_first["confidence"]
                   - min(0.9, config.MIND_BELIEF_INIT_MAX)) < 1e-9
        replay = memory_commit.commit_memory(
            session_id="verify-p2-crash", cycle_id="cy-crash", result=crash_result,
        )
        assert replay.get("skipped") == "already_committed"
        assert state.read_state("verify-p2-crash")["version"] == state_first["version"]
        assert memory_store.get_belief("verify-p2-crash", "崩溃主题")["version"] == belief_first["version"]
        assert memory_store.count_episodes("verify-p2-crash") == 1
        checks.append("memory commit replays after simulated crash without changes")

        # ── 9. 老库升级 ───────────────────────────────────────────────────────
        import sqlite3
        upgrade_dir = base / "olddata"
        upgrade_dir.mkdir()
        upgrade_db = upgrade_dir / "bot.db"
        raw = sqlite3.connect(upgrade_db)
        raw.execute(
            """CREATE TABLE mind_cycle (
                 cycle_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                 status TEXT NOT NULL DEFAULT 'running', l1_json TEXT, plan_json TEXT,
                 expr_text TEXT, final_text TEXT, error TEXT,
                 created_at REAL NOT NULL DEFAULT 0.0, updated_at REAL NOT NULL DEFAULT 0.0)"""
        )
        raw.commit()
        raw.close()
        old_dir, old_path = db.DB_DIR, db.DB_PATH
        db.DB_DIR, db.DB_PATH = upgrade_dir, upgrade_db
        try:
            db.init_db()
            columns = {row["name"] for row in db.fetch_all("PRAGMA table_info(mind_cycle)")}
            assert {"l2_json", "wm_json", "check_json", "l2_attempts",
                    "memory_committed", "degraded"} <= columns
        finally:
            db.DB_DIR, db.DB_PATH = old_dir, old_path
        checks.append("old mind_cycle table upgraded in place")

        # ── 10. usage 计费：mind_think 与 mind_l1 / mind_expr ─────────────────
        from app.services import usage
        from unittest.mock import MagicMock
        fake_resp = MagicMock()
        fake_resp.ok = True
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "id": "fake", "provider": "fake",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
        with patch("requests.post", return_value=fake_resp):
            for purpose in ("mind_l1", "mind_think", "mind_expr"):
                usage.measured_post("https://fake.test", headers={}, json={"model": "fake"},
                                    timeout=1, proxies={}, purpose=purpose)
        purposes = {row["purpose"] for row in usage.usage_summary()}
        assert {"mind_l1", "mind_think", "mind_expr"} <= purposes
        checks.append("usage records mind_l1 / mind_think / mind_expr")

    return checks


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    checks = run()
    print(json.dumps({"ok": True, "checks": checks}, ensure_ascii=False, indent=2))
