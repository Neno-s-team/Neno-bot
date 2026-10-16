from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import time

from app.services.world import actions
from app.services.world.catalog import Catalog
from app.services.world.models import ActionRequest, WorldCreate
from app.services.world.randomness import sample, stable_id
from app.services.world.scene_builder import build_scene
from app.storage.world_store import WorldError, WorldStore, decode_entity, emit, encode, entity, job, save_entity, world


class WorldEngine:
    def __init__(self, store: WorldStore):
        self.store = store
        store.initialize()
        self.catalog = Catalog(store)

    @staticmethod
    def _save_world(c, w: dict) -> None:
        c.execute("""UPDATE world_worlds SET sim_time=?,status=?,rate=?,last_wall=?,version=?,settings=?,error=?
            WHERE world_id=?""", (w["sim_time"], w["status"], w["rate"], w["last_wall"], w["version"],
                                   encode(w["settings"]), w.get("error"), w["world_id"]))

    def _command(self, wid: str, request, kind: str, handler) -> dict:
        data = request.model_dump(mode="json")
        fingerprint = hashlib.sha256(encode({"kind": kind, "body": data}).encode()).hexdigest()
        with self.store.transaction(write=True) as c:
            w = world(c, wid)
            old = c.execute("SELECT fingerprint,result FROM world_commands WHERE world_id=? AND request_id=?", (wid, request.request_id)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise WorldError("request_id was used with a different command", 409)
                return json.loads(old["result"])
            result = handler(c, w, data)
            w["version"] += 1
            self._save_world(c, w)
            result["world_version"] = w["version"]
            c.execute("INSERT INTO world_commands VALUES (?,?,?,?)", (wid, request.request_id, fingerprint, encode(result)))
            return result

    def create(self, request: WorldCreate) -> dict:
        settings = {"utc_offset_minutes": request.utc_offset_minutes, "template": request.template,
                    "random": request.random.model_dump(), "random_version": 1, "random_state": {}}
        w = {"world_id": request.world_id, "name": request.name, "seed": request.seed,
             "catalog_version": self.catalog.version, "sim_time": request.start_at.timestamp(),
             "status": "running" if request.running else "paused", "rate": request.rate,
             "last_wall": time.time(), "version": 1, "settings": settings, "error": None}
        with self.store.transaction(write=True) as c:
            if c.execute("SELECT 1 FROM world_worlds WHERE world_id=?", (request.world_id,)).fetchone():
                raise WorldError("World ID already exists", 409)
            c.execute("INSERT INTO world_worlds VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                w["world_id"], w["name"], w["seed"], w["catalog_version"], w["sim_time"], w["status"],
                w["rate"], w["last_wall"], w["version"], encode(settings), None))
            values, template = build_scene(c, self.catalog, w, request.template)
            hour = (w["sim_time"] + request.utc_offset_minutes * 60) % 86400 / 3600
            for value in values:
                if value["entity_id"] == "shop":
                    value["state"]["open"] = 9 <= hour < 18
                save_entity(c, value)
            event_id = emit(c, w, "created", "world.created", {"source": "authored_scene", "template_version": template["version"],
                             "changes": values, "seed": w["seed"], "catalog_version": w["catalog_version"]})
            for value in values:
                if value["kind"] == "actor":
                    actions.observe(c, w, value["entity_id"], event_id, "initial_sight")
            for schedule in template.get("daily_schedules", []):
                when = self._next_hour(w, schedule["hour"])
                job(c, w["world_id"], f"daily:{schedule['id']}:{int(when)}", when, "daily", schedule)
            when = w["sim_time"] + settings["random"]["interval_seconds"]
            job(c, w["world_id"], f"random:1:{int(when)}", when, "random", {})
        return {"world": w, "entities": len(values), "event_id": event_id, "chat_connected": False}

    @staticmethod
    def _next_hour(w: dict, hour: int) -> float:
        offset = w["settings"]["utc_offset_minutes"] * 60
        start = (w["sim_time"] + offset) // 86400 * 86400 - offset
        when = start + hour * 3600
        return when if when > w["sim_time"] else when + 86400

    def list_worlds(self) -> list[dict]:
        with self.store.transaction() as c:
            ids = c.execute("SELECT world_id FROM world_worlds ORDER BY world_id LIMIT 1000").fetchall()
            return [world(c, row[0]) for row in ids]

    def snapshot(self, wid: str, parent_id: str | None = None, limit: int = 200, offset: int = 0) -> dict:
        with self.store.transaction() as c:
            w = world(c, wid)
            clause, args = "world_id=?", [wid]
            if parent_id is not None:
                clause += " AND parent_id=?"
                args.append(parent_id)
            total = c.execute("SELECT count(*) FROM world_entities WHERE " + clause, args).fetchone()[0]
            rows = c.execute("SELECT * FROM world_entities WHERE " + clause + " ORDER BY entity_id LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            cursor = c.execute("SELECT COALESCE(MAX(id),0) FROM world_events WHERE world_id=?", (wid,)).fetchone()[0]
            due = c.execute("SELECT MIN(run_at) FROM world_jobs WHERE world_id=? AND status='pending'", (wid,)).fetchone()[0]
        return {"world": w, "items": [decode_entity(x) for x in rows], "total": total, "offset": offset,
                "event_cursor": cursor, "next_event_at": due, "view": "admin", "chat_connected": False}

    def get_entity(self, wid: str, key: str) -> dict:
        with self.store.transaction() as c:
            world(c, wid)
            return entity(c, wid, key)

    def events(self, wid: str, after: int = 0, limit: int = 100) -> dict:
        with self.store.transaction() as c:
            world(c, wid)
            rows = c.execute("SELECT * FROM world_events WHERE world_id=? AND id>? ORDER BY id LIMIT ?", (wid, after, limit)).fetchall()
        items = [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
        return {"items": items, "next_cursor": items[-1]["id"] if items else after}

    def history_view(self, wid: str, event_id: int) -> dict:
        state, at, count = {}, None, 0
        with self.store.transaction() as c:
            world(c, wid)
            if not c.execute("SELECT 1 FROM world_events WHERE world_id=? AND id=?", (wid, event_id)).fetchone():
                raise WorldError("Historical event does not belong to this world", 404)
            for row in c.execute("SELECT sim_time,payload FROM world_events WHERE world_id=? AND id<=? ORDER BY id", (wid, event_id)):
                payload = json.loads(row["payload"])
                for change in payload.get("changes", []):
                    state[change["entity_id"]] = change
                at, count = row["sim_time"], count + 1
        return {"world_id": wid, "at_event": event_id, "sim_time": at, "events_applied": count,
                "entities": sorted(state.values(), key=lambda x: x["entity_id"]),
                "mode": "read_only_entity_replay", "model_calls": 0}

    def records(self, wid: str, table: str, limit: int = 100, offset: int = 0) -> dict:
        if table not in {"actions", "threads", "jobs"}:
            raise WorldError("Unknown record collection")
        with self.store.transaction() as c:
            world(c, wid)
            total = c.execute(f"SELECT count(*) FROM world_{table} WHERE world_id=?", (wid,)).fetchone()[0]
            rows = c.execute(f"SELECT * FROM world_{table} WHERE world_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?", (wid, limit, offset)).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            for field in ("data", "payload", "result"):
                if value.get(field) is not None:
                    value[field] = json.loads(value[field])
            result.append(value)
        return {"items": result, "total": total, "offset": offset}

    def knowledge(self, wid: str, actor_id: str, query: str = "", limit: int = 100, offset: int = 0) -> dict:
        with self.store.transaction() as c:
            w = world(c, wid)
            if entity(c, wid, actor_id)["kind"] != "actor":
                raise WorldError("Not an actor")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = c.execute("""SELECT * FROM world_knowledge WHERE world_id=? AND actor_id=?
                AND data LIKE ? ESCAPE '\\' ORDER BY observed_at DESC,entity_id LIMIT ? OFFSET ?""",
                (wid, actor_id, "%" + escaped + "%", limit, offset)).fetchall()
        return {"world_id": wid, "actor_id": actor_id, "world_time": w["sim_time"], "offset": offset,
                "items": [{"observed_at": r["observed_at"], "event_id": r["event_id"], "channel": r["channel"],
                           "fact": json.loads(r["data"])} for r in rows],
                "semantics": "Recorded observations, not a live omniscient snapshot"}

    def context_preview(self, wid: str, actor_id: str, query: str = "", max_chars: int = 4000) -> dict:
        known = self.knowledge(wid, actor_id, query, 100)
        selected, used = [], 0
        for observation in known["items"]:
            text = encode(observation)
            if used + len(text) > max_chars:
                continue
            selected.append(observation)
            used += len(text)
        return {"world_id": wid, "actor_id": actor_id, "observations": selected, "characters": used,
                "max_characters": max_chars, "omitted_from_candidates": len(known["items"]) - len(selected),
                "chat_connected": False, "notice": "Preview only. Observations may be outdated; no hidden facts are refreshed."}

    def control(self, wid: str, request) -> dict:
        def perform(c, w, data):
            operation = data["operation"]
            if operation == "rate" and data["rate"] is None:
                raise WorldError("rate is required")
            if w["status"] == "running":
                self._wall_step(c, w, time.time(), 1000)
                if w.get("backlog"):
                    raise WorldError("World has a catch-up backlog; let it drain before controlling time", 409)
            if operation == "pause":
                w["status"] = "paused"
            elif operation == "resume":
                w["status"], w["error"] = "running", None
            else:
                w["rate"] = data["rate"]
            w["last_wall"] = time.time()
            event = emit(c, w, "command:" + data["request_id"], "admin.control", {"operation": operation, "status": w["status"], "rate": w["rate"]})
            return {"status": w["status"], "rate": w["rate"], "event_id": event}
        return self._command(wid, request, "control", perform)

    def actor_control(self, wid: str, actor_id: str, request) -> dict:
        def perform(c, w, data):
            value = entity(c, wid, actor_id)
            if value["kind"] != "actor":
                raise WorldError("Not an actor")
            value["state"]["active"] = data["active"]
            save_entity(c, value)
            changed_actions = []
            expected = "paused" if data["active"] else "running"
            for action in c.execute("SELECT * FROM world_actions WHERE world_id=? AND actor_id=? AND status=?", (wid, actor_id, expected)).fetchall():
                payload = json.loads(action["data"])
                if data["active"]:
                    due = w["sim_time"] + payload.pop("_remaining_seconds", 0)
                    status = "running"
                    c.execute("UPDATE world_jobs SET run_at=?,status='pending' WHERE world_id=? AND job_id=?", (due, wid, "action:" + action["action_id"]))
                else:
                    payload["_remaining_seconds"] = max(0, action["due_at"] - w["sim_time"])
                    due, status = action["due_at"], "paused"
                    c.execute("UPDATE world_jobs SET status='paused' WHERE world_id=? AND job_id=?", (wid, "action:" + action["action_id"]))
                c.execute("UPDATE world_actions SET due_at=?,status=?,data=? WHERE world_id=? AND action_id=?", (due, status, encode(payload), wid, action["action_id"]))
                changed_actions.append({"action_id": action["action_id"], "status": status, "due_at": due})
            event = emit(c, w, "command:" + data["request_id"], "admin.actor_control", {"changes": [value], "actions": changed_actions}, actor_id)
            return {"actor": value, "event_id": event}
        return self._command(wid, request, "actor_control:" + actor_id, perform)

    def create_entity(self, wid: str, request) -> dict:
        def perform(c, w, data):
            if c.execute("SELECT 1 FROM world_entities WHERE world_id=? AND entity_id=?", (wid, data["entity_id"])).fetchone():
                raise WorldError("Entity ID already exists", 409)
            typ = self.catalog.get(c, w["catalog_version"], data["type_id"])
            parent = entity(c, wid, data["parent_id"])
            if parent["kind"] not in {"place", "actor"} and "container" not in parent["capabilities"]:
                raise WorldError("Parent is not a place, actor or container")
            if data["owner_id"] and entity(c, wid, data["owner_id"])["kind"] != "actor":
                raise WorldError("Owner must be an actor")
            value = {"world_id": wid, "entity_id": data["entity_id"], "kind": "item", "type_id": typ["type_id"],
                     "label": data.get("label") or typ["label"], "parent_id": parent["entity_id"], "owner_id": data["owner_id"],
                     "state": {**typ.get("properties", {}).get("default_state", {}), "quantity": data["quantity"]},
                     "capabilities": typ["capabilities"], "version": 0}
            save_entity(c, value)
            event = emit(c, w, "command:" + data["request_id"], "admin.entity_created", {"changes": [value], "type_source": typ["source"]})
            self._witnesses(c, w, [value], event)
            return {"entity": value, "event_id": event, "support": typ["support"]}
        return self._command(wid, request, "entity_create", perform)

    def set_random(self, wid: str, request) -> dict:
        def perform(c, w, data):
            w["settings"]["random"] = data["settings"]
            w["settings"]["random_version"] += 1
            c.execute("UPDATE world_jobs SET status='cancelled' WHERE world_id=? AND kind='random' AND status='pending'", (wid,))
            version = w["settings"]["random_version"]
            when = w["sim_time"] + data["settings"]["interval_seconds"]
            job(c, wid, f"random:{version}:{when}", when, "random", {})
            event = emit(c, w, "command:" + data["request_id"], "admin.random_config", {"settings": data["settings"], "version": version})
            return {"settings": data["settings"], "version": version, "event_id": event}
        return self._command(wid, request, "random", perform)

    def submit_action(self, wid: str, request: ActionRequest) -> dict:
        return self._command(wid, request, "action", lambda c, w, data: self._queue_action(c, w, data))

    def _queue_action(self, c, w: dict, data: dict) -> dict:
        wid, actor_id = w["world_id"], data["actor_id"]
        actor = entity(c, wid, actor_id)
        entity(c, wid, data["target_id"])
        if actor["kind"] != "actor" or not actor["state"].get("active", True):
            raise WorldError("Actor is inactive or invalid", 409)
        if c.execute("SELECT 1 FROM world_actions WHERE world_id=? AND actor_id=? AND status='running'", (wid, actor_id)).fetchone():
            raise WorldError("Actor already has an action in progress", 409)
        thread_id = data.get("thread_id")
        if thread_id:
            row = c.execute("SELECT actor_id,status FROM world_threads WHERE world_id=? AND thread_id=?", (wid, thread_id)).fetchone()
            if not row or row["actor_id"] != actor_id or row["status"] not in {"active", "blocked"}:
                raise WorldError("Thread is not an unfinished objective of this actor", 409)
            c.execute("UPDATE world_threads SET status='active' WHERE world_id=? AND thread_id=?", (wid, thread_id))
        action_id = stable_id("action_", data["request_id"])
        durations = {"move": 60, "take": 5, "put": 5, "open": 3, "close": 3,
                     "turn_on": 1, "turn_off": 1, "clean": 120, "consume": 60,
                     "pour": 5, "repair": 600, "read": 900, "inspect": 0,
                     "cook": 900, "split": 2, "buy": 30}
        duration = data["duration_seconds"] if data.get("duration_seconds") is not None else durations[data["operation"]]
        due = w["sim_time"] + duration
        c.execute("INSERT INTO world_actions VALUES (?,?,?,?,?,?,?)", (wid, action_id, actor_id, "running", due, encode(data), None))
        job(c, wid, "action:" + action_id, due, "action", {"action_id": action_id})
        event = emit(c, w, "start:" + action_id, "action.started", {"action_id": action_id, "request": data, "due_at": due}, actor_id)
        return {"action_id": action_id, "status": "running", "due_at": due, "event_id": event}

    def submit_thread(self, wid: str, request) -> dict:
        def perform(c, w, data):
            actor = entity(c, wid, data["actor_id"])
            if actor["kind"] != "actor":
                raise WorldError("Not an actor")
            for condition in data["conditions"]:
                current = entity(c, wid, condition["entity_id"])
                for part in condition["field"].split("."):
                    if not isinstance(current, dict) or part not in current:
                        raise WorldError("Completion condition references an undefined field")
                    current = current[part]
            tid = stable_id("thread_", data["request_id"])
            data["attempts"] = []
            c.execute("INSERT INTO world_threads VALUES (?,?,?,?,?,?)", (wid, tid, data["actor_id"], data["title"], "active", encode(data)))
            event = emit(c, w, "command:" + data["request_id"], "thread.created", {"thread_id": tid, "conditions": data["conditions"]}, data["actor_id"])
            return {"thread_id": tid, "status": "active", "event_id": event}
        return self._command(wid, request, "thread", perform)

    def thread_control(self, wid: str, tid: str, request) -> dict:
        def perform(c, w, data):
            row = c.execute("SELECT * FROM world_threads WHERE world_id=? AND thread_id=?", (wid, tid)).fetchone()
            if not row:
                raise WorldError("Thread not found", 404)
            if row["status"] in {"done", "abandoned"}:
                raise WorldError("Thread is already closed", 409)
            for action in c.execute("SELECT data FROM world_actions WHERE world_id=? AND actor_id=? AND status IN ('running','paused')", (wid, row["actor_id"])):
                if json.loads(action[0]).get("thread_id") == tid:
                    raise WorldError("Finish the associated action before changing thread status", 409)
            status = {"pause": "paused", "resume": "active", "abandon": "abandoned"}[data["operation"]]
            payload = json.loads(row["data"])
            payload["status_reason"] = data["reason"]
            c.execute("UPDATE world_threads SET status=?,data=? WHERE world_id=? AND thread_id=?", (status, encode(payload), wid, tid))
            event = emit(c, w, "command:" + data["request_id"], "thread.control", {"thread_id": tid, "status": status, "reason": data["reason"]}, row["actor_id"])
            return {"thread_id": tid, "status": status, "event_id": event}
        return self._command(wid, request, "thread_control:" + tid, perform)

    def inject(self, wid: str, request) -> dict:
        def perform(c, w, data):
            target = entity(c, wid, data["target_id"])
            if data["kind"] == "delivery":
                if not data["type_id"]:
                    raise WorldError("delivery requires type_id")
                typ = self.catalog.get(c, w["catalog_version"], data["type_id"])
                if target["kind"] != "place" and "container" not in target["capabilities"]:
                    raise WorldError("Delivery target must be a place or container")
                if "portable" not in typ["capabilities"]:
                    raise WorldError("Delivery type must explicitly support portable capability")
            elif target["kind"] != "place" or data["weather"] is None:
                raise WorldError("Weather injection requires a place and weather")
            key = stable_id("injection_", data["request_id"])
            due = w["sim_time"] + data["delay_seconds"]
            job(c, wid, key, due, "injection", data)
            event = emit(c, w, "command:" + data["request_id"], "admin.injection_scheduled", {"job_id": key, "due_at": due, "kind": data["kind"]})
            return {"job_id": key, "due_at": due, "event_id": event}
        return self._command(wid, request, "inject", perform)

    def advance(self, wid: str, request) -> dict:
        def perform(c, w, data):
            if w["status"] != "paused":
                raise WorldError("Pause the world before manual time advancement", 409)
            target = w["sim_time"] + data["seconds"]
            if data["next_event"]:
                due = c.execute("SELECT MIN(run_at) FROM world_jobs WHERE world_id=? AND status='pending'", (wid,)).fetchone()[0]
                if due is None:
                    return {"sim_time": w["sim_time"], "processed": 0, "backlog": False}
                target = max(w["sim_time"], due)
            result = self._advance(c, w, target, data["max_events"])
            w["last_wall"] = time.time()
            emit(c, w, "command:" + data["request_id"], "admin.advance", result)
            return result
        return self._command(wid, request, "advance", perform)

    def _advance(self, c, w: dict, target: float, budget: int) -> dict:
        count = 0
        while count < budget:
            row = c.execute("""SELECT * FROM world_jobs WHERE world_id=? AND status='pending' AND run_at<=?
                ORDER BY run_at,job_id LIMIT 1""", (w["world_id"], target)).fetchone()
            if row is None:
                break
            w["sim_time"] = max(w["sim_time"], row["run_at"])
            task = {**dict(row), "payload": json.loads(row["payload"])}
            if task["kind"] == "action":
                self._complete_action(c, w, task)
            elif task["kind"] == "daily":
                self._daily(c, w, task)
            elif task["kind"] == "random":
                self._random(c, w, task)
            elif task["kind"] == "injection":
                self._injected(c, w, task)
            else:
                raise WorldError("Unknown persistent job kind")
            c.execute("UPDATE world_jobs SET status='done' WHERE world_id=? AND job_id=?", (w["world_id"], task["job_id"]))
            count += 1
        backlog = c.execute("SELECT 1 FROM world_jobs WHERE world_id=? AND status='pending' AND run_at<=? LIMIT 1", (w["world_id"], target)).fetchone() is not None
        if not backlog:
            w["sim_time"] = target
        w["backlog"] = backlog
        return {"sim_time": w["sim_time"], "target_time": target, "processed": count, "backlog": backlog}

    def _complete_action(self, c, w: dict, task: dict) -> None:
        wid, aid = w["world_id"], task["payload"]["action_id"]
        row = c.execute("SELECT * FROM world_actions WHERE world_id=? AND action_id=?", (wid, aid)).fetchone()
        if not row or row["status"] != "running":
            return
        request = json.loads(row["data"])
        c.execute("SAVEPOINT action_effect")
        try:
            changes = actions.execute(c, w, aid, request, self.catalog)
            c.execute("RELEASE action_effect")
            result, status = {"changes": changes}, "completed"
        except WorldError as error:
            c.execute("ROLLBACK TO action_effect")
            c.execute("RELEASE action_effect")
            result, status, changes = {"reason": str(error), "changes": []}, "failed", []
        event = emit(c, w, "result:" + aid, "action." + status, {"action_id": aid, **result}, request["actor_id"])
        result["event_id"] = event
        c.execute("UPDATE world_actions SET status=?,result=? WHERE world_id=? AND action_id=?", (status, encode(result), wid, aid))
        self._witnesses(c, w, changes, event, request["actor_id"])
        if request.get("thread_id"):
            thread = c.execute("SELECT data FROM world_threads WHERE world_id=? AND thread_id=?", (wid, request["thread_id"])).fetchone()
            data = json.loads(thread[0])
            data["attempts"].append({"action_id": aid, "event_id": event, "status": status})
            complete = status == "completed"
            for condition in data["conditions"]:
                value = entity(c, wid, condition["entity_id"])
                for part in condition["field"].split("."):
                    value = value.get(part) if isinstance(value, dict) else None
                complete = complete and value == condition["equals"]
            thread_status = "done" if complete else "blocked" if status == "failed" else "active"
            c.execute("UPDATE world_threads SET status=?,data=? WHERE world_id=? AND thread_id=?", (thread_status, encode(data), wid, request["thread_id"]))
            emit(c, w, "thread-result:" + aid, "thread.progress", {"thread_id": request["thread_id"], "status": thread_status, "cause_event": event}, request["actor_id"])

    def _witnesses(self, c, w: dict, changes: list[dict], event_id: int, actor_id: str | None = None) -> None:
        places = {actions.room(c, w["world_id"], value) for value in changes}
        ids = {actor_id} if actor_id else set()
        for place in places:
            ids.update(r[0] for r in c.execute("SELECT entity_id FROM world_entities WHERE world_id=? AND kind='actor' AND parent_id=?", (w["world_id"], place)))
        for key in sorted(ids):
            actions.observe(c, w, key, event_id)
        if actor_id and entity(c, w["world_id"], actor_id)["state"].get("active", True):
            actions.remember(c, w, actor_id, changes, event_id, "own_action_result")

    def _daily(self, c, w: dict, task: dict) -> None:
        payload, changes = task["payload"], []
        if payload.get("operation") in {"shop_open", "shop_close"}:
            value = entity(c, w["world_id"], "shop")
            value["state"]["open"] = payload["operation"] == "shop_open"
            save_entity(c, value)
            changes.append(value)
            event = emit(c, w, task["job_id"], "schedule.shop", {"schedule": payload["id"], "changes": changes})
            self._witnesses(c, w, changes, event)
        else:
            request = ActionRequest(request_id=task["job_id"], actor_id=payload["actor_id"], operation="move", target_id=payload["destination"], duration_seconds=60).model_dump(mode="json")
            try:
                self._queue_action(c, w, request)
            except WorldError as error:
                emit(c, w, task["job_id"], "schedule.blocked", {"reason": str(error), "schedule": payload["id"]}, payload["actor_id"])
        when = task["run_at"] + 86400
        job(c, w["world_id"], f"daily:{payload['id']}:{int(when)}", when, "daily", payload)

    def _random(self, c, w: dict, task: dict) -> None:
        settings, wid = w["settings"], w["world_id"]
        config, version = settings["random"], settings["random_version"]
        state = settings["random_state"]
        day = int((w["sim_time"] + settings["utc_offset_minutes"] * 60) // 86400)
        if state.get("day") != day:
            state.update(day=day, count=0, regions={})
        draw = sample(w["seed"], task["job_id"], "occurrence", version)
        by_kind = state.setdefault("last_by_kind", {})
        regions = state.setdefault("regions", {})
        candidates = []
        if config.get("weather_weight", 1) > 0:
            candidates.append(("weather", "street", "street", config.get("weather_weight", 1)))
        if config["rare_events_enabled"] and config.get("failure_weight", 0.1) > 0:
            eligible_lamps = []
            rows = c.execute("SELECT * FROM world_entities WHERE world_id=? AND type_id='neno:lamp' ORDER BY entity_id LIMIT 1000", (wid,)).fetchall()
            for row in rows:
                value = decode_entity(row)
                if not value["state"].get("broken", False):
                    eligible_lamps.append((value["entity_id"], actions.room(c, wid, value)))
            if eligible_lamps:
                weight = config.get("failure_weight", 0.1) / len(eligible_lamps)
                candidates.extend(("failure", key, place, weight) for key, place in eligible_lamps)
        candidates = [x for x in candidates if w["sim_time"] - by_kind.get(x[0], -1e20) >= config["cooldown_seconds"]
                      and regions.get(x[2], 0) < config.get("max_events_per_region_per_day", 3)]
        eligible = state.get("count", 0) < config["max_events_per_day"] and bool(candidates)
        payload = {"rule_version": version, "draw": draw, "probability": config["probability"], "eligible": eligible,
                   "opportunity": task["job_id"], "selected": None, "changes": []}
        if eligible and draw < config["probability"]:
            choice_draw = sample(w["seed"], task["job_id"], "candidate", version)
            cursor = choice_draw * sum(x[3] for x in candidates)
            chosen = candidates[-1]
            for choice in candidates:
                cursor -= choice[3]
                if cursor < 0:
                    chosen = choice
                    break
            kind, key, place, weight = chosen
            payload.update(choice_draw=choice_draw, candidate_count=len(candidates), region=place)
            value = entity(c, wid, key)
            if kind == "weather":
                options = [x for x in ["clear", "cloudy", "rain"] if x != value["state"].get("weather")]
                value["state"]["weather"] = options[int(sample(w["seed"], task["job_id"], "weather", version) * len(options))]
            else:
                value["state"].update(broken=True, on=False)
            save_entity(c, value)
            payload.update(selected={"kind": kind, "entity_id": key}, changes=[value])
            state.update(count=state.get("count", 0) + 1, last_event_at=w["sim_time"])
            by_kind[kind] = w["sim_time"]
            regions[place] = regions.get(place, 0) + 1
        event = emit(c, w, task["job_id"], "random.opportunity", payload)
        self._witnesses(c, w, payload["changes"], event)
        when = task["run_at"] + config["interval_seconds"]
        job(c, wid, f"random:{version}:{when}", when, "random", {})

    def _injected(self, c, w: dict, task: dict) -> None:
        data = task["payload"]
        target = entity(c, w["world_id"], data["target_id"])
        if data["kind"] == "weather":
            target["state"]["weather"] = data["weather"]
            changes = [target]
        else:
            typ = self.catalog.get(c, w["catalog_version"], data["type_id"])
            changes = [{"world_id": w["world_id"], "entity_id": stable_id("delivery_", task["job_id"]),
                        "kind": "item", "type_id": typ["type_id"], "label": data.get("label") or typ["label"],
                        "parent_id": target["entity_id"], "owner_id": target.get("owner_id"),
                        "state": {**typ.get("properties", {}).get("default_state", {}), "quantity": data["quantity"]},
                        "capabilities": typ["capabilities"], "version": 0}]
        for value in changes:
            save_entity(c, value)
        event = emit(c, w, task["job_id"], "admin." + data["kind"], {"changes": changes, "source": "explicit_admin_injection"})
        self._witnesses(c, w, changes, event)

    def _wall_step(self, c, w: dict, now: float, budget: int = 500) -> dict:
        if now <= w["last_wall"]:
            return {"sim_time": w["sim_time"], "processed": 0, "backlog": False}
        elapsed = max(0, now - w["last_wall"])
        start = w["sim_time"]
        target = start + min(elapsed * w["rate"], 86400)
        result = self._advance(c, w, target, budget)
        w["last_wall"] += (w["sim_time"] - start) / w["rate"]
        if not result["backlog"] and elapsed * w["rate"] <= 86400:
            w["last_wall"] = now
        result["backlog"] = result["backlog"] or elapsed * w["rate"] > 86400
        w["backlog"] = result["backlog"]
        return result

    def tick(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self.store.transaction() as c:
            ids = [r[0] for r in c.execute("SELECT world_id FROM world_worlds WHERE status='running' ORDER BY world_id LIMIT 1000")]
        progressed, errors = [], []
        for wid in ids:
            try:
                with self.store.transaction(write=True) as c:
                    w = world(c, wid)
                    if w["status"] != "running":
                        continue
                    result = self._wall_step(c, w, now)
                    w["version"] += 1
                    self._save_world(c, w)
                    progressed.append({"world_id": wid, **result})
            except Exception as error:
                # A concurrent maintenance/read connection must not turn a
                # transient SQLite lock into a permanent world error.  The
                # scheduler will retry the tick on its next iteration.
                if isinstance(error, sqlite3.OperationalError) and "locked" in str(error).lower():
                    errors.append({"world_id": wid, "error": str(error)[:500], "retryable": True})
                    continue
                with self.store.transaction(write=True) as c:
                    c.execute("UPDATE world_worlds SET status='error',error=? WHERE world_id=?", (str(error)[:500], wid))
                errors.append({"world_id": wid, "error": str(error)[:500]})
        return {"progressed": progressed, "errors": errors}
