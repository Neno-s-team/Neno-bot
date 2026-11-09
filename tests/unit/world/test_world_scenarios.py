"""世界后端离线验收：scripts/verify_world_*.py 的完整场景纳入 pytest（临时库、不调模型）。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_world_backend_scenarios():
    result = _load("verify_world_backend").run()
    assert result["status"] == "passed"
    assert len(result["scenarios"]) == 10


def test_world_cognition_scenarios():
    result = _load("verify_world_cognition").run()
    assert result["passed"] == 7
    assert result["provider_calls"] == 0


def _engine(tmp_path, world_id="clock"):
    from datetime import datetime
    from app.services.world.engine import WorldEngine
    from app.services.world.models import RandomSettings, WorldCreate
    from app.storage.world_store import WorldStore

    engine = WorldEngine(WorldStore(tmp_path / "world.db"))
    engine.create(WorldCreate(world_id=world_id, running=True, random=RandomSettings(probability=0),
                              start_at=datetime.fromisoformat("2026-09-25T09:00:00+08:00")))
    return engine


def test_idle_clock_does_not_write_every_second(tmp_path):
    engine = _engine(tmp_path)
    start = engine.snapshot("clock")["world"]
    for second in range(1, 31):
        engine.tick(start["last_wall"] + second)
    idle = engine.snapshot("clock")["world"]
    assert idle["version"] == start["version"] and idle["sim_time"] == start["sim_time"]

    engine.tick(start["last_wall"] + 61)
    persisted = engine.snapshot("clock")["world"]
    assert abs(persisted["sim_time"] - start["sim_time"] - 61) < 0.01
    assert persisted["version"] == start["version"]


def test_due_job_is_processed_on_time_without_idle_persist(tmp_path):
    from app.services.world.models import ActionRequest

    engine = _engine(tmp_path)
    engine.submit_action("clock", ActionRequest(request_id="r", actor_id="neno", operation="read",
                                                target_id="book_1", duration_seconds=10))
    base = engine.snapshot("clock")["world"]
    engine.tick(base["last_wall"] + 5)
    assert engine.get_entity("clock", "book_1")["state"]["read_count"] == 0
    engine.tick(base["last_wall"] + 11)
    assert engine.get_entity("clock", "book_1")["state"]["read_count"] == 1
    assert engine.snapshot("clock")["world"]["version"] == base["version"] + 1


def test_actor_context_marks_old_observations(tmp_path):
    from app.services.world.cognition import WorldCognition
    from app.services.world.cognition_config import WorldLLMConfig
    from app.services.world.models import Advance, Control
    from app.storage.world_store import world

    engine = _engine(tmp_path)
    engine.control("clock", Control(request_id="p", operation="pause"))
    engine.advance("clock", Advance(request_id="two-hours", seconds=7200))
    service = WorldCognition(engine, WorldLLMConfig(enabled=True, model="m", url="http://x.invalid", api_key="k"))
    with engine.store.transaction() as c:
        w = world(c, "clock")
        context = service._actor_context(c, w, "neno", service._mind(c, w, "neno"))
    by_id = {x["fact"]["entity_id"]: x for x in context["observations"]}
    assert by_id["book_1"]["seen_minutes_ago"] == 120 and by_id["book_1"]["may_have_changed"] is True
    assert by_id["living"]["seen_minutes_ago"] == 120 and "may_have_changed" not in by_id["living"]
    assert "observed_at" not in by_id["book_1"]


def test_provider_failures_back_off_exponentially(tmp_path):
    from app.services.world.cognition import WorldCognition
    from app.services.world.cognition_config import WorldLLMConfig

    engine = _engine(tmp_path)
    service = WorldCognition(engine, WorldLLMConfig(enabled=True, model="m", url="http://x.invalid", api_key="k",
                                                    min_call_interval_seconds=1, max_calls_per_day=1000))
    t0 = 1_800_000_000.0

    def record(status, at, result='{"provider_error":true}'):
        with engine.store.transaction(write=True) as c:
            c.execute("INSERT INTO world_llm_calls(world_id,kind,subject,status,created_at,sim_time,model,context,result) "
                      "VALUES ('clock','actor','neno',?,?,0,'m','{}',?)", (status, at, result))

    def blocked(at):
        with engine.store.transaction() as c:
            return service._budget(c, at)["blocked"]

    record("failed", t0)
    assert blocked(t0 + 119) == "provider_failure_backoff" and blocked(t0 + 121) is None
    record("failed", t0 + 200)
    assert blocked(t0 + 200 + 239) == "provider_failure_backoff" and blocked(t0 + 200 + 241) is None
    for i in range(6):
        record("failed", t0 + 1000 + i)
    assert blocked(t0 + 1005 + 3599) == "provider_failure_backoff" and blocked(t0 + 1005 + 3601) is None
    record("applied", t0 + 5000)
    assert blocked(t0 + 5002) is None
    record("failed", t0 + 6000, '{"reason":"Action references an unobserved entity"}')
    assert blocked(t0 + 6002) is None
