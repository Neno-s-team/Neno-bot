"""一轮发几条：按真实分布抽，激动/想说得多时偏多；E3 不超过计划条数。"""
from __future__ import annotations

from collections import Counter

from app import config
from app.services.mind import expression
from app.services.mind.cycle import _draw_burst
from tests.unit.mind.test_mind_p2 import fake_mind_env, mind_db  # noqa: F401


def _counts(suggested=1, arousal=0.0, n=4000):
    return Counter(_draw_burst(f"cycle-{i}", suggested, arousal) for i in range(n))


def test_calm_reply_is_mostly_one_message(monkeypatch):
    monkeypatch.setattr(config, "MIND_BURST_WEIGHTS", [0.59, 0.256, 0.098, 0.036, 0.02])
    monkeypatch.setattr(config, "MIND_E3_MAX_MESSAGES", 4)
    c = _counts()
    total = sum(c.values())
    assert 0.55 < c[1] / total < 0.68
    assert 0.20 < c[2] / total < 0.32
    assert (c[3] + c[4]) / total < 0.2 and max(c) == 4  # 上限 4，5 条档并入不出现


def test_excited_or_wordy_leans_longer_but_not_forced(monkeypatch):
    monkeypatch.setattr(config, "MIND_BURST_WEIGHTS", [0.59, 0.256, 0.098, 0.036, 0.02])
    monkeypatch.setattr(config, "MIND_E3_MAX_MESSAGES", 4)
    calm, hot = _counts(), _counts(suggested=3, arousal=0.8)
    mean = lambda c: sum(k * v for k, v in c.items()) / sum(c.values())  # noqa: E731
    assert mean(hot) > mean(calm) + 0.5
    assert hot[1] > 0  # 再激动也可能只回一条


def test_draw_is_stable_for_replay():
    assert _draw_burst("same-cycle", 2, 0.3) == _draw_burst("same-cycle", 2, 0.3)


def test_e3_respects_plan_count():
    text = "在\n刚睡醒\n睡了一下午\n脑子还是懵的\n咋啦"
    assert expression.run_e3(text, max_messages=2).split("\n") == ["在", "刚睡醒 睡了一下午 脑子还是懵的 咋啦"]


def test_cycle_sends_no_more_than_drawn(mind_db, fake_mind_env, monkeypatch):
    import json

    from app.storage.db import fetch_one
    from tests.unit.mind.test_mind_p2 import _l1_payload, _l2_payload, _patch_providers
    from app.services.chat_service import run_chat_turn

    monkeypatch.setattr("app.services.mind.cycle._draw_burst", lambda cycle_id, suggested, arousal: 2)
    _patch_providers(monkeypatch, l1=_l1_payload(depth="normal"), think=_l2_payload(n_messages=4),
                     expr="在\n刚睡醒\n睡了一下午\n脑子还是懵的\n咋啦")
    result = run_chat_turn("burst", "在吗", trace_id="tburst")
    plan = json.loads(fetch_one("SELECT plan_json FROM mind_cycle WHERE session_id='burst'")["plan_json"])
    assert plan["n_messages"] == 2
    assert len(result["reply"].split("\n")) <= 3  # 计划 2 条 + 至多 1 条表情包


def test_line_lengths_follow_real_bands(monkeypatch):
    from app.services.mind.expression import draw_line_lengths
    monkeypatch.setattr(config, "MIND_LINE_LENGTH_BANDS", [[3, 0.35], [6, 0.3], [10, 0.22], [18, 0.1], [30, 0.03]])
    short = Counter(x for i in range(2000) for x in draw_line_lengths(f"c{i}", 1, "short"))
    long = Counter(x for i in range(2000) for x in draw_line_lengths(f"c{i}", 1, "long"))
    assert (short[3] + short[6]) / 2000 > 0.6 and short[30] < 80
    assert long[18] + long[30] > 2 * (short[18] + short[30])
    assert draw_line_lengths("x", 3) == draw_line_lengths("x", 3) and len(draw_line_lengths("x", 3)) == 3
