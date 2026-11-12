"""主动找他：念头从日子里来，自己冒出来；时机合适、想清楚了才发。默认关；影子模式只记不发。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import config
from app.services.mind import clock, cycle, intents, proactive, scheduler, store
from app.storage.db import execute_write, fetch_all, fetch_one, get_conn

S = "tg:1"
T0 = datetime(2026, 11, 12, 7, 0, tzinfo=timezone.utc).timestamp()   # 本地 15:00


def _say(role, text, at):
    stamp = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    execute_write("INSERT INTO messages(session_id, role, content, created_at) VALUES (?, ?, ?, ?)", (S, role, text, stamp))


def _rows(where="1=1"):
    return [dict(r) for r in fetch_all(f"SELECT * FROM mind_proactive WHERE {where} ORDER BY id")]


@pytest.fixture()
def env(isolated_db, monkeypatch):
    state = {"t": T0, "seg": {"activity": "歇会儿", "level": 3, "muted": False},
             "draw": {"want": 0.0, "when": 0.5, "still": 0.0}, "moments": [], "said": "路边有只橘猫\n胖得离谱", "calls": []}
    monkeypatch.setattr(clock, "now", lambda: state["t"])
    monkeypatch.setattr(clock, "local_now", lambda: datetime.fromtimestamp(state["t"], ZoneInfo("Asia/Shanghai")))
    monkeypatch.setattr(config, "MIND_PROACTIVE", "shadow")
    monkeypatch.setattr(config, "MIND_HOME_SESSION", S)
    monkeypatch.setattr(config, "MIND_ASYNC_DELIVERY", True)
    monkeypatch.setattr(proactive.schedule, "segment_at", lambda *a, **k: dict(state["seg"]))
    monkeypatch.setattr(proactive, "_draw", lambda *parts: state["draw"][parts[1]])
    monkeypatch.setattr(proactive.world_link, "enabled", lambda: True)
    monkeypatch.setattr(proactive.world_link, "_actor", lambda: {"state": {"recent_moments": state["moments"]}})

    def fake_cycle(session_id, message, history, **kw):
        state["calls"].append({"message": message, **kw})
        if isinstance(state["said"], Exception):
            raise state["said"]
        return state["said"]

    monkeypatch.setattr(cycle, "run_cycle", fake_cycle)
    return state


def _cat(env, share=0.8, minutes_ago=10):
    env["moments"].append({"at": env["t"] - minutes_ago * 60, "text": "路边蹲着一只很胖的橘猫", "share": share, "mood": 0.2})


def test_off_by_default_does_nothing(env, monkeypatch):
    monkeypatch.setattr(config, "MIND_PROACTIVE", "off")
    _cat(env)
    assert proactive.tick(S, force=True) == {} and scheduler.process_proactive(env["t"]) == 0
    assert _rows() == [] and env["calls"] == []


def test_a_thing_worth_telling_becomes_a_thought_that_surfaces_later(env):
    _cat(env)
    env["moments"].append({"at": T0 - 300, "text": "电梯有点慢", "share": 0.3, "mood": 0.0})       # 不值得说
    env["moments"].append({"at": T0 - 9 * 3600, "text": "早上的事", "share": 0.9, "mood": 0.0})    # 太久了
    assert proactive.collect(S, T0) == 1 and proactive.collect(S, T0) == 0                        # 同一件事只登记一回
    row, = _rows()
    assert row["kind"] == "moment" and row["status"] == "waiting" and row["weight"] == 0.8
    assert row["born_at"] == T0 - 600 and 2 * 60 <= row["due_at"] - row["born_at"] <= 240 * 60     # 过一会儿才冒出来
    assert row["expire_at"] == row["born_at"] + 4 * 3600
    env["draw"]["want"] = 0.99                                                                    # 这回没抽中"想说"
    env["moments"].append({"at": T0 - 60, "text": "食堂今天有糖醋排骨", "share": 0.6, "mood": 0.1})
    assert proactive.collect(S, T0) == 0
    assert _rows("status = 'skipped'")[0]["reason"] == "没那么想说"
    assert proactive.tick(S, T0, force=True) == {"new": 0} and env["calls"] == []                  # 还没到点，不想


def test_waits_until_she_can_look_at_her_phone_then_only_records_in_shadow_mode(env):
    _cat(env, minutes_ago=60)
    env["seg"] = {"activity": "组会", "level": 1, "muted": True}
    assert proactive.tick(S, force=True)["held"] == "她在组会，顾不上手机"
    assert _rows()[0]["status"] == "waiting" and env["calls"] == []
    env["seg"] = {"activity": "歇会儿", "level": 3, "muted": False}
    assert proactive.tick(S, force=True)["outcome"] == "shadow"
    row, = _rows()
    assert row["status"] == "shadow" and row["final_text"] == "路边有只橘猫\n胖得离谱" and row["decided_at"] == T0
    call, = env["calls"]
    assert "刚遇到的事，想跟他说：路边蹲着一只很胖的橘猫" in call["initiative"] and call["message"] == call["initiative"]
    assert call["trigger_type"] == "self" and call["commit"] is False and call["allow_fallback"] is False
    assert fetch_all("SELECT 1 FROM mind_delivery") == []                                          # 影子模式：一条都不发
    assert "本来会发" in proactive.show(S)[0] and "路边有只橘猫 / 胖得离谱" in proactive.show(S)[0]


def test_expires_when_the_moment_has_passed(env):
    _cat(env, minutes_ago=60)
    env["seg"] = {"activity": "洗澡", "level": 1, "muted": False}
    proactive.tick(S, force=True)
    env["seg"] = {"activity": "开会", "level": 3, "muted": True}
    assert proactive._reachable(S, T0) == (False, "她在开会，顾不上手机")
    env["t"] = T0 + 4 * 3600
    env["seg"] = {"activity": "歇会儿", "level": 3, "muted": False}
    assert "outcome" not in proactive.tick(S, force=True)
    row, = _rows()
    assert row["status"] == "lapsed" and row["reason"] == "她在洗澡，顾不上手机，等到新鲜劲过了" and env["calls"] == []


def test_not_while_they_are_chatting_and_the_fuse(env, monkeypatch):
    _cat(env, minutes_ago=60)
    _say("user", "在干嘛", T0 - 300)
    assert proactive.tick(S, force=True)["held"] == "刚聊过"                # 话会在聊天里自然带出来
    env["t"] = T0 + 30 * 60
    monkeypatch.setattr(config, "MIND_PROACTIVE_FUSE", 0)
    assert proactive.tick(S, force=True)["held"].startswith("保险丝")
    monkeypatch.setattr(config, "MIND_PROACTIVE_FUSE", 20)
    store.create_deferred(S, ["t1"], env["t"] + 60, "reply")               # 有一条正等着她回
    assert proactive.tick(S, force=True)["held"] == "正在聊"
    assert _rows("kind = 'silence'") == []                                 # 半小时前才说过话，谈不上"好久没聊"


def test_on_mode_sends_through_the_normal_delivery_path_then_she_follows_up(env, monkeypatch):
    monkeypatch.setattr(config, "MIND_PROACTIVE", "on")
    _cat(env, minutes_ago=60)
    assert proactive.tick(S, force=True)["outcome"] == "queued" and env["calls"][0]["commit"] is True
    queued = [dict(r) for r in fetch_all("SELECT * FROM mind_delivery ORDER BY seq")]
    assert [d["kind"] for d in queued] == ["typing", "text", "typing", "text"] and queued[0]["session_id"] == S
    assert proactive.unanswered(S) == 0                                     # 还没真发出去，不算
    execute_write("UPDATE mind_delivery SET status = 'sent'")   # 网关发出去了
    env["t"] = T0 + 60
    proactive.tick(S, force=True)
    first = _rows("kind = 'moment'")[0]
    assert first["status"] == "sent" and proactive.unanswered(S) == 1
    nudge = _rows("kind = 'nudge'")[0]                                      # 他没回 → 生出一个"追一句"的念头
    assert nudge["key"] == f"nudge:{first['id']}" and nudge["status"] == "waiting"
    assert 8 * 60 <= nudge["due_at"] - nudge["born_at"] <= 240 * 60
    env["t"] = nudge["due_at"] + 25 * 60
    env["said"] = "人呢"
    assert proactive.tick(S, force=True)["outcome"] == "queued"
    assert "主动发了「路边有只橘猫 / 胖得离谱」，他一直没回" in env["calls"][-1]["initiative"]   # 带上她上回的原话
    execute_write("UPDATE mind_delivery SET status = 'sent'")
    proactive.tick(S, force=True)
    assert proactive.unanswered(S) == 2
    _say("user", "刚在开会", env["t"] + 30)                                 # 他一回，这个数清零
    assert proactive.unanswered(S) == 0


def test_the_more_he_ignores_the_less_she_sends(env):
    _cat(env, minutes_ago=60)
    proactive.tick(S, force=True)                                           # 第一回（影子）
    env["t"] = T0 + 3600
    env["draw"]["still"] = 0.9                                              # 0.9 ≥ 0.85：这回不追了
    assert proactive.tick(S, force=True)["outcome"] == "lapsed"
    nudge = _rows("kind = 'nudge'")[0]
    assert nudge["status"] == "lapsed" and nudge["reason"] == "发了 1 回他都没回，不想再发了" and len(env["calls"]) == 1
    env["moments"].append({"at": env["t"] - 60, "text": "奶茶店排了好长的队", "share": 0.7, "mood": 0.0})
    env["t"] += 3600
    env["draw"]["still"] = 0.5                                              # 0.5 < 0.85：又遇到一件事，还是说了
    assert proactive.tick(S, force=True)["outcome"] == "shadow"
    assert "奶茶店排了好长的队（另外：你之前主动发的 1 回消息，他还没回）" in env["calls"][-1]["initiative"]
    second = env["t"]
    env["t"] = second + 60
    proactive.tick(S, force=True)                                           # 这一回也没回 → 又一个"追一句"在等
    env["draw"]["when"] = 0.01
    env["moments"].append({"at": second + 120, "text": "楼下的桂花开了", "share": 0.9, "mood": 0.3})
    env["t"] = second + 21 * 60
    env["draw"]["still"] = 0.5                                              # 两回没回：0.5 ≥ 0.45，这件不说了
    assert proactive.tick(S, force=True)["outcome"] == "lapsed"
    assert _rows("status = 'lapsed'")[-1]["text"] == "楼下的桂花开了" and len(env["calls"]) == 2
    env["moments"].append({"at": second + 180, "text": "食堂今天有糖醋排骨", "share": 0.9, "mood": 0.3})
    env["draw"]["still"] = 0.1                                              # 0.1 < 0.45：说了，追问并进这条里
    assert proactive.tick(S, force=True)["outcome"] == "shadow"
    assert _rows("kind = 'nudge'")[-1]["status"] == "merged" and "2 回消息，他还没回" in env["calls"][-1]["initiative"]
    assert proactive.STILL == [1.0, 0.85, 0.45, 0.15, 0.05]


def test_she_can_think_better_of_it_and_failures_do_not_send(env, monkeypatch):
    monkeypatch.setattr(config, "MIND_PROACTIVE", "on")
    _cat(env, minutes_ago=60)
    env["said"] = ""
    assert proactive.tick(S, force=True)["outcome"] == "declined" and _rows()[0]["reason"] == "想了想，算了"
    env["t"] = T0 + 3600
    env["moments"].append({"at": T0 + 1800, "text": "奶茶店排了好长的队", "share": 0.7, "mood": 0.0})
    env["said"] = RuntimeError("model down")
    assert proactive.tick(S, force=True)["outcome"] == "failed"
    assert _rows("status = 'failed'")[0]["reason"] == "没想成：RuntimeError"
    assert fetch_all("SELECT 1 FROM mind_delivery") == [] and proactive.unanswered(S) == 0


def test_a_message_that_cannot_go_out_is_dropped_not_sent_late(env, monkeypatch):
    monkeypatch.setattr(config, "MIND_PROACTIVE", "on")
    _cat(env, minutes_ago=60)
    proactive.tick(S, force=True)
    env["t"] = T0 + 31 * 60                                                 # 断网半小时：还排着没发
    proactive.tick(S, force=True)
    row = _rows("kind = 'moment'")[0]
    assert row["status"] == "cancelled" and row["reason"] == "一直发不出去，过期作废"
    assert fetch_one("SELECT COUNT(*) AS n FROM mind_delivery WHERE status = 'pending' AND kind = 'text'")["n"] == 0
    env["moments"].append({"at": env["t"] - 60, "text": "奶茶店排了好长的队", "share": 0.7, "mood": 0.0})
    env["t"] += 3600
    proactive.tick(S, force=True)                                           # 第二条排上了，他先来了消息
    store.cancel_remaining_deliveries(_rows("status = 'queued'")[0]["cycle_id"])
    proactive.tick(S, force=True)
    assert _rows("kind = 'moment'")[1]["reason"] == "他先来了消息，没发出去" and _rows("kind = 'nudge'") == []


def test_things_on_her_mind_only_come_up_while_she_is_awake(env):
    _say("user", "晚安", T0 - 30 * 3600)
    _say("assistant", "安", T0 - 30 * 3600 + 60)
    with get_conn() as conn:
        intents.add(conn, S, "问问他面试怎么样", source="reflection", strength=0.8, due_at=T0 + 86400, key="i1")
        intents.add(conn, S, "去图书馆看文献", source="reflection", strength=0.8, due_at=T0 + 86400, key="i2")
        intents.add(conn, S, "电脑记得备份一下", source="reflection", strength=0.8, due_at=T0 + 86400, key="i3")
    env["seg"] = {"activity": "睡觉", "level": 0, "muted": True}
    assert proactive.collect(S, T0) == 0 and _rows() == []                  # 睡着时不登记，免得一睁眼全发出来
    env["seg"] = {"activity": "歇会儿", "level": 3, "muted": False}
    assert proactive.collect(S, T0) == 2
    kinds = {r["kind"]: r for r in _rows()}
    assert set(kinds) == {"intent", "silence"} and len(_rows()) == 2        # 去图书馆归世界；备份电脑是她自己的事
    assert kinds["intent"]["text"] == "问问他面试怎么样" and "没跟他说话了" in kinds["silence"]["text"]
    assert kinds["silence"]["key"] == "silence:2026-11-12" and proactive.collect(S, T0 + 3600) == 0   # 一天只想起一回


def test_prompts_know_nobody_wrote_first():
    from app.services.mind import deliberation, expression
    from app.services.mind.schemas import E1Plan, L1Appraisal

    l1 = L1Appraisal(gut="橘猫")
    asked = deliberation.build_deliberation_messages(
        wm_text="（工作记忆）", l1=l1, message="x", self_card="卡", leak_cap=0.3,
        initiative="刚遇到的事，想跟他说：路边蹲着一只很胖的橘猫")[1]["content"]
    assert "【此刻】\n他没有发新消息" in asked and "想主动找他：刚遇到的事，想跟他说：路边蹲着一只很胖的橘猫" in asked
    assert "【当前消息】" not in asked and "恒为 true；" not in asked and "还是算了，就 false" in asked
    plain = deliberation.build_deliberation_messages(
        wm_text="（工作记忆）", l1=l1, message="在干嘛", self_card="卡", leak_cap=0.3)[1]["content"]
    assert "【当前消息】\n在干嘛" in plain and "- l2.decision.reply 恒为 true；" in plain and "【此刻】" not in plain
    plan = E1Plan(say_points=["路上看到只橘猫，很胖"], n_messages=2)
    said = expression._build_e2_messages(plan, "风格卡", "", [], "x", initiative=True)[1]["content"]
    assert "是你想主动找他" in said and "你想说的意思：路上看到只橘猫，很胖" in said
    assert "对方：" not in said and "随手发消息那样打字" in said
    reply = expression._build_e2_messages(plan, "风格卡", "", [], "在干嘛")[1]["content"]
    assert "对方：在干嘛\n\n你想回的意思：路上看到只橘猫，很胖" in reply and "随手回那样打字" in reply


def test_cycle_lets_her_decide_not_to_send_and_shadow_leaves_no_memory(isolated_db, monkeypatch):
    from app.services.mind.deliberation import Decision, L2Result
    from app.services.mind.schemas import L1Appraisal

    monkeypatch.setattr(config, "MIND_ENABLED", True)
    monkeypatch.setattr(config, "MIND_L1_MODEL", "fake/l1")
    seen = {"l2": [], "e2": [], "commit": 0, "result": L2Result(decision=Decision(reply=False))}
    monkeypatch.setattr(cycle.wm_builder, "build_working_memory", lambda *a, **k: {"text": "（工作记忆）"})
    monkeypatch.setattr(cycle.deliberation, "run_deliberation",
                        lambda **k: (seen["l2"].append(k["initiative"]) or seen["result"], "fake"))
    monkeypatch.setattr(cycle.consistency, "check_result", lambda *a, **k: (True, [], []))
    monkeypatch.setattr(cycle.expression, "run_e2", lambda **k: seen["e2"].append(k["initiative"]) or "路边有只橘猫")
    monkeypatch.setattr(cycle.memory_commit, "commit_memory", lambda **k: seen.update(commit=seen["commit"] + 1))
    monkeypatch.setattr(cycle, "_record_intents", lambda *a, **k: None)
    l1 = json.dumps(L1Appraisal(gut="橘猫").to_dict(), ensure_ascii=False)

    def run(ref, **kw):
        return cycle.run_cycle(S, "想说橘猫", [], messages=[{"role": "user", "content": "x"}], trace_id=ref,
                               trigger_type="self", trigger_ref=ref, preset_l1_json=l1, allow_fallback=False,
                               initiative="想说橘猫", **kw)

    assert run("self:a") == "" and seen["l2"] == ["想说橘猫"] and seen["e2"] == []     # 想了想，算了：不措辞
    done = store.get_cycle(cycle._derive_cycle_id(S, "self:a"))
    assert done["status"] == "completed" and done["final_text"] == "" and done["trigger_type"] == "self"
    seen["result"] = L2Result(decision=Decision(reply=True), say_points=["路上看到只橘猫"])
    assert run("self:b", commit=False) == "路边有只橘猫" and seen["e2"] == [True]
    assert seen["commit"] == 0                                                        # 影子：话没真发，不记成"说过了"
    assert run("self:c") == "路边有只橘猫" and seen["commit"] == 1

