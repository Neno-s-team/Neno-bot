"""Offline P1 mind checks: temporary DB + fake provider + synthetic data."""
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


def run():
    checks = []
    with tempfile.TemporaryDirectory(prefix="neno-mind-") as temp:
        base = Path(temp)
        from app.storage import db, operations as ops
        from app.services import chat_service
        from app import config
        from app.services.mind import store, state

        db.DB_DIR = base / "data"
        db.DB_DIR.mkdir()
        db.DB_PATH = db.DB_DIR / "bot.db"
        ops._instance = ops.Operations(db.DB_DIR / "operations.db")
        db.init_db()

        # ── 配置 ──────────────────────────────────────────────────────────────
        config.MIND_ENABLED = True
        config.MIND_L1_MODEL = "fake-l1"
        config.MIND_EXPR_MODEL = "fake-expr"
        config.MIND_RECENT_TURNS = 6
        config.MIND_AFFECT_HALF_LIFE_MINUTES = 180
        config.MIND_TIMEZONE = "Asia/Shanghai"

        def fake_request(**kwargs):
            purpose = kwargs.get("purpose", "")
            if purpose == "mind_l1":
                return json.dumps({
                    "category": "分享",
                    "gut": "他想聊聊",
                    "affect_delta": {"valence": 0.1, "arousal": 0.2},
                    "urgency": 0.3,
                    "cues": ["测试"],
                    "depth": "fast",
                    "reply_will": "now",
                }, ensure_ascii=False)
            if purpose == "mind_expr":
                return "收到啦\n今天怎么样"
            raise AssertionError(f"unexpected purpose: {purpose}")

        # ── 正常路径 ──────────────────────────────────────────────────────────
        with patch("app.services.mind.appraisal.request_model_response", fake_request), \
             patch("app.services.mind.expression.request_model_response", fake_request):
            result = chat_service.run_chat_turn("verify", "你好", trace_id="v1")
            assert result["reply"] == "收到啦\n今天怎么样"
            cycle_row = db.fetch_one("SELECT * FROM mind_cycle WHERE session_id='verify'")
            assert cycle_row["status"] == "completed"
            assert cycle_row["l1_json"] is not None
            assert cycle_row["plan_json"] is not None
            assert cycle_row["final_text"] is not None
        checks.append("normal path: cycle completed with all layers")

        # ── L1 降级 ───────────────────────────────────────────────────────────
        def fake_l1_invalid(**kwargs):
            if kwargs.get("purpose") == "mind_l1":
                return "{invalid"
            raise AssertionError()

        def fake_old(messages, **kwargs):
            return "兜底回复"

        with patch("app.services.mind.appraisal.request_model_response", fake_l1_invalid), \
             patch("app.services.chat.llm_gateway.generate_chat_reply", fake_old):
            result = chat_service.run_chat_turn("verify", "测试降级", trace_id="v2")
            assert result["reply"] == "兜底回复"
            cycle_row = db.fetch_one("SELECT * FROM mind_cycle WHERE session_id='verify' AND trigger_ref='v2'")
            assert cycle_row["status"] == "failed"
        checks.append("L1 fallback: cycle failed, old path used")

        # ── 深夜 leak ─────────────────────────────────────────────────────────
        with patch("app.services.mind.state.datetime") as mock_dt:
            from unittest.mock import MagicMock
            mock_now = MagicMock()
            mock_now.hour = 1
            mock_dt.now.return_value = mock_now
            leak_night = state.compute_leak(arousal=0.5, base_inhibition=0.7)

        with patch("app.services.mind.state.datetime") as mock_dt:
            from unittest.mock import MagicMock
            mock_now = MagicMock()
            mock_now.hour = 15
            mock_dt.now.return_value = mock_now
            leak_day = state.compute_leak(arousal=0.5, base_inhibition=0.7)

        assert leak_night > leak_day
        checks.append("night leak higher than day")

        # ── E3 渲染 ───────────────────────────────────────────────────────────
        from app.services.mind.expression import run_e3
        raw = "# 标题\n**加粗**\n- 列表项\n```python\ncode\n```\n[表情包：不存在的]\n正常"
        rendered = run_e3(raw, sticker_names=set())
        assert "#" not in rendered
        assert "**" not in rendered
        assert "```" not in rendered
        assert "code" not in rendered
        assert "不存在的" not in rendered  # 空表时显式标记被删除
        assert "正常" in rendered  # 5 行在 max_total=4 限制内
        checks.append("E3: markdown stripped, code block removed, max 4 lines")

        # ── usage 计费 ────────────────────────────────────────────────────────
        from app.services import usage
        from unittest.mock import MagicMock
        fake_resp = MagicMock()
        fake_resp.ok = True
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "id": "fake", "provider": "fake",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}
        }
        with patch("requests.post", return_value=fake_resp):
            usage.measured_post("https://fake.test", headers={}, json={"model": "fake"},
                                timeout=1, proxies={}, purpose="mind_l1")
            usage.measured_post("https://fake.test", headers={}, json={"model": "fake"},
                                timeout=1, proxies={}, purpose="mind_expr")
        rows = usage.usage_summary()
        purposes = [r["purpose"] for r in rows]
        assert "mind_l1" in purposes
        assert "mind_expr" in purposes
        checks.append("usage: mind_l1 and mind_expr recorded")

    return checks


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    checks = run()
    print(json.dumps({"ok": True, "checks": checks}, ensure_ascii=False, indent=2))





