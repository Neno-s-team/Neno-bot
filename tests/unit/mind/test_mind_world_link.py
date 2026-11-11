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


def test_lived_day_reaches_reflection(linked, monkeypatch):
    from app.services.mind import reflection
    from app.services.world.npcs import land_contact
    from app.storage.world_store import world as load_world
    monkeypatch.setattr(config, "MIND_HOME_SESSION", "tg:home")
    linked.advance("life", Advance(request_id="d", seconds=3 * 3600))
    with linked.store.transaction(write=True) as c:
        w = load_world(c, "life")
        land_contact(c, w, {"label": "妈妈", "channel": "call", "npc": "mom"}, [["A", "吃了没"]], "妈打电话来问吃了没", "k")
    lived = world_link.day_events(T0 - 60, T0 + 4 * 3600)
    assert lived and any("妈打电话来问吃了没" in x for x in lived)
    assert "tg:home" in reflection.active_sessions(T0)
    material = reflection._day_material("tg:home", T0 - 60, T0 + 4 * 3600)
    assert material["lived"] == lived
    prompt = reflection._messages("", material, {"valence": 0, "arousal": 0})[1]["content"]
    assert "【你今天过的日子】" in prompt and "妈打电话来问吃了没" in prompt
    assert reflection._day_material("tg:other", T0 - 60, T0 + 4 * 3600)["lived"] == []


def test_she_knows_what_broke_and_whether_she_fixed_it(linked):
    """坏了什么、修没修好：此刻的情境里有，夜里回想的那一天里也有（不是只有一句"有个东西坏了"）。"""
    from app.services.mind.memory import wm_builder
    from app.services.mind.schemas import L1Appraisal
    from app.storage.world_store import emit, world as load_world
    _set_doing(linked, "read")
    with linked.store.transaction(write=True) as c:
        w = load_world(c, "life")
        laptop = entity(c, "life", "laptop")
        laptop["state"]["broken"] = True
        save_entity(c, laptop)
        emit(c, w, "p1", "perturb.happened", {"id": "thing_broke", "label": "有个东西坏了", "target": "neno",
                                              "applied": [{"broke": "laptop", "label": "电脑"}]}, "neno")
        emit(c, w, "f1", "life.fix_failed", {"item": "laptop", "label": "电脑"}, "neno")
    memory = wm_builder.build_working_memory("tg:x", l1=L1Appraisal.from_dict({"cues": []}), message="在干嘛",
                                             history=[], self_card="")
    assert "坏着没修好的：电脑" in wm_builder.render_working_memory(memory)
    lived = world_link.day_events(T0 - 60, T0 + 60)
    assert any(x.endswith("电脑坏了") for x in lived) and any("电脑，没修好" in x for x in lived)
    with linked.store.transaction(write=True) as c:
        laptop = entity(c, "life", "laptop")
        laptop["state"]["broken"] = False
        save_entity(c, laptop)
    memory = wm_builder.build_working_memory("tg:x", l1=L1Appraisal.from_dict({"cues": []}), message="在干嘛",
                                             history=[], self_card="")
    assert "坏着" not in wm_builder.render_working_memory(memory)


def test_she_knows_which_city_she_lives_in(linked):
    from app.services.mind.memory import wm_builder
    from app.services.mind.schemas import L1Appraisal
    _set_doing(linked, "read")
    memory = wm_builder.build_working_memory("tg:x", l1=L1Appraisal.from_dict({"cues": []}), message="你在哪个城市",
                                             history=[], self_card="")
    text = wm_builder.render_working_memory(memory)
    assert "你住杭州紫金文苑（三坝地铁站附近），学校在浙江大学紫金港校区" in text


def test_she_knows_the_day_the_weather_and_whats_on(linked):
    """今天什么日子、外面什么天、最近听说的事：都在她的情境里；没听说的不在。"""
    from app.services.mind.memory import wm_builder
    from app.services.mind.schemas import L1Appraisal
    from app.services.world import happenings, weather
    from app.storage.world_store import world as load_world
    _set_doing(linked, "read")
    with linked.store.transaction(write=True) as c:
        w = load_world(c, "life")
        weather.apply(c, w, {"weather": "rain", "label": "小雨", "temp": 9.0, "feels": 6.0, "wind": 10,
                             "today": {"label": "小雨", "weather": "rain", "min": 7, "max": 11, "rain_prob": 90},
                             "tomorrow": {"label": "多云", "weather": "cloudy", "min": 8, "max": 16, "rain_prob": 10}})
        st = happenings.state(w)
        st["known"]["neno"] = {}
        tpl = dict(happenings.templates(w)["lecture"])
        from datetime import date
        talk = happenings._create(c, w, tpl, date(2026, 11, 12))
        hidden = happenings._create(c, w, dict(happenings.templates(w)["reading_group"]), date(2026, 11, 13))
        happenings.know(c, w, "neno", talk, "学院群", "t")
        linked._save_world(c, w)
    memory = wm_builder.build_working_memory("tg:x", l1=L1Appraisal.from_dict({"cues": []}), message="明天干嘛",
                                             history=[], self_card="")
    text = wm_builder.render_working_memory(memory)
    assert "今天：周二" in text
    assert "外面：小雨，9°C（体感 6°C）；明天多云 8–16°C" in text
    assert talk["title"] in text and "后天" in text
    assert hidden["title"] not in text          # 没听说的事她不知道
    lived = world_link.day_events(T0 - 60, T0 + 60)
    assert any(f"听说{talk['title']}（学院群）" in x for x in lived) and any("天气：小雨，9°C" in x for x in lived)


def test_she_knows_what_she_is_attending(linked):
    from app.storage.world_store import entity, save_entity
    _set_doing(linked, "attend", place="seminar_room")
    with linked.store.transaction(write=True) as c:
        neno = entity(c, "life", "neno")
        neno["state"]["doing"]["what"] = "在听讲座《西湖文学地图》"
        save_entity(c, neno)
    seg = schedule.segment_at(T0)
    assert seg["activity"] == "在听讲座《西湖文学地图》" and seg["level"] == 1


def test_she_knows_how_her_wallet_is_doing(linked):
    from app.services.mind.memory import wm_builder
    from app.services.mind.schemas import L1Appraisal
    from app.services.world import money
    from app.storage.world_store import entity, save_entity, world as load_world
    _set_doing(linked, "read")
    with linked.store.transaction(write=True) as c:
        w = load_world(c, "life")
        neno = entity(c, "life", "neno")
        neno["state"]["credits"] = 130
        save_entity(c, neno)
        food = entity(c, "life", "home_food")
        food["state"]["quantity"] = 0
        save_entity(c, food)
        money.spend(c, w, "neno", 18, "张记锅贴", "吃饭", "t1", exact=True)
        money.spend(c, w, "neno", 39, "话费", "bill", "t2", force=True, exact=True)
        linked._save_world(c, w)
    memory = wm_builder.build_working_memory("tg:x", l1=L1Appraisal.from_dict({"cues": []}), message="晚上吃啥",
                                             history=[], self_card="")
    text = wm_builder.render_working_memory(memory)
    assert "手头：¥73，离下次到账还有 4 天，很紧；家里吃的没了" in text
    lived = world_link.day_events(T0 - 60, T0 + 60)
    assert any("交了话费 ¥39" in x for x in lived) and lived[-1] == "这天一共花了 ¥18"


def test_she_knows_what_is_due_and_what_she_is_working_on(linked):
    from app.services.mind.memory import wm_builder
    from app.services.mind.schemas import L1Appraisal
    from app.services.world import tasks
    from app.storage.world_store import emit, entity, save_entity, world as load_world
    _set_doing(linked, "study")
    with linked.store.transaction(write=True) as c:
        w = load_world(c, "life")
        tasks.state(w)["items"] = {
            "a": {"id": "a", "src": "advisor_task", "title": "改开题报告第二章", "from": "导师", "assigned": T0 - 86400,
                  "due": T0 + 2 * 86400 - 6 * 3600, "effort": 8.0, "done": 4.0, "status": "open",
                  "after_due": "overdue", "npc": None, "known": True},
            "b": {"id": "b", "src": "advisor_task", "title": "补文献综述", "from": "导师", "assigned": T0 - 9 * 86400,
                  "due": T0 - 86400, "effort": 6.0, "done": 1.0, "status": "open", "after_due": "overdue", "npc": None,
                  "known": True, "overdue": True},
            "c": {"id": "c", "src": "advisor_task", "title": "还没公布的活", "from": "导师", "assigned": T0 + 86400,
                  "due": T0 + 5 * 86400, "effort": 6.0, "done": 0.0, "status": "open", "after_due": "close", "npc": None}}
        neno = entity(c, "life", "neno")
        neno["state"]["doing"].update(task="a", what="在弄：改开题报告第二章")
        save_entity(c, neno)
        emit(c, w, "p1", "task.progress", {"tid": "a", "title": "改开题报告第二章", "hours": 1.5, "gained": 1.2}, "neno")
        emit(c, w, "p2", "task.progress", {"tid": "a", "title": "改开题报告第二章", "hours": 1.0, "gained": 0.8}, "neno")
        emit(c, w, "m1", "task.missed", {"tid": "b", "title": "补文献综述", "outcome": "overdue", "share": 0.2}, "neno")
        linked._save_world(c, w)
    memory = wm_builder.build_working_memory("tg:x", l1=L1Appraisal.from_dict({"cues": []}), message="在忙啥",
                                             history=[], self_card="")
    text = wm_builder.render_working_memory(memory)
    assert "你此刻在卧室，在弄：改开题报告第二章" in text
    assert "- 已经过了期限（原定昨天）：补文献综述（导师；开了个头）" in text
    assert "- 后天 15:00 前：改开题报告第二章（导师；做了一半）" in text
    assert text.index("补文献综述") < text.index("- 后天 15:00 前")     # 欠着的排前面
    assert "还没公布的活" not in text
    lived = world_link.day_events(T0 - 60, T0 + 60)
    assert "补文献综述到期了还没交" in " ".join(lived)
    assert "这天在「改开题报告第二章」上花了 2.5 小时" in lived
