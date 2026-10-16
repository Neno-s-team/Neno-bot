"""Short isolated decision scenarios; no provider, live database or Telegram access."""
from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.world.cognition import WorldCognition
from app.services.world.cognition_config import WorldLLMConfig
from app.services.world.cognition_models import CognitionSettings, CognitionUpdate
from app.services.world.engine import WorldEngine
from app.services.world.models import ActorControl, Advance, Control, RandomSettings, WorldCreate
from app.storage.world_store import WorldStore


def run():
    checks, calls = [], []
    config = WorldLLMConfig(enabled=True, model="fixture", url="http://fixture.invalid", api_key="fixture",
                            max_calls_per_day=48, min_call_interval_seconds=1)
    with TemporaryDirectory(prefix="world-cognition-") as directory:
        engine = WorldEngine(WorldStore(Path(directory) / "world.db"))
        world_count = 0

        def setup(reply, *, director=False):
            nonlocal world_count
            world_count += 1
            wid = "case" + str(world_count)
            engine.create(WorldCreate(world_id=wid, running=True, random=RandomSettings(probability=0)))
            for aid in ("neighbor", "shopkeeper"):
                engine.actor_control(wid, aid, ActorControl(request_id="pause-" + aid, active=False))

            def model(messages):
                context = json.loads(messages[-1]["content"])
                calls.append(context)
                # Opening another writer proves the provider call is outside the DB transaction.
                with engine.store.transaction(write=True) as c:
                    c.execute("SELECT 1")
                assert "screwdriver_1" not in json.dumps(context, ensure_ascii=False)
                assert "stock_bread" not in json.dumps(context, ensure_ascii=False)
                if 'action_menu' in context:
                    assert all(a['target_id'] != 'table_1' for a in context['action_menu'] if a['operation'] == 'move')
                if context.get('action_menu') and reply.get('action'):
                    def key(step):
                        return next((a['key'] for a in context['action_menu'] if a['operation'] == step['operation']
                                     and a['target_id'] == step['target_id'] and a['params'] == step.get('params', {})), None)
                    first = key(reply['action'])
                    following = [key(x) for x in reply.get('follow_up_actions', [])]
                    if first and all(following):
                        wire = {k:v for k,v in reply.items() if k not in {'action','follow_up_actions'}}
                        wire.update(action_key=first, follow_up_keys=following)
                        return json.dumps(wire, ensure_ascii=False)
                return json.dumps(reply, ensure_ascii=False)

            service = WorldCognition(engine, config, model)
            service.configure(wid, CognitionUpdate(request_id="enable", settings=CognitionSettings(enabled=True,
                              director_enabled=director, director_probability=1)))
            return wid, service

        now = time.time()

        def advance(wid, request_id, seconds):
            engine.control(wid, Control(request_id=request_id + '-pause', operation='pause'))
            engine.advance(wid, Advance(request_id=request_id, seconds=seconds))
            engine.control(wid, Control(request_id=request_id + '-resume', operation='resume'))

        plan = {"summary": "把书拿起来读完，再放回桌子。", "plan": ["拿书", "阅读", "归位"],
                "new_goal": {"title": "读书并归位", "motivation": "读一会儿书并保持桌面整洁",
                    "conditions": [{"entity_id": "book_1", "field": "state.read_count", "equals": 1},
                                   {"entity_id": "book_1", "field": "parent_id", "equals": "table_1"}]},
                "action": {"operation": "take", "target_id": "book_1"},
                "follow_up_actions": [{"operation": "read", "target_id": "book_1"},
                                      {"operation": "put", "target_id": "book_1", "params": {"destination_id": "table_1"}}]}
        wid, service = setup(plan)
        result = service.step(now)
        assert result["status"] == "applied", result
        assert engine.get_entity(wid, "book_1")["state"]["read_count"] == 0
        assert len(calls) == 1
        # Restart the decision service before following the persisted plan.
        service = WorldCognition(engine, config, lambda _: (_ for _ in ()).throw(AssertionError("Unexpected extra model call")))
        for index, duration in enumerate((5, 900, 5)):
            advance(wid, f"progress-{index}", duration)
            if index < 2:
                service.step(now + 0.1)
        assert engine.get_entity(wid, "book_1")["state"]["read_count"] == 1
        assert engine.get_entity(wid, "book_1")["parent_id"] == "table_1"
        assert engine.records(wid, "threads")["items"][0]["status"] == "done"
        assert len(engine.records(wid, "actions")["items"]) == 3
        engine.control(wid, Control(request_id="stop", operation="pause"))
        checks.append("one grounded model decision -> persisted three-step plan -> measured completion after restart")
        checks.append("hidden contents excluded and no database lock held across provider call")

        wid, service = setup({"summary": "错误地猜测盒子里的工具", "action": {"operation": "take", "target_id": "screwdriver_1"}})
        result = service.step(now + 2)
        assert result["status"] == "failed" and "unobserved" in result["reason"], result
        assert not engine.records(wid, "actions")["items"]
        engine.control(wid, Control(request_id="stop", operation="pause"))
        checks.append("unobserved entity references rejected without world effects")

        wid, service = setup({"summary": "先看看周围", "action": {"operation": "inspect", "target_id": "living"}})
        restricted = WorldCognition(engine, replace(config, max_calls_per_day=2), service.model_call)
        assert restricted.step(now + 4)["idle"] == "daily_call_limit"
        assert len(calls) == 2
        claim = service._claim(now + 4)
        assert service._claim(now + 6)["idle"] == "request_in_flight"
        engine.control(wid, Control(request_id="stop", operation="pause"))
        stopped = service._finish(claim, json.dumps({"summary": "等一会儿"}))
        assert stopped["status"] == "failed" and "paused" in stopped["reason"], stopped
        assert not engine.records(wid, "actions")["items"]
        checks.append("durable daily limit, single request lease, and pause while waiting for model")

        idea = {"kind": "opportunity", "summary": "给当前角色一个可选择的日常方向",
                "region_id": "living", "actor_id": "neno", "title": "整理身边的空间",
                "suggestion": "可以看看身边哪些东西需要整理，不必马上做。"}
        wid, service = setup(idea, director=True)
        advance(wid, "event-time", 7200)
        result = service.step(now + 8)
        assert result["status"] == "applied" and result["kind"] == "opportunity", result
        state = service.status(wid)
        neno = next(x for x in state["actors"] if x["actor_id"] == "neno")
        assert neno["opportunities"][0]["status"] == "unverified_idea"
        assert "整理身边的空间" not in json.dumps(engine.knowledge(wid, "neno"), ensure_ascii=False)
        assert not engine.records(wid, "actions")["items"]
        checks.append("director idea respects random gate/budget and is not a factual memory or forced action")
        previous = engine.snapshot(wid)["event_cursor"]
        service.status(wid); service.decisions(wid); service.decisions(wid, include_context=True)
        assert engine.snapshot(wid)["event_cursor"] == previous
        checks.append("inspection endpoints do not advance time, draw randomness or call the model")
        engine.control(wid, Control(request_id='stop', operation='pause'))
        wid, service = setup({'summary': '邻居决定先休息一会儿', 'wait_seconds': 3600})
        engine.actor_control(wid, 'neno', ActorControl(request_id='pause-neno', active=False))
        engine.actor_control(wid, 'neighbor', ActorControl(request_id='resume-neighbor', active=True))
        result = service.step(now + 10)
        assert result['status'] == 'applied' and calls[-1]['actor_id'] == 'neighbor', result
        checks.append('an NPC receives independent decisions while Neno is inactive')
        return {"passed": len(checks), "checks": checks, "provider_calls": 0, "chat_connected": False}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
