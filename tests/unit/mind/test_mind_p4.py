"""P4：夜间反思、遗忘、在意事项。模型一律用假 provider；时间用假时钟。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import config
from app.services.mind import concerns, reflection, scheduler as mind_scheduler, state
from app.services.mind.memory import store as memory_store
from app.services.mind.memory import wm_builder
from app.storage.db import execute_write, fetch_all, fetch_one, get_conn
from tests.unit.mind.test_mind_p3a import _patch_clock, fake_provider, mind_env  # noqa: F401

TZ = ZoneInfo("Asia/Shanghai")
S = "tg:p4"


def at(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=TZ).timestamp()


def _say(role: str, content: str, when: float) -> None:
    stamp = datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    execute_write("INSERT INTO messages(session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                  (S, role, content, stamp))


def _cycle(when: float, committed: int = 0) -> None:
    execute_write(
        "INSERT INTO mind_cycle(cycle_id, session_id, trigger_type, status, memory_committed, created_at, updated_at)"
        " VALUES (?, ?, 'message', 'completed', ?, ?, ?)", (f"c{when}", S, committed, when, when))


class Reflector:
    def __init__(self, payload=None, fail=0):
        self.payload = payload or {}
        self.fail = fail
        self.calls = []

    def __call__(self, *, purpose, messages, **_kw):
        assert purpose == "mind_reflect"
        self.calls.append(messages)
        if self.fail:
            self.fail -= 1
            raise RuntimeError("LLM request failed")
        base = {"day_summary": "平平常常的一天", "new_notes": [], "belief_updates": [], "concern_updates": [],
                "new_concerns": [], "mood": {"valence": 0.0, "settle": 0.5}, "tomorrow": []}
        return json.dumps({**base, **self.payload}, ensure_ascii=False)


@pytest.fixture()
def p4(mind_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "MIND_REFLECT_ENABLED", True)
    monkeypatch.setattr(config, "MIND_REFLECT_DELAY_MIN", [10, 60])
    monkeypatch.setattr(config, "MIND_FADE_THRESHOLD", [0.2, 0.35])
    monkeypatch.setattr(config, "MIND_JOB_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(config, "MIND_MEMORY_HALF_LIFE_DAYS", 14.0)
    return mind_env


def _use(monkeypatch, reflector):
    monkeypatch.setattr("app.services.mind.reflection.request_model_response", reflector)


def _night(p4, when="2026-11-10 00:31"):
    p4["clock"]["t"] = at(when)
    mind_scheduler.process_due_batch()
    return fetch_one("SELECT * FROM mind_reflection WHERE session_id = ?", (S,))


def test_reflection_scheduled_after_falling_asleep_with_drawn_delay(p4, monkeypatch):
    reflector = Reflector()
    _use(monkeypatch, reflector)
    _say("user", "今天好累", at("2026-11-09 21:00"))
    _cycle(at("2026-11-09 21:00"))
    p4["clock"]["t"] = at("2026-11-09 23:50")
    mind_scheduler.process_due_batch()
    assert fetch_one("SELECT * FROM mind_reflection WHERE session_id = ?", (S,)) is None  # 还没睡
    row = _night(p4)
    assert row["status"] == "pending" and row["night"] == "2026-11-10"
    delay = (row["due_at"] - at("2026-11-10 00:30")) / 60
    assert 10 <= delay <= 60
    p4["clock"]["t"] = row["due_at"] + 1
    mind_scheduler.process_due_batch()
    mind_scheduler.process_due_batch()
    row = fetch_one("SELECT * FROM mind_reflection WHERE session_id = ?", (S,))
    assert row["status"] == "done" and len(reflector.calls) == 1  # 一晚只反思一次
    assert "今天好累" in reflector.calls[0][1]["content"]


def _run_now(p4):
    row = _night(p4)
    p4["clock"]["t"] = row["due_at"] + 1
    mind_scheduler.process_due_batch()
    return fetch_one("SELECT * FROM mind_reflection WHERE session_id = ?", (S,))


def test_small_talk_gets_remembered_and_concerns_open(p4, monkeypatch):
    _use(monkeypatch, Reflector({
        "new_notes": [{"summary": "他说最近在学吉他，手指磨出了茧", "tags": ["吉他"]}],
        "new_concerns": [{"title": "他的考试", "detail": "下周三考试，他有点慌", "intensity": 0.9, "evidence": []}],
        "belief_updates": [{"subject": "他", "content": "最近压力有点大", "confidence": 0.9, "evidence": []}],
    }))
    _say("user", "我最近在学吉他 手指都磨出茧了", at("2026-11-09 20:00"))
    _cycle(at("2026-11-09 20:00"))
    row = _run_now(p4)
    result = json.loads(row["result_json"])
    assert result["notes"] == 1 and result["concerns_new"] == 1 and result["model"] == "ok"
    episode = fetch_one("SELECT * FROM mind_episode WHERE session_id = ? AND source = 'reflection'", (S,))
    assert "吉他" in episode["summary"] and episode["strength"] == 0.8
    assert [e["id"] for e in memory_store.search_episodes(S, '"吉他"')] == [episode["id"]]
    worry = concerns.list_open(S)[0]
    assert worry["title"] == "他的考试" and worry["intensity"] == 0.8  # 新在意上限 0.8
    assert 0.1 <= worry["decay_per_day"] <= 0.45
    belief = fetch_one("SELECT * FROM mind_belief WHERE session_id = ?", (S,))
    assert belief["confidence"] == pytest.approx(config.MIND_BELIEF_INIT_MAX)


def test_records_the_model_made_up_are_dropped_not_remembered(p4, monkeypatch):
    """便宜模型会引用不存在的记录号：丢掉那个引用，不写进记忆；写成 "#12" 的也认。"""
    _say("user", "我最近在学吉他 手指都磨出茧了", at("2026-11-09 20:00"))
    _cycle(at("2026-11-09 20:00"))
    real = memory_store.insert_episode(S, "c1", 0, summary="他说最近在学吉他，手指磨出了茧", importance=0.5,
                                       ts=at("2026-11-09 20:00"))
    _use(monkeypatch, Reflector({
        "new_concerns": [{"title": "他的考试", "detail": "下周三", "intensity": 0.5, "evidence": [99999, f"#{real}", "不是数字"]}],
        "belief_updates": [{"subject": "他", "content": "最近压力有点大", "confidence": 0.5, "evidence": [88888, real, real]}],
    }))
    row = _run_now(p4)
    assert row["status"] == "done" and json.loads(row["result_json"])["model"] == "ok"
    assert concerns.list_open(S)[0]["evidence"] == [real]
    assert json.loads(fetch_one("SELECT evidence_json FROM mind_belief WHERE session_id = ?", (S,))["evidence_json"]) == [real]
    assert reflection._cited([3, "#3", " 4 ", None, "x", 7], {3, 4}) == [3, 4]


def test_model_failure_retries_then_still_does_rule_parts(p4, monkeypatch):
    reflector = Reflector(fail=99)
    _use(monkeypatch, reflector)
    state.apply_affect_delta(S, 0.0, 0.8)
    _say("user", "在吗", at("2026-11-09 22:00"))
    _cycle(at("2026-11-09 22:00"))
    row = _run_now(p4)
    assert row["status"] == "pending" and row["attempts"] == 1
    for _ in range(2):
        p4["clock"]["t"] = row["due_at"] + 1
        mind_scheduler.process_due_batch()
        row = fetch_one("SELECT * FROM mind_reflection WHERE session_id = ?", (S,))
    assert row["status"] == "done" and len(reflector.calls) == 3
    assert json.loads(row["result_json"])["model"].startswith("failed:")
    assert state.read_state(S)["arousal"] < 0.8 * 0.5  # 没想成，觉还是睡了


def test_forgetting_turns_old_weak_memory_into_gist_but_keeps_important(p4, monkeypatch):
    _use(monkeypatch, Reflector())
    old = at("2026-10-01 12:00")
    weak = memory_store.insert_episode(S, "old1", 0, summary="那天下午他说楼下新开了一家卖烤红薯的小摊，味道一般",
                                       importance=0.2, ts=old)
    vivid = memory_store.insert_episode(S, "old2", 0, summary="他第一次说喜欢我的那个晚上，我们聊到三点",
                                        importance=1.0, valence=0.9, arousal=0.8, ts=old)
    execute_write("UPDATE mind_episode SET created_at = ? WHERE id IN (?, ?)", (old, weak, vivid))
    _say("user", "晚安", at("2026-11-09 23:00"))
    _cycle(at("2026-11-09 23:00"))
    row = _run_now(p4)
    assert json.loads(row["result_json"])["episodes_faded"] == 1
    faded = memory_store.get_episode(S, weak)
    assert faded["summary"].endswith("……") and len(faded["summary"]) < 20
    assert faded["full_summary"].startswith("那天下午") and faded["faded_at"]
    assert memory_store.get_episode(S, vivid)["faded_at"] is None
    assert memory_store.search_episodes(S, '"味道"') == []  # 细节搜不到了
    assert fetch_all("SELECT 1 FROM messages WHERE session_id = ?", (S,))  # 聊天原文不删


def test_concern_lifecycle_rewarm_and_let_go_is_not_certain(p4, monkeypatch):
    now = at("2026-11-09 12:00")
    p4["clock"]["t"] = now
    with get_conn() as conn:
        exam = concerns.open_concern(conn, S, "他的考试", "下周三考试", 0.5, 0.3, [], now)
    p4["clock"]["t"] = now + 2 * 86400
    faded = concerns.list_open(S)[0]["effective"]
    assert faded == pytest.approx(0.5 * 2 ** (-0.6))
    warmed = concerns.rewarm(S, ["考试"], "明天就考试了", "t1")
    assert warmed == [exam]
    bumped = concerns.list_open(S)[0]["effective"]
    assert 0.05 <= bumped - faded <= 0.25
    assert concerns.rewarm(S, ["吃饭"], "晚饭吃啥", "t2") == []

    released = kept = 0
    for night in range(16):
        sid = f"tg:letgo{night}"
        with get_conn() as conn:
            cid = concerns.open_concern(conn, sid, "小事", "", 0.6, 0.2, [], now)
        material = {"episodes": [], "concerns": concerns.list_open(sid)}
        reflection._apply(sid, f"2026-11-{night + 1:02d}", now, {
            "concern_updates": [{"id": cid, "action": "let_go", "intensity": 0, "note": "想开了"}]}, material)
        status = fetch_one("SELECT status FROM mind_concern WHERE id = ?", (cid,))["status"]
        released += status == "closed"
        kept += status == "open"
    assert released and kept  # 说放下，大多真放下了，偶尔还挂着一点


def test_concerns_show_up_in_working_memory_and_l1(p4, monkeypatch, fake_provider):
    now = at("2026-11-09 12:00")
    p4["clock"]["t"] = now
    with get_conn() as conn:
        concerns.open_concern(conn, S, "他的考试", "下周三", 0.7, 0.2, [], now)
    from app.services.mind.schemas import L1Appraisal
    memory = wm_builder.build_working_memory(S, l1=L1Appraisal.from_dict({"cues": []}), message="hi",
                                             history=[], self_card="")
    assert memory["concerns"] == ["他的考试（很在意，刚才开始的）：下周三"]
    assert "【在意事项】\n- 他的考试（很在意，刚才开始的）：下周三" in wm_builder.render_working_memory(memory)
    from app.services.mind import appraisal
    messages = appraisal._build_l1_messages("hi", {"valence": 0, "arousal": 0}, "", [], ["他的考试"])
    assert "心里记挂着：他的考试" in messages[1]["content"]


def test_no_chat_no_model_call(p4, monkeypatch):
    reflector = Reflector()
    _use(monkeypatch, reflector)
    _cycle(at("2026-11-09 20:00"))
    row = _run_now(p4)
    assert row["status"] == "done" and reflector.calls == []
    assert json.loads(row["result_json"])["model"] == "skipped"


def test_sleep_settles_mood_by_drawn_amount(p4, monkeypatch):
    _use(monkeypatch, Reflector({"mood": {"valence": 0.4, "settle": 1.0}}))
    p4["clock"]["t"] = at("2026-11-09 22:00")
    state.apply_affect_delta(S, -0.6, 0.9)
    _say("user", "对不起 刚才是我不好", at("2026-11-09 22:00"))
    _cycle(at("2026-11-09 22:00"))
    before = state.read_state(S)
    row = _night(p4)
    p4["clock"]["t"] = row["due_at"] + 1
    raw = state.read_state(S)
    mind_scheduler.process_due_batch()
    after = state.read_state(S)
    assert after["arousal"] < raw["arousal"] * 0.45  # 一觉醒来激动退了大半
    assert raw["valence"] < after["valence"] < 0.4  # 朝睡前心情靠，但不是一步到位
    assert before["valence"] < 0


def test_relationship_moves_slowly_only_through_reflection(p4, monkeypatch):
    from app.services.mind import relationship

    assert "很好的朋友" in relationship.describe(S)
    _use(monkeypatch, Reflector({"relation": {"closer": 1, "attraction": 1, "distance": 0, "view": "他今天挺暖的"}}))
    _say("user", "今天陪你聊到很晚 开心", at("2026-11-09 22:00"))
    _cycle(at("2026-11-09 22:00"))
    row = _run_now(p4)
    moved = json.loads(row["result_json"])["relation"]
    rel = relationship.get(S)
    assert 0.7 + 0.04 - 1e-9 <= rel["closeness"] <= 0.7 + 0.08 + 1e-9  # 一晚最多挪 0.08，且按抽样打折
    assert 0.1 < rel["attraction"] <= 0.18 + 1e-9
    assert moved["closeness"] == round(rel["closeness"], 3)
    assert "他今天挺暖的" in relationship.describe(S)
    from app.services.mind.memory import wm_builder
    from app.services.mind.schemas import L1Appraisal
    memory = wm_builder.build_working_memory(S, l1=L1Appraisal.from_dict({"cues": []}), message="hi", history=[], self_card="")
    assert "【你和他】" in wm_builder.render_working_memory(memory)


def test_relationship_scale_is_drawn(p4):
    from app.services.mind import relationship
    steps = set()
    for n in range(6):
        sid = f"tg:rel{n}"
        with get_conn() as conn:
            relationship.nudge(conn, sid, "2026-11-10", {"closer": 1, "attraction": 0, "distance": 0, "view": ""})
        steps.add(round(relationship.get(sid)["closeness"] - 0.7, 4))
    assert len(steps) > 3 and all(0.04 - 1e-9 <= x <= 0.08 + 1e-9 for x in steps)
