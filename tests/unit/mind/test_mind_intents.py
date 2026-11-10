"""D4 打算 / D6 心愿：睡前想的明天、聊天里说的"一会儿去"变成打算，给世界挑事加分、做了就收起、会淡会过期；
心愿不预设，同一件事连着几晚出现才可能长出来。"""
from __future__ import annotations

import json

import pytest

from app import config
from app.services.mind import clock, intents, reflection, wishes
from app.storage.db import fetch_all, fetch_one, get_conn

T0 = 1_794_300_000.0


@pytest.fixture()
def mind(isolated_db, monkeypatch):
    state = {"t": T0}
    monkeypatch.setattr(clock, "now", lambda: state["t"])
    return state


def test_keywords_map_to_world_choices():
    assert intents.match("明天把第二章论文改完") == ["study_desk", "study_library", "study_cafe"]
    assert "walk_river" in intents.match("想去江边走走")
    assert intents.match("发呆") == []


def test_tomorrow_becomes_intent_and_boosts_choice(mind):
    with get_conn() as conn:
        n = reflection._apply_tomorrow(conn, "s", "2026-11-10", {"tomorrow": ["去图书馆写论文", "发会儿呆"]}, T0)
    assert n == 2
    rows = intents.open_intents("s")
    assert {r["text"] for r in rows} == {"去图书馆写论文", "发会儿呆"}
    assert all(0.4 <= r["strength"] <= 0.8 for r in rows)
    boost = intents.boosts()
    assert boost["study_library"] > 0.5 and "walk_river" not in boost
    assert intents.render("s")[0].startswith("去图书馆写论文")


def test_intent_fades_lapses_and_completes(mind):
    with get_conn() as conn:
        intents.add(conn, "s", "去江边走走", source="chat", strength=0.6, due_at=T0 + 6 * 3600, key="k1")
        intents.add(conn, "s", "写论文", source="chat", strength=0.6, due_at=T0 + 86400, key="k2")
    fresh = {r["text"]: r["effective"] for r in intents.open_intents("s")}
    mind["t"] = T0 + 86400
    later = {r["text"]: r["effective"] for r in intents.open_intents("s")}
    assert 0.6 * 0.5 - 1e-9 <= later["写论文"] <= 0.6 * 0.8 + 1e-9 and later["写论文"] < fresh["写论文"]
    assert intents.mark_done("study_desk") and "写论文" not in {r["text"] for r in intents.open_intents("s")}
    mind["t"] = T0 + 3 * 86400
    with get_conn() as conn:
        assert intents.expire(conn, mind["t"]) == 1
    assert fetch_one("SELECT status FROM mind_intent WHERE key='k1'")["status"] == "lapsed"


def test_chat_intents_recorded_from_l2(mind):
    from app.services.mind.cycle import _record_intents
    from app.services.mind.deliberation import Intent, L2Result
    _record_intents("s", "c1", L2Result(intents=[Intent(action="一会儿下楼买咖啡")]))
    _record_intents("s", "c1", L2Result(intents=[Intent(action="一会儿下楼买咖啡")]))  # 重放不重复
    rows = fetch_all("SELECT * FROM mind_intent")
    assert len(rows) == 1 and json.loads(rows[0]["choices_json"]) == ["coffee", "study_cafe"]


def test_wishes_only_grow_from_repetition(mind):
    grown_on = []
    for night in range(10):
        with get_conn() as conn:
            result = wishes.observe(conn, "s", f"n{night}", {"tomorrow": ["想去江边走走"]}, T0 + night * 86400)
        if result["grown"]:
            grown_on.append(night)
    assert grown_on and grown_on[0] >= 2  # 至少连着三晚才可能长出来
    assert wishes.render("s") == ["一直想去江边走走（心愿）"]
    assert intents.boosts().get("walk_river", 0) > 0


def test_wish_growth_is_not_certain(mind):
    nights_needed = set()
    for k in range(12):
        sid = f"s{k}"
        for night in range(12):
            with get_conn() as conn:
                if wishes.observe(conn, sid, f"n{night}", {"tomorrow": ["写小说"]}, T0)["grown"]:
                    nights_needed.add(night)
                    break
    assert len(nights_needed) > 1  # 有人第三晚就长出来，有人要更久


def test_unrepeated_seed_fades_away(mind):
    with get_conn() as conn:
        wishes.observe(conn, "s", "n0", {"tomorrow": ["学吉他"]}, T0)
        for night in range(1, 12):
            wishes.observe(conn, "s", f"n{night}", {"tomorrow": []}, T0)
    assert fetch_one("SELECT status FROM mind_wish WHERE session_id='s'")["status"] == "gone"
