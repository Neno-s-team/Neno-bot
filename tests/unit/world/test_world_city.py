"""真实城市（杭州）：路程按坐标和真实地铁算；她从城里挑具体的店，店开着才去，去过有印象，第一次去会标出来。"""
from __future__ import annotations

from collections import Counter
from datetime import datetime

import pytest

from app.services.world import city
from app.services.world.engine import WorldEngine
from app.services.world.models import Advance, WorldCreate
from app.storage.world_store import WorldStore, world

START = datetime.fromisoformat("2026-11-14T07:00:00+08:00")  # 周六


@pytest.fixture()
def engine(tmp_path):
    e = WorldEngine(WorldStore(tmp_path / "w.db"))
    e.create(WorldCreate(world_id="w", seed="city", template="life", rhythms="life", start_at=START))
    return e


def _w(engine):
    with engine.store.transaction() as c:
        return world(c, "w")


def test_routes_follow_real_distances(engine):
    w = _w(engine)
    assert city.route(w, "my_room", "home_kitchen")["minutes"] == 0
    to_class = city.route(w, "my_room", "seminar_room")
    assert to_class["mode"] in ("bike", "walk") and 5 <= to_class["minutes"] <= 25  # 家到紫金港西区，骑车十来分钟
    assert city.route(w, "my_room", "parents_home")["mode"] == "train"
    far = max((v for v in city.data(w)["venues"] if v.get("town")),
              key=lambda v: city.dist(city.point(w, "my_room"), (v["lat"], v["lon"])))
    trip = city.route(w, "my_room", far["id"])
    assert trip["mode"] == "metro" and "号线" in trip["label"] and 20 <= trip["minutes"] <= 120


def test_metro_counts_transfers():
    data = city.load("hangzhou")
    direct, lines = city._ride("hangzhou", "三坝", "浙大紫金港")
    assert direct < 15 and len(lines) == 1
    far, far_lines = city._ride("hangzhou", "三坝", "火车东站")
    assert far > direct and far < 90 and all(line in {x["line"] for x in data["lines"]} for line in far_lines)


def test_nearby_and_hours(engine):
    w = _w(engine)
    shops = city.nearby(w, "my_room", ["noodle", "snack"], 1500)
    assert len(shops) >= 5
    home = city.point(w, "my_room")
    d = [city.dist(home, (v["lat"], v["lon"])) for v in shops]
    assert d == sorted(d)
    breakfast = next(v for v in city.data(w)["venues"] if v["kind"] == "breakfast")
    assert city.open_at(breakfast, 7) and not city.open_at(breakfast, 15)
    bbq = next(v for v in city.data(w)["venues"] if v.get("open") and v["open"][1] > 24)
    assert city.open_at(bbq, 0.5)  # 烧烤开到后半夜


def _run(engine, days):
    for i in range(days * 3):
        engine.advance("w", Advance(request_id=f"s{i}", seconds=8 * 3600, max_events=2000))
    return [e for e in engine.events("w", limit=50000)["items"] if e["actor_id"] == "neno"]


def test_she_goes_to_real_places_that_are_open(engine):
    events = _run(engine, 6)
    visits = [e for e in events if e["kind"] == "life.visit"]
    assert len({e["payload"]["venue"] for e in visits}) >= 5
    assert any(e["payload"]["visits"] == 1 for e in visits) and any(e["payload"]["visits"] > 1 for e in visits)
    w = _w(engine)
    offset = w["settings"]["utc_offset_minutes"] * 60
    for e in visits:
        hour = ((e["sim_time"] + offset) % 86400) / 3600
        assert city.open_at(city.data(w)["by_id"][e["payload"]["venue"]], hour), e
    routes = [e for e in events if e["kind"] == "activity.started" and e["payload"]["activity"] == "commute"]
    assert routes
    likes = w["settings"]["life_venues"]["neno"]
    assert all(-1 <= v["like"] <= 1 for v in likes.values())


def test_impressions_follow_the_place_not_just_luck(engine):
    """同一家店去多了，印象主要看店本身（隐藏品质），不是每次掷骰子。"""
    from app.services.world.randomness import sample
    _run(engine, 10)
    w = _w(engine)
    seen = {vid: m for vid, m in w["settings"]["life_venues"]["neno"].items() if m["visits"] >= 2}
    assert seen
    agree = sum((m["like"] > 0) == (sample(w["seed"], vid, "venue-quality") > 0.5) for vid, m in seen.items())
    assert agree / len(seen) >= 0.7
    favourite = max(seen.items(), key=lambda kv: kv[1]["visits"])[0]
    assert sample(w["seed"], favourite, "venue-quality") > 0.4  # 常去的那家多半是真好吃


def test_city_replays_from_seed(tmp_path):
    def picks(name):
        e = WorldEngine(WorldStore(tmp_path / name))
        e.create(WorldCreate(world_id="w", seed="replay", template="life", rhythms="life", start_at=START))
        return [(x["sim_time"], x["payload"]["venue"]) for x in _run(e, 2) if x["kind"] == "life.visit"]
    a, b = picks("a.db"), picks("b.db")
    assert a == b and len(Counter(v for _, v in a)) >= 2


def _at_home(c, w, hours_later=0.0):
    from app.storage.world_store import entity, save_entity
    w["sim_time"] += hours_later * 3600
    neno = entity(c, "w", "neno")
    neno["state"].pop("doing", None)
    neno["parent_id"] = "my_room"
    save_entity(c, neno)
    return entity(c, "w", "neno")


def test_no_long_trip_when_time_is_short(engine):
    """离下一件事只剩 40 分钟：不会跑去市区，楼下买杯奶茶还行。"""
    from app.services.world import life
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        neno = _at_home(c, w, 6)  # 13:00
        ids = lambda room: {ch["id"] for ch, _, _ in life._candidates(c, w, "neno", neno, "k", room)}  # noqa: E731
        assert "go_out" in ids(8.0)
        short = ids(0.7)
        assert "go_out" not in short and "milk_tea" in short


def test_she_already_knows_her_neighbourhood(engine):
    """她在这片住了几年：家附近的店多半知道，好吃的去得多；远的知道得少。"""
    from app.services.world import life
    from app.services.world.randomness import sample
    w = _w(engine)
    memory = life._venue_memory(w, "neno")
    home = city.point(w, "my_room")
    near = [v for v in city.nearby(w, "my_room", list(life._EVERYDAY), 600, limit=200)]
    far = [v for v in city.nearby(w, "my_room", list(life._EVERYDAY), 1500, limit=200)
           if city.dist(home, (v["lat"], v["lon"])) > 1100]
    share = lambda vs: sum(v["id"] in memory for v in vs) / len(vs)  # noqa: E731
    assert share(near) > share(far) + 0.2
    good = [m["visits"] for vid, m in memory.items() if sample(w["seed"], vid, "venue-quality") > 0.6]
    bad = [m["visits"] for vid, m in memory.items() if sample(w["seed"], vid, "venue-quality") < 0.4]
    assert sum(good) / len(good) > sum(bad) / len(bad)
    assert all(m["first"] is None for m in memory.values())  # 不是"第一次去"


def test_just_ate_there_wants_somewhere_else(engine):
    from app.services.world import life
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        _at_home(c, w, 5)  # 12:00
        choice = next(ch for ch in w["settings"]["rhythms"]["choices"] if ch["id"] == "eat_out")
        first, _ = life._pick_venue(w, "neno", choice, "my_room", 12, "k1")
        life._venue_memory(w, "neno").setdefault(first, {"like": 0.0, "visits": 1, "first": None})["last"] = w["sim_time"]
        again, _ = life._pick_venue(w, "neno", choice, "my_room", 12, "k1")
    assert again != first


def test_one_meal_at_a_time(engine):
    """刚在外面吃过，两个多小时内不会回家再吃一顿（零食不算）。"""
    from app.services.world import life
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        neno = _at_home(c, w, 5)
        w["settings"].setdefault("life_cooldown", {}).setdefault("neno", {})["group:meal"] = w["sim_time"] + 2.5 * 3600
        ids = {ch["id"] for ch, _, _ in life._candidates(c, w, "neno", neno, "k", 8)}
    assert not ids & {"eat_home", "eat_out", "cook", "eat_canteen"}


def test_first_visit_impression_mostly_reflects_the_place(engine):
    """头一回去一家店，觉得好不好大半看店本身，小半看那天心情。"""
    from app.services.world import life
    from app.services.world.randomness import sample
    from app.storage.world_store import entity, save_entity
    agree = total = 0
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        memory = life._venue_memory(w, "neno")
        for v in city.nearby(w, "my_room", ["noodle", "snack", "restaurant", "fast_food"], 3000, limit=60):
            city.ensure_place(c, w, v["id"])
            neno = entity(c, "w", "neno")
            neno["parent_id"] = v["id"]
            save_entity(c, neno)
            memory[v["id"]] = {"like": 0.0, "visits": 1, "first": w["sim_time"]}
            life._venue_taste(c, w, "neno", "到时间了")
            quality = sample(w["seed"], v["id"], "venue-quality")
            if abs(quality - 0.5) > 0.15:
                total += 1
                agree += (memory[v["id"]]["like"] > 0) == (quality > 0.5)
    assert total >= 20 and agree / total >= 0.85


def test_long_bike_rides_lose_to_the_metro(engine):
    """骑车 50 多分钟到河坊街 vs 地铁差不多时间：坐地铁。近处还是骑车。"""
    w = _w(engine)
    far = next(v for v in city.data(w)["venues"] if v["name"] == "河坊街")
    assert city.route(w, "my_room", far["id"])["mode"] == "metro"
    assert city.route(w, "my_room", "library")["mode"] == "bike"


# ── 多城市 MC1：一个城市设置决定地图、事件池、同城活动的城市名 ─────────────────

def test_one_city_setting_decides_map_events_and_local_listings(monkeypatch):
    import json
    from pathlib import Path

    from app.services.world import rhythms

    monkeypatch.delenv("WORLD_CITY", raising=False)
    assert city.packs() == ["hangzhou"] and city.default_name() == "hangzhou"
    cfg = rhythms.load("life")
    assert cfg["city"]["name"] == "hangzhou" and cfg["real"]["city"] == "hangzhou"
    assert cfg["happenings"]["pool"] == ["common", "hangzhou"]              # 全国通用的 + 这座城特有的
    raw = Path(rhythms.CONTENT / "life.json").read_text(encoding="utf-8")
    assert "hangzhou" not in raw                                            # 作息配置里不再写死哪座城
    beijing = {"name": "beijing", "label": "北京", "douban": "beijing", "events": ["common", "beijing"]}
    monkeypatch.setattr(city, "pack", lambda name: beijing if name == "beijing" else pytest.fail(name))
    other = rhythms.load("life", "beijing")
    assert (other["city"]["name"], other["real"]["city"], other["happenings"]["pool"]) == ("beijing", "beijing", ["common", "beijing"])
    assert other["city"]["places"] == cfg["city"]["places"]                 # 锚点名各城市一样，坐标由城市包的地图给
    assert {k: v for k, v in other.items() if k not in ("city", "real", "happenings")} == {
        k: v for k, v in cfg.items() if k not in ("city", "real", "happenings")}
    monkeypatch.setenv("WORLD_CITY", "Beijing")
    assert rhythms.load("life")["city"]["name"] == "beijing"                # 新建世界时的默认城市看环境变量
    assert "city" not in rhythms.load("neighborhood")                       # 不是生活世界的配置：原样


def test_unknown_city_is_refused_and_pack_carries_what_the_builder_needs():
    with pytest.raises(ValueError, match="没有这座城市的城市包"):
        city.pack("atlantis")
    info = city.pack("hangzhou")
    assert info["label"] == "杭州" and info["douban"] == "hangzhou" and info["timezone"] == "Asia/Shanghai"
    assert {"home", "campus", "life_radius", "metro_bbox", "downtown_bbox", "famous", "version"} <= set(info["build"])
    assert city.load("hangzhou")["city"] == "杭州" and city.load("hangzhou")["version"] == info["build"]["version"]


def test_world_keeps_the_city_it_was_built_in(engine, monkeypatch):
    from app.services.world import happenings
    from app.services.world.runtime import _ensure_life_world

    w = _w(engine)
    assert w["settings"]["city"] == "hangzhou" and len(happenings.templates(w)) == 34
    assert {"lecture", "metro_delay", "marathon", "real_exhibition"} <= set(happenings.templates(w))
    monkeypatch.setenv("WORLD_CITY", "beijing")                             # 后来改了环境变量：她不会因此搬家
    _ensure_life_world(engine, "w")
    assert _w(engine)["settings"]["city"] == "hangzhou" and _w(engine)["settings"]["rhythms"]["city"]["name"] == "hangzhou"
    with engine.store.transaction(write=True) as c:                         # 多城市之前建的世界：没记城市
        old = world(c, "w")
        old["settings"].pop("city")
        engine._save_world(c, old)
    _ensure_life_world(engine, "w")
    assert _w(engine)["settings"]["city"] == "hangzhou"

