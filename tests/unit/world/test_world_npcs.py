"""W4 NPC：醒来排一天（只排队不调模型）、到点找她（她忙就晚点，打电话没接后来回过去）、
对话落地两边各记一笔；关系锁过滤她的感情线；预算与失败退避；模型不在事务里。"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.services.world import npcs
from app.services.world.engine import WorldEngine
from app.services.world.models import Advance, WorldCreate
from app.storage.world_store import WorldStore, entity, save_entity, world

START = datetime.fromisoformat("2026-11-10T07:00:00+08:00")


def make(tmp_path, seed="npc"):
    engine = WorldEngine(WorldStore(tmp_path / "w.db"))
    engine.create(WorldCreate(world_id="w", seed=seed, template="life", rhythms="life", start_at=START))
    return engine


def calls(engine, kind=None, status=None):
    with engine.store.transaction() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM world_llm_calls ORDER BY id")]
    return [r for r in rows if (kind is None or r["kind"] == kind) and (status is None or r["status"] == status)]


def fake(monkeypatch, plan=None, contact=None):
    sent = []

    def request(messages, schema, name, max_tokens):
        sent.append(name)
        if name == "npc_day":
            return json.dumps(plan or {"day": ["08:00 上班"], "contact_time": "19:30", "contact_channel": "call",
                                       "contact_about": "问吃饭没", "memory_note": "女儿忙论文"}, ensure_ascii=False)
        return json.dumps(contact or {"lines": [["A", "吃了没"], ["B", "吃了"]], "npc_note": "她挺好",
                                      "neno_note": "妈打电话问吃饭没"}, ensure_ascii=False)

    monkeypatch.setattr(npcs, "_request", request)
    return sent


def drain(engine):
    out = []
    while True:
        result = npcs.step(engine.store, max_calls_per_day=100)
        if "idle" in result:
            return out
        out.append(result)


def test_wake_queues_plan_without_calling_model(tmp_path, monkeypatch):
    sent = fake(monkeypatch)
    engine = make(tmp_path)
    for i in range(3):
        engine.advance("w", Advance(request_id=f"a{i}", seconds=8 * 3600, max_events=2000))
    queued = calls(engine, "npc_plan", "queued")
    assert {r["subject"] for r in queued} == {"mom", "dad", "advisor", "senior"}  # 每人一天醒一次
    assert sent == []  # 推进世界时从不调模型
    drain(engine)
    assert {r["status"] for r in calls(engine, "npc_plan")} == {"applied"}


def test_contact_lands_in_her_state_and_memory(tmp_path, monkeypatch):
    fake(monkeypatch)
    engine = make(tmp_path)
    for i in range(3):
        engine.advance("w", Advance(request_id=f"a{i}", seconds=6 * 3600))
        drain(engine)
    contacted = [e for e in engine.events("w", limit=50000)["items"] if e["kind"] == "npc.contacted"]
    assert contacted
    with engine.store.transaction() as c:
        neno = entity(c, "w", "neno")
        memory = world(c, "w")["settings"]["npc_memory"]
    assert neno["state"]["recent_contacts"][-1]["note"] == "妈打电话问吃饭没"
    assert any("她挺好" in x for x in sum(memory.values(), []))


def test_busy_defers_and_call_becomes_missed(tmp_path, monkeypatch):
    fake(monkeypatch)
    engine = make(tmp_path)
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        w.setdefault("settings", {})
        neno = entity(c, "w", "neno")
        neno["state"]["doing"] = {"activity": "meeting", "label": "组会", "since": w["sim_time"], "until": w["sim_time"] + 7200,
                                  "source": "t", "job": "x"}
        save_entity(c, neno)
        npcs._contact_due(c, w, {"job_id": "npc_contact:mom:1", "payload": {"npc": "mom", "channel": "call", "about": "x"}})
        retry = c.execute("SELECT * FROM world_jobs WHERE job_id LIKE 'npc_contact:mom:1:retry1'").fetchone()
    payload = json.loads(retry["payload"])
    assert payload["missed"] is True and payload["tries"] == 1
    assert 40 * 60 <= retry["run_at"] - START.timestamp() <= 120 * 60
    assert not calls(engine, "npc_contact")


@pytest.mark.parametrize("text,kin,hit", [
    ("师姐说要给你介绍对象", False, True),
    ("妈：想你了宝贝", True, False),
    ("师姐：想你了宝贝", False, True),
    ("导师：第二章再改改", False, False),
])
def test_relationship_lock_filter(text, kin, hit):
    assert npcs.romance_hit(text, kin) is hit


def test_romance_content_is_dropped_and_logged(tmp_path, monkeypatch):
    fake(monkeypatch, contact={"lines": [["A", "我给你介绍个对象吧"], ["B", "不用"]], "npc_note": "x", "neno_note": "师姐要介绍对象"})
    engine = make(tmp_path)
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        npcs._contact_due(c, w, {"job_id": "npc_contact:senior:1", "payload": {"npc": "senior", "channel": "message", "about": "x"}})
    drain(engine)
    row = calls(engine, "npc_contact")[0]
    assert row["status"] == "failed" and "relationship lock" in json.loads(row["result"])["reason"]
    assert [e for e in engine.events("w", limit=1000)["items"] if e["kind"] == "npc.relationship_lock"]
    with engine.store.transaction() as c:
        assert "recent_contacts" not in entity(c, "w", "neno")["state"]


def test_budget_and_backoff(tmp_path, monkeypatch):
    engine = make(tmp_path)
    engine.advance("w", Advance(request_id="a", seconds=4 * 3600))
    assert npcs.step(engine.store, max_calls_per_day=0)["idle"] == "daily_npc_budget"

    def broken(*_a, **_k):
        raise RuntimeError("LLM request failed")

    monkeypatch.setattr(npcs, "_request", broken)
    first = npcs.step(engine.store, max_calls_per_day=100)
    assert first["status"] == "failed" and first["provider_error"] is True
    assert npcs.step(engine.store, max_calls_per_day=100)["idle"] == "provider_failure_backoff"


def test_no_model_call_inside_world_transaction(tmp_path, monkeypatch):
    engine = make(tmp_path)
    engine.advance("w", Advance(request_id="a", seconds=4 * 3600))

    def request(*_a, **_k):
        with engine.store.transaction(write=True) as c:  # 能拿到写锁 = 调模型时没持锁
            c.execute("SELECT 1")
        return json.dumps({"day": [], "contact_time": None, "contact_channel": None, "contact_about": None, "memory_note": ""})

    monkeypatch.setattr(npcs, "_request", request)
    assert npcs.step(engine.store, max_calls_per_day=100)["status"] == "applied"
