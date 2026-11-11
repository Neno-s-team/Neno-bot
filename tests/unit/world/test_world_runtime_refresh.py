"""她的生活世界启动时：作息/选项以文件为准刷新，她自己的状态（喜好、习惯、东西坏没坏）不动。"""
from __future__ import annotations

from app.services.world import rhythms
from app.services.world.engine import WorldEngine
from app.services.world.runtime import _ensure_life_world
from app.storage.world_store import WorldStore, entity, save_entity, world


def test_restart_refreshes_rhythms_but_keeps_her_state(tmp_path):
    engine = WorldEngine(WorldStore(tmp_path / "w.db"))
    _ensure_life_world(engine, "life")
    with engine.store.transaction(write=True) as c:
        w = world(c, "life")
        w["settings"]["rhythms"]["choices"] = [ch for ch in w["settings"]["rhythms"]["choices"] if ch["id"] != "fix_item"]
        w["settings"]["life_prefs"] = {"neno": {"cafe": 0.42}}
        laptop = entity(c, "life", "laptop")
        laptop["state"]["broken"] = True
        save_entity(c, laptop)
        engine._save_world(c, w)
    _ensure_life_world(engine, "life")
    with engine.store.transaction() as c:
        w = world(c, "life")
        assert w["settings"]["rhythms"] == rhythms.load("life")
        assert w["settings"]["life_prefs"]["neno"]["cafe"] == 0.42
        assert entity(c, "life", "laptop")["state"]["broken"]


def test_restart_adds_new_template_things_without_touching_old(tmp_path):
    engine = WorldEngine(WorldStore(tmp_path / "w.db"))
    _ensure_life_world(engine, "life")
    with engine.store.transaction(write=True) as c:
        c.execute("DELETE FROM world_entities WHERE world_id='life' AND entity_id='kettle'")
        laptop = entity(c, "life", "laptop")
        laptop["state"]["broken"] = True
        save_entity(c, laptop)
    _ensure_life_world(engine, "life")
    with engine.store.transaction() as c:
        assert entity(c, "life", "kettle")["label"] == "电热水壶"
        assert entity(c, "life", "hair_dryer")["label"] == "吹风机"
        assert entity(c, "life", "laptop")["state"]["broken"]
