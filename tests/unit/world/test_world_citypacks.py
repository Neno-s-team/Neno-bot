"""每个城市包都得是完整的：有地图、锚点齐全、模板里的固定地点都能落到坐标上、城市特有的事提到的地方和地铁线在地图里真有。
新加一座城市不用改这里——有了 pack.json 和 map.json 就会被查到。"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.services.world import city, happenings, rhythms
from app.services.world.engine import WorldEngine
from app.services.world.models import WorldCreate
from app.storage.world_store import WorldStore, world

PACKS = city.packs(include_draft=True)      # 草稿也要查：放出来之前就得是完整的


def test_hangzhou_is_always_there_and_drafts_are_not_offered():
    import run

    assert "hangzhou" in city.packs() and ("hangzhou", "杭州") in run.cities()
    drafts = [name for name in PACKS if city.pack(name).get("draft")]
    assert not set(drafts) & set(city.packs()) and not set(drafts) & {name for name, _ in run.cities()}


@pytest.mark.parametrize("name", PACKS)
def test_pack_is_complete(name):
    info, data = city.pack(name), city.load(name)
    assert info["name"] == name and info["label"] == data["city"] and info["douban"] and info["timezone"]
    assert data["version"] == info["build"]["version"] and "OpenStreetMap" in data["attribution"]
    home, campus = data["anchors"]["home"], data["anchors"]["campus"]
    assert home["name"] and home["station"] in data["stations"] and campus["station"] in data["stations"]
    for key in ("east_gate", "west", "library"):
        assert len(campus[key]) == 2
    assert 300 <= city.dist((home["lat"], home["lon"]), campus["east_gate"]) <= 3000      # 家离学校不远不近，骑车能到
    kinds = {v["kind"] for v in data["venues"]}
    assert {"cafe", "convenience", "restaurant", "canteen", "cinema", "mall", "museum", "bookstore"} <= kinds
    assert len(data["venues"]) >= 300 and len(data["stations"]) >= 50 and data["lines"]


@pytest.mark.parametrize("name", PACKS)
def test_fixed_places_and_local_events_exist_on_the_map(name):
    info, data = city.pack(name), city.load(name)
    cfg = rhythms.load("life", name)
    assert cfg["city"]["name"] == name and cfg["real"]["city"] == info["douban"] and cfg["happenings"]["pool"] == info["events"]
    for place, spec in cfg["city"]["places"].items():
        assert city._resolve(data, spec) is not None, f"{name}：模板里的 {place}（{spec}）落不到坐标上"
    names = {v["name"] for v in data["venues"]}
    lines = {line["line"] for line in data["lines"]}
    seen = set()
    for pool in info["events"]:
        templates = happenings.load(pool)
        assert templates, f"事件池 {pool} 是空的"
        assert not seen & set(templates), f"{pool} 和别的池有重名的模板"
        seen |= set(templates)
        for tpl in templates.values():
            where = (tpl.get("place") or {}).get("name")
            assert where is None or where in names, f"{name}：《{tpl['title']}》说的 {where} 不在地图里"
            for line in (tpl.get("slots") or {}).get("line", []):
                assert line in lines, f"{name}：地铁 {line} 不在地图里"
    assert {"lecture", "film", "exhibition", "metro_delay", "new_year_eve", "marathon"} <= seen


@pytest.mark.parametrize("name", PACKS)
def test_she_can_live_a_day_there(name, tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_CITY", name)
    engine = WorldEngine(WorldStore(tmp_path / "w.db"))
    engine.create(WorldCreate(world_id="w", seed=f"pack-{name}", template="life", rhythms="life",
                              start_at=datetime.fromisoformat("2026-11-14T07:00:00+08:00")))
    with engine.store.transaction() as c:
        w = world(c, "w")
    assert w["settings"]["city"] == name and city.pack(name)["label"] in city.home_line(w)
    to_class = city.route(w, "my_room", "seminar_room")
    assert to_class["mode"] in ("bike", "walk", "metro") and 3 <= to_class["minutes"] <= 40
    far = max((v for v in city.data(w)["venues"] if v.get("town")),
              key=lambda v: city.dist(city.point(w, "my_room"), (v["lat"], v["lon"])))
    assert city.route(w, "my_room", far["id"])["minutes"] <= 150
    assert city.anchor_labels(w).get("cafe") and city.anchor_labels(w).get("riverside")
