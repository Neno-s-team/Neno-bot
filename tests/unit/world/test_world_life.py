"""她的一天（D1 种子世界 + D2 空档挑事）：义务按时、挑的事不越过义务、可重放、做不成就先不想、习惯会积累。"""
from __future__ import annotations

from datetime import datetime

from app.services.world.engine import WorldEngine
from app.services.world.models import Advance, WorldCreate
from app.storage.world_store import WorldStore, world

START = datetime.fromisoformat("2026-11-10T07:00:00+08:00")  # 周二


def make(tmp_path, seed="life-1", name="w.db"):
    engine = WorldEngine(WorldStore(tmp_path / name))
    engine.create(WorldCreate(world_id="w", seed=seed, template="life", rhythms="life", start_at=START))
    return engine


def run(engine, days):
    for i in range(days * 3):
        engine.advance("w", Advance(request_id=f"s{i}", seconds=8 * 3600, max_events=2000))
    return [e for e in engine.events("w", limit=50000)["items"] if e["actor_id"] == "neno"]


def _local(ts):
    return datetime.fromtimestamp(ts)


def test_obligations_happen_with_commute_and_choices_fill_the_gaps(tmp_path):
    events = run(make(tmp_path), 3)
    starts = [e for e in events if e["kind"] == "activity.started"]
    cls = next(e for e in starts if e["payload"]["activity"] == "class")
    commute = [e for e in starts if e["payload"]["activity"] == "commute" and e["payload"]["source"] == "routine:class"]
    assert commute and commute[0]["sim_time"] < cls["sim_time"]
    assert 8 * 60 + 50 <= _local(cls["sim_time"]).hour * 60 + _local(cls["sim_time"]).minute <= 9 * 60 + 10
    assert any(e["payload"]["activity"] == "meeting" for e in starts)
    assert sum(1 for e in starts if e["payload"]["activity"] == "sleep") >= 2
    chosen = [e for e in starts if e["payload"]["source"].startswith("life:") and e["payload"]["activity"] != "commute"]
    assert len({e["payload"]["source"] for e in chosen}) >= 6  # 不是天天一个样


def test_chosen_things_end_before_next_obligation(tmp_path):
    events = run(make(tmp_path, "gap"), 3)
    starts = [e for e in events if e["kind"] == "activity.started"]
    duty = [e for e in starts if e["payload"]["source"].startswith("routine:") and e["payload"]["activity"] != "commute"]
    for e in starts:
        if not e["payload"]["source"].startswith("life:"):
            continue
        nxt = next((d for d in duty if d["sim_time"] > e["sim_time"]), None)
        if nxt is not None:
            assert e["payload"]["until"] <= nxt["sim_time"] + 60


def test_replay_and_seed_divergence(tmp_path):
    pick = lambda ev: [(e["sim_time"], e["payload"].get("source")) for e in ev if e["kind"] == "activity.started"]  # noqa: E731
    a, b, c = run(make(tmp_path, "r", "a.db"), 2), run(make(tmp_path, "r", "b.db"), 2), run(make(tmp_path, "q", "c.db"), 2)
    assert pick(a) == pick(b) and pick(a) != pick(c)


def test_blocked_choice_is_not_retried_immediately(tmp_path):
    events = run(make(tmp_path, "block"), 3)
    blocked = [e for e in events if e["kind"] == "life.blocked" and e["payload"].get("choice")]
    for x, y in zip(blocked, blocked[1:]):
        if x["payload"]["choice"] == y["payload"]["choice"]:
            assert y["sim_time"] - x["sim_time"] >= 3 * 3600 - 1


def test_habits_accumulate_and_company_is_required(tmp_path):
    engine = make(tmp_path, "habit")
    events = run(engine, 4)
    with engine.store.transaction() as c:
        habits = world(c, "w")["settings"]["life_habits"]["neno"]
    assert max(habits.values()) > 2
    for e in [e for e in events if e["kind"] == "activity.started" and e["payload"]["activity"] == "chat"]:
        assert e["payload"]["source"] in {"life:chat_mom", "habit"}


def _choose_at(engine, hour, minute=0):
    """把她放到空闲状态、指定时刻，直接跑一次挑事，返回 life.chose 事件。"""
    from app.services.world import life
    from app.storage.world_store import entity, save_entity
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        neno = entity(c, "w", "neno")
        neno["state"].pop("doing", None)
        neno["parent_id"] = "my_room"
        save_entity(c, neno)
        c.execute("UPDATE world_jobs SET status='cancelled' WHERE world_id='w' AND status='pending' AND kind!='routine'")
        key = f"life:neno:probe:{hour}:{minute}"
        life.choose(c, w, {"job_id": key, "payload": {"actor_id": "neno"}, "run_at": w["sim_time"]})
        engine._save_world(c, w)
        row = c.execute("SELECT payload FROM world_events WHERE world_id='w' AND event_key=?", (key + ":chose",)).fetchone()
    import json
    return json.loads(row[0]) if row else None


def test_noise_changes_ranking_between_draws(tmp_path):
    """同一时刻、同样状态，只换抽签 key：先后顺序会变（噪声在起作用）。"""
    import json
    from app.services.world import life
    from app.storage.world_store import entity
    engine = make(tmp_path, "noise")
    run(engine, 1)
    orders = set()
    with engine.store.transaction(write=True) as c:
        import copy
        base = world(c, "w")
        c.execute("SAVEPOINT probe")
        for n in range(10):
            w = copy.deepcopy(base)  # 习惯/冷却等内存状态每次都从同一起点开始
            c.execute("UPDATE world_entities SET state=json_remove(state,'$.doing') WHERE world_id='w' AND entity_id='neno'")
            life.choose(c, w, {"job_id": f"probe{n}", "payload": {"actor_id": "neno"}, "run_at": w["sim_time"]})
            row = c.execute("SELECT payload FROM world_events WHERE event_key=?", (f"probe{n}:chose",)).fetchone()
            orders.add(tuple(x[0] for x in json.loads(row[0])["options"]))
            c.execute("ROLLBACK TO probe")
    assert len(orders) > 1


def test_failed_choice_cools_down(tmp_path, monkeypatch):
    from app.services.world import life
    from app.storage.world_store import WorldError, entity, save_entity
    engine = make(tmp_path, "cool")
    run(engine, 1)

    def broke(*_a, **_k):
        raise WorldError("Not enough money", 409)

    monkeypatch.setattr(life, "_do", broke)
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        neno = entity(c, "w", "neno")
        neno["state"].pop("doing", None)
        save_entity(c, neno)
        life.choose(c, w, {"job_id": "life:neno:cool", "payload": {"actor_id": "neno"}, "run_at": w["sim_time"]})
        chose = c.execute("SELECT payload FROM world_events WHERE event_key='life:neno:cool:chose'").fetchone()
        picked = __import__("json").loads(chose[0])["choice"]
        until = w["settings"]["life_cooldown"]["neno"][picked]
        assert until >= w["sim_time"] + 3 * 3600 - 1
        assert picked not in [ch["id"] for ch, _, _ in life._candidates(c, w, "neno", entity(c, "w", "neno"))]


def test_chat_needs_someone_awake_there(tmp_path):
    from app.services.world import life
    from app.storage.world_store import entity, save_entity
    engine = make(tmp_path, "company")
    run(engine, 1)
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        w["sim_time"] += 6 * 3600  # 13:00，chat_mom 的时段内
        neno = entity(c, "w", "neno")
        neno["state"].pop("doing", None)
        neno["parent_id"] = "home_living"
        save_entity(c, neno)

        def ids():
            return [ch["id"] for ch, _, _ in life._candidates(c, w, "neno", entity(c, "w", "neno"))]

        for parent in ("mom", "dad"):
            p = entity(c, "w", parent)
            p["parent_id"] = "parents_room"
            save_entity(c, p)
        assert "chat_mom" not in ids()
        mom = entity(c, "w", "mom")
        mom["parent_id"] = "home_living"
        mom["state"].pop("doing", None)
        save_entity(c, mom)
        assert "chat_mom" in ids()
