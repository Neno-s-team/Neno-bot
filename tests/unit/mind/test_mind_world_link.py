"""D3 世界 → 大脑：她此刻在干嘛、打断等级、往后的时段都从世界读；没接世界时照旧走课表。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app import config
from app.services.mind import clock, schedule, world_link
from app.services.world.engine import WorldEngine
from app.services.world.models import Advance, WorldCreate
from app.storage import db as db_storage
from app.storage.world_store import WorldStore, entity, save_entity

T0 = datetime(2026, 11, 10, 13, 0, tzinfo=timezone.utc).timestamp()  # 21:00 本地


@pytest.fixture()
def linked(isolated_db, monkeypatch):
    engine = WorldEngine(WorldStore(db_storage.DB_PATH))
    engine.create(WorldCreate(world_id="life", seed="link", template="life", rhythms="life",
                              start_at=datetime.fromtimestamp(T0, timezone.utc)))
    monkeypatch.setattr(config, "MIND_WORLD_ID", "life")
    monkeypatch.setattr(config, "MIND_WORLD_ACTOR", "neno")
    monkeypatch.setattr(clock, "now", lambda: T0)
    return engine


def _set_doing(engine, activity, hours=1.0, place="my_room", conditions=None):
    with engine.store.transaction(write=True) as c:
        neno = entity(c, "life", "neno")
        neno["parent_id"] = place
        neno["state"]["doing"] = {"activity": activity, "label": activity, "since": T0, "until": T0 + hours * 3600,
                                  "source": "test", "job": "x"}
        if conditions:
            neno["state"]["conditions"] = conditions
        save_entity(c, neno)


def test_level_and_place_come_from_world(linked):
    _set_doing(linked, "study")
    seg = schedule.segment_at(T0)
    assert seg["world"] == "live" and seg["level"] == 2 and seg["activity"] == "看文献写论文"
    assert seg["place"] == "卧室"
    _set_doing(linked, "scroll")
    assert schedule.segment_at(T0)["level"] == 4
    _set_doing(linked, "sleep", hours=7)
    assert schedule.segment_at(T0)["level"] == 0


def test_future_segments_follow_activity_then_obligations(linked):
    _set_doing(linked, "study", hours=0.5)
    spans = list(schedule.iter_segments(T0, T0 + 6 * 3600))
    assert spans[0][0] == T0 and spans[0][1] == T0 + 1800 and spans[0][2]["level"] == 2
    assert spans[1][2]["activity"] == "空闲" and spans[1][2]["level"] == 3
    sleep = [s for s in spans if s[2]["activity"] == "睡觉"]
    assert sleep and sleep[0][2]["level"] == 0  # 夜里那觉按作息表排进来
    assert all(a[1] <= b[0] + 1e-6 for a, b in zip(spans, spans[1:]))


def test_working_memory_knows_what_she_is_doing(linked):
    from app.services.mind.memory import wm_builder
    from app.services.mind.schemas import L1Appraisal
    _set_doing(linked, "read", place="my_room", conditions=[{"label": "感冒了，嗓子疼", "until": T0 + 86400}])
    memory = wm_builder.build_working_memory("tg:x", l1=L1Appraisal.from_dict({"cues": []}), message="在干嘛",
                                             history=[], self_card="")
    text = wm_builder.render_working_memory(memory)
    assert "你此刻在卧室，看闲书" in text and "身上：感冒了，嗓子疼" in text
    assert memory["situation"]["interrupt_level"] == 3


def test_expression_gets_doing_line(linked):
    from app.services.mind.cycle import _doing_now
    from app.services.mind.expression import _build_e2_messages
    from app.services.mind.schemas import E1Plan
    _set_doing(linked, "study", place="library")
    doing = _doing_now()
    assert doing == "在图书馆，看文献写论文"
    prompt = _build_e2_messages(E1Plan(), "", "", [], "在干嘛", doing=doing)[1]["content"]
    assert "你此刻：在图书馆，看文献写论文" in prompt and "别编" not in prompt


def test_not_linked_falls_back_to_schedule(isolated_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MIND_WORLD_ID", "")
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"sleep": {"start": "00:30", "end": "08:00"},
                                "default": {"activity": "空闲", "level": 3}}), encoding="utf-8")
    monkeypatch.setattr(config, "MIND_SCHEDULE_PATH", str(path))
    assert schedule.segment_at(T0)["world"] == "schedule"
    assert world_link.now_segment() is None


def test_world_missing_falls_back(isolated_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MIND_WORLD_ID", "nope")
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"default": {"activity": "空闲", "level": 3}}), encoding="utf-8")
    monkeypatch.setattr(config, "MIND_SCHEDULE_PATH", str(path))
    assert schedule.segment_at(T0)["world"] == "schedule"


def test_live_world_advances_and_brain_sees_it(linked):
    linked.advance("life", Advance(request_id="a", seconds=4 * 3600))
    with linked.store.transaction() as c:
        sim = c.execute("SELECT sim_time FROM world_worlds WHERE world_id='life'").fetchone()[0]
    assert sim == T0 + 4 * 3600
