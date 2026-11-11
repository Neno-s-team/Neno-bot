"""日历与真实天气：周末/节日/假期/学期认得出；假期不上课；周末更想出门；
真实天气落进世界、冷和雨让人不想在外面待；有真实天气时不再随机变天，断网久了退回随机。"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.services.world import almanac, life, perturbations, rhythms, weather
from app.services.world.engine import WorldEngine
from app.services.world.models import WorldCreate
from app.services.world.runtime import _refresh_weather
from app.storage.world_store import WorldStore, entity, save_entity, world

START = datetime.fromisoformat("2026-11-14T11:00:00+08:00")  # 周六


@pytest.fixture()
def engine(tmp_path):
    e = WorldEngine(WorldStore(tmp_path / "w.db"))
    e.create(WorldCreate(world_id="w", seed="cal", template="life", rhythms="life", start_at=START))
    return e


def _w(engine):
    with engine.store.transaction() as c:
        return world(c, "w")


def test_days_are_recognised(engine):
    w = _w(engine)
    sat = almanac.on(w, date(2026, 11, 14))
    assert {"weekend", "off", "term"} <= sat["tags"] and "workday" not in sat["tags"]
    d11 = almanac.on(w, date(2026, 11, 11))
    assert {"festival", "workday"} <= d11["tags"] and d11["names"] == ["双十一"] and "off" not in d11["tags"]
    spring = almanac.on(w, date(2027, 2, 6))
    assert {"holiday", "festival", "break", "off"} <= spring["tags"] and spring["names"][0] == "春节"
    assert "exam" in almanac.on(w, date(2027, 1, 12))["tags"]
    assert almanac.note(w, START.timestamp()) == "周六（周末）"
    assert "寒假" in almanac.note(w, datetime.fromisoformat("2027-02-02T10:00:00+08:00").timestamp())


def test_no_class_during_break_or_holidays(engine):
    w = _w(engine)
    cls = next(e for e in w["settings"]["rhythms"]["actors"]["neno"]["routine"] if e["id"] == "class")
    day = lambda d: (d - date(1970, 1, 1)).days  # noqa: E731
    assert rhythms._occurrence(w, "neno", cls, day(date(2026, 11, 17))) is not None   # 学期里的周二
    assert rhythms._occurrence(w, "neno", cls, day(date(2027, 2, 2))) is None         # 寒假里的周二
    assert rhythms._occurrence(w, "neno", cls, day(date(2027, 1, 12))) is None        # 考试周


def _scores(c, w, room=10.0):
    neno = entity(c, "w", "neno")
    neno["state"].pop("doing", None)
    neno["parent_id"] = "my_room"
    save_entity(c, neno)
    return {ch["id"]: s for ch, _, s in life._candidates(c, w, "neno", entity(c, "w", "neno"), "k", room)}


def test_weekend_changes_what_she_feels_like(engine, monkeypatch):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        weekend = _scores(c, w)
        monkeypatch.setattr(almanac, "boost", lambda *a, **k: 0.0)
        plain = _scores(c, w)
    assert weekend["go_out"] - plain["go_out"] == pytest.approx(0.45)
    assert weekend["study_desk"] - plain["study_desk"] == pytest.approx(-0.2)
    assert weekend["rest_sofa"] == pytest.approx(plain["rest_sofa"])


RAW = {"current": {"temperature_2m": 8.6, "apparent_temperature": 5.2, "weather_code": 61, "wind_speed_10m": 12.3},
       "daily": {"weather_code": [61, 2], "temperature_2m_max": [11.2, 16.4], "temperature_2m_min": [6.8, 7.9],
                 "precipitation_probability_max": [90, 10]}}


def test_real_weather_lands_in_the_world(engine):
    assert _refresh_weather(engine, "w", fetch=lambda lat, lon: RAW) is True
    with engine.store.transaction() as c:
        street = entity(c, "w", "street")["state"]
        events = [r[0] for r in c.execute("SELECT payload FROM world_events WHERE kind='weather.changed'")]
    assert street["weather"] == "rain" and street["weather_label"] == "小雨" and street["temp"] == 8.6
    assert weather.line(street) == "小雨，9°C（体感 5°C）；明天多云 8–16°C"
    assert len(events) == 1
    assert _refresh_weather(engine, "w", fetch=lambda lat, lon: RAW) is False   # 没变：不再记事件
    with engine.store.transaction() as c:
        assert c.execute("SELECT count(*) FROM world_events WHERE kind='weather.changed'").fetchone()[0] == 1


def test_offline_keeps_world_running(engine):
    def down(lat, lon):
        raise OSError("no network")
    assert _refresh_weather(engine, "w", fetch=down) is None
    with engine.store.transaction() as c:
        assert "weather_real_at" not in entity(c, "w", "street")["state"]


def test_real_weather_replaces_random_weather_until_stale(engine):
    pool = lambda c, w: {e["id"] for e, _, _ in perturbations._candidates(c, w, "small", 1.0)}  # noqa: E731
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        assert pool(c, w) & {"rain", "clouds"}
    _refresh_weather(engine, "w", fetch=lambda lat, lon: RAW)
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        assert not pool(c, w) & {"rain", "clear_up", "clouds", "clouds_part"}
        w["sim_time"] += weather.FRESH_SECONDS + 60   # 断网三小时多：退回随机天气
        assert pool(c, w) & {"clear_up"}


def test_cold_and_rain_keep_her_in(engine):
    mild = {"weather": "clear", "temp": 20, "feels": 20, "wind": 5}
    harsh = {"weather": "cloudy", "temp": 3, "feels": 2, "wind": 5}
    assert weather.mood(mild) > 0 > weather.mood(harsh) > weather.mood({**harsh, "weather": "rain"})
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        street = entity(c, "w", "street")

        def at(sky):
            street["state"].update(sky)
            save_entity(c, street)
            return _scores(c, w)

        nice, cold = at(mild), at(harsh)
    assert nice["walk_river"] - cold["walk_river"] == pytest.approx(1.0 * (weather.mood(mild) - weather.mood(harsh)))
    assert nice["go_out"] > cold["go_out"]
    assert nice["read_bed"] == pytest.approx(cold["read_bed"])  # 屋里的事不受影响
