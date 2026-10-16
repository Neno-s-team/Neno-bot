"""Run complete world scenarios against temporary SQLite files; no API spending or Telegram."""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.services.world.__main__ import create_app
from app.services.world.engine import WorldEngine
from app.services.world.models import ActionRequest, ActorControl, Advance, Control, EntityCreate, RandomSettings, ThreadRequest, WorldCreate
from app.storage.world_store import WorldError, WorldStore


def run() -> dict:
    completed = []
    start = datetime.fromisoformat("2026-09-25T09:00:00+08:00")
    with TemporaryDirectory(prefix="neno-world-scenarios-") as temporary:
        path = Path(temporary) / "world.db"
        engine = WorldEngine(WorldStore(path))
        engine.create(WorldCreate(world_id="life", start_at=start, random=RandomSettings(probability=0)))
        n = 0

        def perform(operation, target, params=None, thread_id=None, duration=1):
            nonlocal n
            n += 1
            request = ActionRequest(request_id=f"a-{n}", actor_id="neno", operation=operation,
                                    target_id=target, params=params or {}, duration_seconds=duration, thread_id=thread_id)
            started = engine.submit_action("life", request)
            assert engine.submit_action("life", request) == started
            engine.advance("life", Advance(request_id=f"time-{n}", seconds=duration))
            row = next(x for x in engine.records("life", "actions")["items"] if x["action_id"] == started["action_id"])
            return row

        initial = engine.snapshot("life")
        known = engine.knowledge("life", "neno")
        assert "screwdriver_1" not in {x["fact"]["entity_id"] for x in known["items"]}
        assert "stock_bread" not in json.dumps(engine.context_preview("life", "neno"))
        before_reads = engine.snapshot("life")["event_cursor"]
        for _ in range(3):
            engine.snapshot("life"); engine.context_preview("life", "neno")
        assert engine.snapshot("life")["event_cursor"] == before_reads
        completed.append("closed-container knowledge isolation and read-only queries")

        thread = engine.submit_thread("life", ThreadRequest(request_id="repair-goal", actor_id="neno",
            title="准备修好台灯，不等于已完成", conditions=[{"entity_id": "lamp_1", "field": "state.broken", "equals": False}]))
        failed = perform("repair", "lamp_1", {"tool_id": "screwdriver_1", "part_id": "switch_1"}, thread["thread_id"])
        assert failed["status"] == "failed"
        assert engine.get_entity("life", "switch_1")["state"]["quantity"] == 1
        assert engine.records("life", "threads")["items"][0]["status"] == "blocked"
        assert perform("open", "toolbox")["status"] == "completed"
        repaired = perform("repair", "lamp_1", {"tool_id": "screwdriver_1", "part_id": "switch_1"}, thread["thread_id"], 600)
        assert repaired["status"] == "completed"
        assert engine.get_entity("life", "lamp_1")["state"]["broken"] is False
        assert engine.get_entity("life", "switch_1")["state"]["quantity"] == 0
        assert engine.records("life", "threads")["items"][0]["status"] == "done"
        part_knowledge = engine.knowledge("life", "neno", "switch_1")["items"]
        assert any(x["fact"]["state"]["quantity"] == 0 for x in part_knowledge)
        replay = engine.history_view("life", initial["event_cursor"])
        assert next(x for x in replay["entities"] if x["entity_id"] == "lamp_1")["state"]["broken"] is True
        assert engine.get_entity("life", "lamp_1")["state"]["broken"] is False
        completed.append("failed repair, unchanged resources, persistent goal, successful repair and historical replay")

        for op, target, params in [("move", "kitchen", {}), ("open", "kitchen_cabinet", {}),
                                   ("turn_on", "stove_1", {}), ("cook", "pan_1", {"ingredients": ["rice_1", "egg_1"]})]:
            assert perform(op, target, params)["status"] == "completed"
        assert engine.get_entity("life", "rice_1")["state"]["quantity"] == 3
        assert engine.get_entity("life", "egg_1")["state"]["quantity"] == 2
        assert engine.get_entity("life", "pan_1")["state"]["dirty"]
        assert perform("clean", "pan_1")["status"] == "completed"
        assert not engine.get_entity("life", "pan_1")["state"]["dirty"]
        assert perform("turn_off", "stove_1")["status"] == "completed"
        completed.append("cooking consumes ingredients, creates a meal, and leaves washable cookware")

        assert perform("move", "shop")["status"] == "completed"
        assert perform("take", "stock_bread")["status"] == "failed"
        bought = perform("buy", "stock_bread", {"quantity": 2})
        assert bought["status"] == "completed"
        assert engine.get_entity("life", "stock_bread")["state"]["quantity"] == 10
        assert engine.get_entity("life", "neno")["state"]["credits"] == 94
        purchased = next(x for x in bought["result"]["changes"] if x["entity_id"].startswith("item_"))
        assert perform("move", "living")["status"] == "completed"
        assert perform("put", purchased["entity_id"], {"destination_id": "table_1"})["status"] == "completed"
        completed.append("shopping respects ownership, finite stock, payment, transport and placement")

        fresh = WorldEngine(WorldStore(path))
        assert fresh.get_entity("life", purchased["entity_id"])["parent_id"] == "table_1"
        assert fresh.get_entity("life", "lamp_1")["state"]["broken"] is False
        command = Advance(request_id="concurrent-step", seconds=30)
        previous = fresh.snapshot("life")["world"]["sim_time"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: fresh.advance("life", command), range(2)))
        assert results[0] == results[1]
        assert fresh.snapshot("life")["world"]["sim_time"] == previous + 30
        try:
            fresh.advance("life", Advance(request_id="concurrent-step", seconds=31))
        except WorldError as error:
            assert error.status == 409
        else:
            raise AssertionError("Conflicting idempotency payload was accepted")
        completed.append("restart persistence and concurrent command idempotency")

        engine.create(WorldCreate(world_id="independent", start_at=start, random=RandomSettings(probability=0)))
        engine.actor_control("independent", "neno", ActorControl(request_id="pause-neno", active=False))
        engine.advance("independent", Advance(request_id="afternoon", seconds=7 * 3600 + 120))
        assert engine.get_entity("independent", "neighbor")["parent_id"] == "park"
        assert engine.get_entity("independent", "neno")["parent_id"] == "living"
        engine.advance("independent", Advance(request_id="evening", seconds=2 * 3600))
        assert not engine.get_entity("independent", "shop")["state"]["open"]
        assert engine.get_entity("independent", "neighbor")["parent_id"] == "neighbor_home"
        assert "shop" not in {x["fact"]["entity_id"] for x in engine.knowledge("independent", "neno")["items"]}
        completed.append("NPC schedules and shop hours continue while Neno is inactive")

        sequences = []
        for wid in ["seed-a", "seed-b"]:
            engine.create(WorldCreate(world_id=wid, seed="shared-seed", start_at=start,
                          random=RandomSettings(probability=1, cooldown_seconds=0, max_events_per_day=100)))
            engine.advance(wid, Advance(request_id="two-hours", seconds=7200))
            events = engine.events(wid, limit=500)["items"]
            sequences.append([(x["payload"]["draw"], x["payload"]["selected"]) for x in events if x["kind"] == "random.opportunity"])
        assert sequences[0] == sequences[1] and len(sequences[0]) == 2
        try:
            engine.history_view("seed-a", engine.snapshot("seed-b")["event_cursor"])
        except WorldError as error:
            assert error.status == 404
        else:
            raise AssertionError("Cross-world replay accepted")
        completed.append("seeded events reproduce and historical reads stay within a world")

        engine.create(WorldCreate(world_id="clock", start_at=start, running=True))
        before = engine.snapshot("clock")["world"]
        engine.tick(before["last_wall"] + 61)
        after = engine.snapshot("clock")["world"]
        assert abs(after["sim_time"] - before["sim_time"] - 61) < 0.01
        engine.control("clock", Control(request_id="pause", operation="pause"))
        paused_at = engine.snapshot("clock")["world"]["sim_time"]
        engine.tick(before["last_wall"] + 600)
        assert engine.snapshot("clock")["world"]["sim_time"] == paused_at
        completed.append("wall-clock progression and explicit global pause")

        engine.create(WorldCreate(world_id="suspend", start_at=start, random=RandomSettings(probability=0)))
        engine.submit_action("suspend", ActionRequest(request_id="reading", actor_id="neno", operation="read", target_id="book_1", duration_seconds=100))
        engine.advance("suspend", Advance(request_id="first-part", seconds=40))
        engine.actor_control("suspend", "neno", ActorControl(request_id="sleep", active=False))
        engine.advance("suspend", Advance(request_id="world-continues", seconds=3600))
        assert engine.get_entity("suspend", "book_1")["state"]["read_count"] == 0
        assert engine.records("suspend", "actions")["items"][0]["status"] == "paused"
        engine.actor_control("suspend", "neno", ActorControl(request_id="wake", active=True))
        engine.advance("suspend", Advance(request_id="remaining", seconds=60))
        assert engine.get_entity("suspend", "book_1")["state"]["read_count"] == 1
        source_type = next(x for x in engine.catalog.search(limit=100)["items"] if x["source"]["name"] == "BDDL")
        engine.create_entity("suspend", EntityCreate(request_id="catalog-instance", entity_id="external_item", type_id=source_type["type_id"], parent_id="neighbor_home"))
        assert engine.get_entity("suspend", "external_item")["type_id"] == source_type["type_id"]
        assert "external_item" not in json.dumps(engine.knowledge("suspend", "neno"))
        completed.append("actor suspension preserves remaining work and catalog instances do not leak across rooms")

        app = create_app(Path(temporary) / "api.db", admin_token="temporary-smoke-token")
        headers = {"x-admin-token": "temporary-smoke-token"}
        with TestClient(app) as client:
            assert client.get("/world/status").status_code == 403
            assert client.get("/world/status", headers=headers).json()["chat_connected"] is False
            assert client.post("/world/worlds", headers=headers, json={"world_id": "api", "start_at": start.isoformat()}).status_code == 201
            assert client.get("/world/worlds/api/snapshot", headers=headers).status_code == 200
            assert client.get("/world/catalog?q=杯子", headers=headers).json()["total"] >= 1
            bad = client.post("/world/worlds/api/actions", headers=headers, json={"request_id":"bad","actor_id":"neno","operation":"cook","target_id":"pan_1","params":{"ingredients":[{}]}})
            assert bad.status_code == 422
            assert client.get("/world/worlds/api/actors/neno/context-preview", headers=headers).json()["chat_connected"] is False
            assert client.get("/world/worlds/missing/snapshot", headers=headers).status_code == 404
        completed.append("authenticated API, input validation and no chat integration")
    return {"status": "passed", "scenarios": completed, "live_database_touched": False,
            "external_model_calls": 0, "telegram_messages_sent": 0}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
