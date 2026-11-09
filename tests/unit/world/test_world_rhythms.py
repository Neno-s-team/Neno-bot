"""W3 需求与节律：累积、越阈只产生感觉、作息带抖动、后果按概率、全部可重放。"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.services.world import rhythms
from app.services.world.engine import WorldEngine
from app.services.world.models import ActionRequest, Advance, RandomSettings, WorldCreate
from app.storage.world_store import WorldStore, world

START = datetime.fromisoformat("2026-11-09T07:00:00+08:00")  # 周一


def make(tmp_path, seed="neno-world-1", wid="w", name="w.db"):
    engine = WorldEngine(WorldStore(tmp_path / name))
    engine.create(WorldCreate(world_id=wid, seed=seed, rhythms="neighborhood",
                              random=RandomSettings(probability=0), start_at=START))
    return engine


def run(engine, hours, wid="w"):
    for i in range(int(hours // 8)):
        engine.advance(wid, Advance(request_id=f"step-{i}", seconds=8 * 3600, max_events=2000))
    return [e for e in engine.events(wid, limit=5000)["items"] if e["kind"] not in {"admin.advance", "random.opportunity"}]


def status(engine, aid, wid="w"):
    with engine.store.transaction() as c:
        return rhythms.status(c, world(c, wid), aid)


def test_needs_accumulate_lazily_and_urge_at_drawn_threshold(tmp_path):
    engine = make(tmp_path)
    before = engine.snapshot("w")["world"]["version"]
    with engine.store.transaction() as c:
        row = c.execute("SELECT * FROM world_need WHERE world_id='w' AND actor_id='neno' AND need='hunger'").fetchone()
    hunger = {"urge_at": __import__("json").loads(row["data"])["urge"], "value": row["value"], "rate_per_hour": row["base"]}
    hours_to_urge = (hunger["urge_at"] - hunger["value"]) / hunger["rate_per_hour"]
    events = run(engine, 8)
    urge = next(e for e in events if e["kind"] == "need.urge" and e["actor_id"] == "neno" and e["payload"]["need"] == "hunger")
    assert abs(urge["sim_time"] - (START.timestamp() + hours_to_urge * 3600)) < 1
    assert urge["payload"]["felt"]["饥饿"] == "有点饿"
    assert 0.45 <= hunger["urge_at"] <= 0.7
    assert engine.snapshot("w")["world"]["version"] - before < 60  # 只在有事发生时写


def test_same_seed_replays_different_seed_diverges(tmp_path):
    a = [(e["sim_time"], e["kind"], e["actor_id"]) for e in run(make(tmp_path, "s1", name="a.db"), 48)]
    b = [(e["sim_time"], e["kind"], e["actor_id"]) for e in run(make(tmp_path, "s1", name="b.db"), 48)]
    c = [(e["sim_time"], e["kind"], e["actor_id"]) for e in run(make(tmp_path, "s2", name="c.db"), 48)]
    assert a == b
    assert a != c


def test_routine_jitters_opens_shop_and_pays(tmp_path):
    engine = make(tmp_path)
    assert engine.get_entity("w", "shop")["state"]["open"] is False
    events = run(engine, 48)
    starts = [e for e in events if e["kind"] == "activity.started" and e["payload"]["activity"] == "work"]
    assert len(starts) == 2
    minutes = [datetime.fromtimestamp(e["sim_time"]).hour * 60 + datetime.fromtimestamp(e["sim_time"]).minute for e in starts]
    assert all(8 * 60 + 40 <= m <= 9 * 60 + 20 for m in minutes) and minutes[0] != minutes[1]
    assert engine.get_entity("w", "shop")["state"]["open"] is False
    assert engine.get_entity("w", "shopkeeper")["state"]["credits"] > 100


def test_routine_can_be_skipped(tmp_path):
    skipped = 0
    for seed in range(12):
        events = run(make(tmp_path, f"skip-{seed}", name=f"{seed}.db"), 48)
        skipped += sum(1 for e in events if e["kind"] == "routine.skipped" and e["payload"]["entry"] == "walk")
    assert 0 < skipped < 24  # 有时不去散步，但不是总不去


def test_sleep_mutes_urges_and_restores_fatigue(tmp_path):
    engine = make(tmp_path)
    events = run(engine, 32)
    sleep = next(e for e in events if e["actor_id"] == "neno" and e["kind"] == "activity.started" and e["payload"]["activity"] == "sleep")
    woke = next(e for e in events if e["actor_id"] == "neno" and e["kind"] == "activity.ended" and e["sim_time"] > sleep["sim_time"])
    assert not [e for e in events if e["actor_id"] == "neno" and e["kind"] == "need.urge"
                and sleep["sim_time"] < e["sim_time"] < woke["sim_time"]]
    assert engine.get_entity("w", "neno")["parent_id"] == "bedroom"


def test_eating_relieves_hunger_with_variation(tmp_path):
    engine = make(tmp_path)
    run(engine, 8)
    for n, op in enumerate([("move", "kitchen", {}), ("open", "kitchen_cabinet", {})]):
        engine.submit_action("w", ActionRequest(request_id=f"prep{n}", actor_id="neno", operation=op[0], target_id=op[1], params=op[2], duration_seconds=1))
        engine.advance("w", Advance(request_id=f"prep-t{n}", seconds=2))
    before = status(engine, "neno")["needs"]["hunger"]["value"]
    engine.submit_action("w", ActionRequest(request_id="eat", actor_id="neno", operation="consume", target_id="egg_1", params={"quantity": 1}, duration_seconds=1))
    engine.advance("w", Advance(request_id="eat-t", seconds=2))
    after = status(engine, "neno")["needs"]["hunger"]["value"]
    relief = before - after
    assert 0.075 - 0.001 <= relief <= 0.125 + 0.001
    assert abs(relief - 0.1) > 1e-6  # 同样一个蛋，每次缓解多少不完全一样


def test_strain_consequence_is_probabilistic(tmp_path):
    outcomes = []
    for seed in range(6):
        events = run(make(tmp_path, f"strain-{seed}", name=f"s{seed}.db"), 48)
        draws = [e for e in events if e["kind"] == "need.strain"]
        hits = [e for e in events if e["kind"] == "need.consequence"]
        assert len(hits) == sum(e["payload"]["happened"] for e in draws)
        outcomes += [e["payload"]["happened"] for e in draws]
    assert True in outcomes and False in outcomes  # 越线不一定出事


def test_actor_sees_feelings_and_activity_choices(tmp_path):
    from app.services.world.cognition import WorldCognition
    from app.services.world.cognition_config import WorldLLMConfig

    engine = make(tmp_path)
    run(engine, 8)
    service = WorldCognition(engine, WorldLLMConfig(enabled=True, model="m", url="http://x.invalid", api_key="k"))
    with engine.store.transaction() as c:
        w = world(c, "w")
        context = service._actor_context(c, w, "neno", service._mind(c, w, "neno"))
    assert context["body"] == {"饥饿": "有点饿"} or "饥饿" in context["body"]
    acts = {a["params"]["activity"]: a["available_now"] for a in context["action_menu"] if a["operation"] == "activity"}
    assert acts["rest"] is True and acts["chat"] is False and "work" not in acts

    engine.submit_action("w", ActionRequest(request_id="nap", actor_id="neno", operation="activity",
                                            target_id="neno", params={"activity": "rest"}))
    engine.advance("w", Advance(request_id="nap-t", seconds=1))
    assert engine.get_entity("w", "neno")["state"]["doing"]["activity"] == "rest"
    engine.submit_action("w", ActionRequest(request_id="go", actor_id="neno", operation="move", target_id="kitchen", duration_seconds=1))
    engine.advance("w", Advance(request_id="go-t", seconds=2))
    assert "doing" not in engine.get_entity("w", "neno")["state"]


def test_world_without_rhythms_is_unchanged(tmp_path):
    engine = WorldEngine(WorldStore(tmp_path / "plain.db"))
    engine.create(WorldCreate(world_id="p", random=RandomSettings(probability=0), start_at=START))
    with engine.store.transaction() as c:
        assert c.execute("SELECT count(*) FROM world_need").fetchone()[0] == 0
        assert rhythms.felt(c, world(c, "p"), "neno") == {}
