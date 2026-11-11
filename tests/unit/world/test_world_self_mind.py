"""她在世界里自己拿主意：冲突时刻才问她（不是每个空档）、她选的就是她做的、心里话留下；
别人找她时由她自己回；她没想成就退回规则；有每日上限。世界的一天进她的夜间反思。"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.services.world import life, npcs, self_mind
from app.services.world.engine import WorldEngine
from app.services.world.models import Advance, WorldCreate
from app.storage.world_store import WorldStore, entity, save_entity, world

START = datetime.fromisoformat("2026-11-10T13:00:00+08:00")


@pytest.fixture()
def linked(tmp_path, monkeypatch):
    monkeypatch.setenv("MIND_WORLD_DECISIONS", "true")
    monkeypatch.setenv("MIND_WORLD_DECISIONS_PER_DAY", "6")
    engine = WorldEngine(WorldStore(tmp_path / "w.db"))
    engine.create(WorldCreate(world_id="w", seed="self", template="life", rhythms="life", start_at=START))
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        w["settings"]["mind_linked"] = True
        engine._save_world(c, w)
        c.execute("UPDATE world_jobs SET status='cancelled' WHERE kind IN ('npc_wake','life_choose','perturb')")
    monkeypatch.setattr("app.services.mind.intents.boosts", lambda now=None: {})
    monkeypatch.setattr(self_mind, "_plans", lambda: [])
    return engine


def _sick(engine):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        neno = entity(c, "w", "neno")
        neno["state"].pop("doing", None)
        neno["state"]["conditions"] = [{"label": "感冒了，嗓子疼", "until": w["sim_time"] + 86400}]
        save_entity(c, neno)


def _choose(engine, key="k1"):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        life.choose(c, w, {"job_id": key, "payload": {"actor_id": "neno"}, "run_at": w["sim_time"]})
        engine._save_world(c, w)


def _calls(engine, kind):
    with engine.store.transaction() as c:
        return [dict(r) for r in c.execute("SELECT * FROM world_llm_calls WHERE kind=? ORDER BY id", (kind,))]


def test_conflict_asks_her_and_her_choice_happens(linked, monkeypatch):
    _sick(linked)
    _choose(linked)
    asked = _calls(linked, "neno_decide")
    assert len(asked) == 1 and "感冒" in json.loads(asked[0]["context"])["reason"]
    options = [o["id"] for o in json.loads(asked[0]["context"])["options"]]
    pick = options[-1]  # 故意选排在最后的那个：落地的必须是她选的，不是规则的首选
    assert pick != options[0]
    monkeypatch.setattr(self_mind, "_request", lambda *a, **k: json.dumps({"choice": pick, "murmur": "算了不出门了"}))
    assert npcs.step(linked.store, max_calls_per_day=100)["status"] == "applied"
    with linked.store.transaction() as c:
        neno = entity(c, "w", "neno")
    assert neno["state"]["doing"]["source"] == "life:" + pick
    assert neno["state"]["last_thought"]["text"] == "算了不出门了"


def test_plain_moments_do_not_ask(linked):
    with linked.store.transaction(write=True) as c:
        neno = entity(c, "w", "neno")
        neno["state"].pop("doing", None)
        save_entity(c, neno)
    _choose(linked)
    assert _calls(linked, "neno_decide") == []
    with linked.store.transaction() as c:
        assert entity(c, "w", "neno")["state"].get("doing")


def test_failed_thought_falls_back_to_rules(linked, monkeypatch):
    _sick(linked)
    _choose(linked)

    def broken(*_a, **_k):
        raise RuntimeError("LLM request failed")

    monkeypatch.setattr(self_mind, "_request", broken)
    assert npcs.step(linked.store, max_calls_per_day=100)["status"] == "failed"
    with linked.store.transaction() as c:
        fallback = c.execute("SELECT * FROM world_jobs WHERE kind='life_choose' AND status='pending'").fetchone()
    assert fallback is not None


def test_daily_cap(linked, monkeypatch):
    monkeypatch.setenv("MIND_WORLD_DECISIONS_PER_DAY", "1")
    _sick(linked)
    _choose(linked, "k1")
    _sick(linked)
    _choose(linked, "k2")
    assert len(_calls(linked, "neno_decide")) == 1


def test_she_replies_to_contacts_in_her_own_voice(linked, monkeypatch):
    monkeypatch.setattr(npcs, "_request", lambda *a, **k: json.dumps(
        {"lines": [["A", "下雨了带伞没"], ["B", "（NPC 替她写的，应被丢掉）"], ["A", "吃饭没"]],
         "npc_note": "问了带伞", "neno_note": "x"}, ensure_ascii=False))
    seen = {}

    def her(prompt, schema, name):
        seen["prompt"] = prompt
        return json.dumps({"replies": ["带了", "吃了 别操心"], "note": "妈又来查岗"}, ensure_ascii=False)

    monkeypatch.setattr(self_mind, "_request", her)
    with linked.store.transaction(write=True) as c:
        w = world(c, "w")
        npcs._contact_due(c, w, {"job_id": "npc_contact:mom:1", "payload": {"npc": "mom", "channel": "call", "about": "伞"}})
    npcs.step(linked.store, max_calls_per_day=100)
    npcs.step(linked.store, max_calls_per_day=100)
    assert "下雨了带伞没" in seen["prompt"] and "应被丢掉" not in seen["prompt"]
    contacted = [e for e in linked.events("w", limit=1000)["items"] if e["kind"] == "npc.contacted"][-1]
    assert contacted["payload"]["lines"] == [["A", "下雨了带伞没"], ["B", "带了"], ["A", "吃饭没"], ["B", "吃了 别操心"]]
    assert contacted["payload"]["note"] == "妈又来查岗"


def test_without_flag_world_runs_on_rules(linked, monkeypatch):
    monkeypatch.setenv("MIND_WORLD_DECISIONS", "false")
    _sick(linked)
    _choose(linked)
    assert _calls(linked, "neno_decide") == []


def test_broken_thing_she_wanted_to_use_asks_her(linked):
    """她本来想用的东西坏了，是值得自己想一下的时刻；坏的东西跟想做的事无关就不问。"""
    with linked.store.transaction(write=True) as c:
        w = world(c, "w")
        cfg = w["settings"]["rhythms"]["choices"]
        study = next(ch for ch in cfg if ch["id"] == "study_desk")
        read = next(ch for ch in cfg if ch["id"] == "read_bed")
        options = [(study, "my_room", 2.0), (read, "my_room", 1.0)]
        assert self_mind.conflict(c, w, "neno", options) is None
        laptop = entity(c, "w", "laptop")
        laptop["state"]["broken"] = True
        save_entity(c, laptop)
        assert "电脑坏了" in self_mind.conflict(c, w, "neno", options)
        assert self_mind.conflict(c, w, "neno", [(read, "my_room", 2.0)]) is None
