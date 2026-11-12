"""给界面看的只读接口：她的一天、她起过的念头、一条消息背后她想的、今天花了多少钱、历史消息。"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app import config
from app.routers import mind as views
from app.services.mind import clock, delivery, store, world_link
from app.storage.db import execute_write
from app.storage.operations import operations

S = "me"
T0 = datetime(2026, 11, 12, 7, 0, tzinfo=timezone.utc).timestamp()      # 本地 15:00
DAY = datetime(2026, 11, 12, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


@pytest.fixture()
def env(isolated_db, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: T0)
    monkeypatch.setattr(clock, "local_now", lambda: datetime.fromtimestamp(T0, ZoneInfo("Asia/Shanghai")))
    monkeypatch.setattr(config, "MIND_HOME_SESSION", S)
    return monkeypatch


def _say(role, text, at, trace=None, meta=None):
    stamp = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    execute_write("INSERT INTO messages(session_id, role, content, trace_id, message_type, metadata_json, created_at)"
                  " VALUES (?, ?, ?, ?, 'text', ?, ?)", (S, role, text, trace, json.dumps(meta) if meta else None, stamp))


def test_history_shows_whether_she_has_seen_each_message(env):
    _say("user", "在干嘛", T0 - 600, trace="t1")
    _say("assistant", "写论文", T0 - 500, trace="delivery:c1:1", meta={"cycle_id": "c1", "seq": 1})
    _say("user", "吃了没", T0 - 60, trace="t2")
    _say("user", "别的会话的话", T0 - 30, trace="t3")
    execute_write("UPDATE messages SET session_id = 'other' WHERE trace_id = 't3'")
    for trace, seen in (("t1", "opened"), ("t2", "none")):
        execute_write("INSERT INTO mind_perception(session_id, trace_id, received_at, glance_at, seen) VALUES (?, ?, ?, ?, ?)",
                      (S, trace, T0, T0, seen))
    items = views.history(S)["items"]
    assert [(i["role"], i["text"]) for i in items] == [("user", "在干嘛"), ("assistant", "写论文"), ("user", "吃了没")]
    assert items[0]["read"] is True and items[2]["read"] is False            # 她点开看了才算已读
    assert items[1]["cycle_id"] == "c1" and "read" not in items[1] and items[0]["at"] == T0 - 600
    assert len(views.history(S, limit=1)["items"]) == 1 and views.history(S, limit=1)["items"][0]["text"] == "吃了没"


def test_what_she_was_thinking_behind_a_message(env):
    store.create_cycle("c1", S, trigger_type="deferred", trigger_ref="job:1")
    store.update_cycle("c1", l1_json=json.dumps({"category": "求安慰", "gut": "他好像不太对劲", "depth": "deep"}),
                       l2_json=json.dumps({"l2": {"interpretation": "他被骂了，想找人说说", "feeling": "有点心疼，又不想显得太紧张"},
                                           "l3": {"say_points": ["问他怎么回事"], "hidden": ["其实挺担心的"], "tone": "平", "hesitate": True}}),
                       plan_json=json.dumps({"say_points": ["问他怎么回事"], "tone": "平"}),
                       expr_text="因为啥骂你啊", final_text="因为啥骂你啊\n别往心里去", status="completed")
    delivery.plan_deliveries(S, "因为啥骂你啊\n别往心里去", cycle_id="c1", base_ts=T0)
    execute_write("UPDATE mind_delivery SET status = 'sent' WHERE cycle_id = 'c1' AND seq <= 1")       # 只发出去第一句
    seen = views.inner("c1")
    assert seen["gut"] == "他好像不太对劲" and seen["thought"] == {"reads": "他被骂了，想找人说说", "feels": "有点心疼，又不想显得太紧张"}
    assert seen["say_points"] == ["问他怎么回事"] and seen["hidden"] == ["其实挺担心的"] and seen["hesitate"] is True
    assert seen["final"] == ["因为啥骂你啊", "别往心里去"] and seen["sent"] == ["因为啥骂你啊"]
    assert seen["unsent"] is False and seen["initiative"] is False
    # 随口一回：没有深思；是她自己想说的、想好了却没发出去
    store.create_cycle("c2", S, trigger_type="self", trigger_ref="self:moment:x")
    store.update_cycle("c2", l1_json=json.dumps({"gut": "橘猫"}), plan_json=json.dumps({"say_points": ["路上有只猫"]}),
                       final_text="路边有只橘猫", status="completed")
    quick = views.inner("c2")
    assert quick["thought"] is None and quick["say_points"] == ["路上有只猫"] and quick["initiative"] is True
    assert quick["sent"] == [] and quick["unsent"] is True
    with pytest.raises(HTTPException) as missing:
        views.inner("nope")
    assert missing.value.status_code == 404


def test_thoughts_list_has_the_ones_she_kept_to_herself(env):
    for i, (status, reason, said) in enumerate([("sent", "", "路边有只橘猫\n胖得离谱"), ("declined", "想了想，算了", None),
                                                ("skipped", "没那么想说", None)]):
        execute_write("INSERT INTO mind_proactive(session_id, key, kind, text, weight, born_at, due_at, expire_at, status, reason,"
                      " final_text, decided_at, created_at, updated_at) VALUES (?, ?, 'moment', ?, 0.8, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (S, f"k{i}", f"事{i}", T0, T0 + i, T0 + 9999, status, reason, said, T0 + i if status != "skipped" else None, T0, T0))
    env.setattr(config, "MIND_PROACTIVE", "shadow")
    listed = views.thoughts(S)
    assert listed["mode"] == "shadow" and [t["text"] for t in listed["items"]] == ["事2", "事1", "事0"]      # 新的在前
    assert listed["items"][2]["said"] == ["路边有只橘猫", "胖得离谱"] and listed["items"][2]["word"] == "发了"
    assert listed["items"][1]["word"] == "想了想没发" and listed["items"][1]["reason"] == "想了想，算了" and listed["items"][1]["said"] == []


def test_cost_today_adds_up_reported_and_estimated(env, monkeypatch):
    def use(provider, usd, at, status="ok"):
        operations().write("INSERT INTO llm_usage(id, created_at, purpose, model, provider, status, cost_usd) VALUES (?,?,?,?,?,?,?)",
                           (f"u{time.perf_counter_ns()}", at, "mind_think", "m", provider, status, usd))

    use("deepseek", 0.004, T0 - 3600)
    use("deepseek", 0.006, T0 - 60)
    use("openrouter", 0.02, T0 - 30)
    use("custom", None, T0 - 20)                     # 这家没报花费、也估不出来
    use("deepseek", 9.9, DAY - 60)                   # 昨天的不算
    monkeypatch.delenv("USD_CNY", raising=False)
    spent = views.cost()
    assert spent["usd"] == pytest.approx(0.03) and spent["cny"] == pytest.approx(0.22, abs=0.01) and spent["calls"] == 4
    assert spent["unknown_calls"] == 1 and spent["since"] == DAY
    assert {p["provider"]: p["usd"] for p in spent["by_provider"]} == {"deepseek": 0.01, "openrouter": 0.02, "custom": 0.0}


def test_her_day_page_only_for_the_session_that_lives_in_the_world(env, monkeypatch):
    seen = {}

    def fake_items(start, end, limit=60):
        seen.update(start=start, end=end)
        return ([{"at": DAY + 9 * 3600, "time": "09:00", "group": "do", "text": "看文献写论文"},
                 {"at": DAY + 12 * 3600, "time": "12:00", "group": "go", "text": "去了温州面馆（第一次去）"},
                 {"at": DAY + 13 * 3600, "time": "13:00", "group": "phone", "text": "刷手机看到《某篇文章》：有意思"}],
                ["这天一共花了 ¥38"])

    monkeypatch.setattr(world_link, "day_items", fake_items)
    monkeypatch.setattr(views, "now", lambda session_id: {"activity": "歇会儿", "sees_in": None})
    page = views.day(S)
    assert page["date"] == "2026-11-12" and page["today"] is True and seen == {"start": DAY, "end": T0}       # 只到此刻，不预告以后
    assert [i["group"] for i in page["items"]] == ["do", "go", "phone"] and page["summary"] == ["这天一共花了 ¥38"]
    assert page["diary"] == "" and page["now"] == {"activity": "歇会儿", "sees_in": None}
    # 昨天：整天的；有她睡前对那天说的一句
    execute_write("INSERT INTO mind_reflection(session_id, night, status, window_start, window_end, result_json) VALUES (?, ?, 'done', ?, ?, ?)",
                  (S, "2026-11-12", DAY - 86400, DAY + 1800, json.dumps({"day_summary": "平平常常的一天，论文推了一点。"}, ensure_ascii=False)))
    past = views.day(S, "2026-11-11")
    assert past["today"] is False and past["now"] is None and seen == {"start": DAY - 86400, "end": DAY}
    assert past["diary"] == "平平常常的一天，论文推了一点。"
    assert views.day("tg:someone-else")["items"] == []                       # 别的会话没有"日子"
    with pytest.raises(HTTPException) as bad:
        views.day(S, "昨天")
    assert bad.value.status_code == 400
