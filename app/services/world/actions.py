from __future__ import annotations

import copy
from collections import deque

from app.services.world.randomness import stable_id
from app.storage.world_store import WorldError, decode_entity, encode, entity, save_entity


def room(c, wid: str, value: dict) -> str:
    seen = set()
    while value["kind"] != "place":
        key = value["entity_id"]
        if key in seen or len(seen) > 64 or not value.get("parent_id"):
            raise WorldError("Entity has no valid containing place")
        seen.add(key)
        value = entity(c, wid, value["parent_id"])
    return value["entity_id"]


def accessible(c, wid: str, actor: dict, target: dict) -> None:
    if room(c, wid, actor) != room(c, wid, target):
        raise WorldError("Target is not in the actor's current place", 409)
    value = target
    for _ in range(64):
        parent = value.get("parent_id")
        if not parent:
            return
        value = entity(c, wid, parent)
        if value["kind"] == "place":
            return
        if value["kind"] == "actor" and value["entity_id"] != actor["entity_id"]:
            raise WorldError("Another actor's inventory is not accessible", 409)
        if "openable" in value["capabilities"] and not value["state"].get("open", False):
            raise WorldError("A containing object is closed", 409)
    raise WorldError("Invalid containment graph")


def capability(target: dict, name: str) -> None:
    if name not in target["capabilities"]:
        raise WorldError(f"Unsupported capability: {name}", 409)


def owned(actor: dict, target: dict) -> None:
    if target.get("owner_id") not in (None, actor["entity_id"]):
        raise WorldError("Target belongs to another actor", 409)


def integer(params: dict, name: str, default: int = 1, maximum: int = 10000) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise WorldError(f"{name} must be an integer from 1 to {maximum}")
    return value


def visible(c, wid: str, actor: dict, limit: int = 1000) -> list[dict]:
    location = entity(c, wid, room(c, wid, actor))
    result, queue, seen = [location], deque([location]), {location["entity_id"]}
    while queue and len(result) < limit:
        parent = queue.popleft()
        if parent["kind"] == "actor" and parent["entity_id"] != actor["entity_id"]:
            continue
        if "openable" in parent["capabilities"] and not parent["state"].get("open", False):
            continue
        rows = c.execute("SELECT * FROM world_entities WHERE world_id=? AND parent_id=? ORDER BY entity_id LIMIT ?",
                         (wid, parent["entity_id"], limit - len(result))).fetchall()
        for row in rows:
            value = decode_entity(row)
            if value["entity_id"] in seen:
                continue
            seen.add(value["entity_id"])
            if value["state"].get("quantity", 1) <= 0:
                continue
            result.append(value)
            queue.append(value)
    return result


def remember(c, w: dict, actor_id: str, values: list[dict], event_id: int, channel: str) -> None:
    for value in values:
        value = copy.deepcopy(value)
        value.pop("world_id", None)
        if value["kind"] == "actor" and value["entity_id"] != actor_id:
            value["state"] = {}
        if value.get("owner_id") != actor_id:
            value.pop("owner_id", None)
        c.execute("""INSERT INTO world_knowledge VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(world_id,actor_id,entity_id) DO UPDATE SET observed_at=excluded.observed_at,
            event_id=excluded.event_id,channel=excluded.channel,data=excluded.data""",
                  (w["world_id"], actor_id, value["entity_id"], w["sim_time"], event_id, channel, encode(value)))


def observe(c, w: dict, actor_id: str, event_id: int, channel: str = "sight") -> int:
    actor = entity(c, w["world_id"], actor_id)
    if not actor["state"].get("active", True):
        return 0
    values = visible(c, w["world_id"], actor)
    remember(c, w, actor_id, values, event_id, channel)
    return len(values)


def route(c, wid: str, actor: dict, destination: dict) -> list[str]:
    if destination["kind"] != "place":
        raise WorldError("Destination must be a place")
    if destination.get("owner_id") not in (None, actor["entity_id"]):
        raise WorldError("Private destination is not accessible", 409)
    if not destination["state"].get("open", True):
        raise WorldError("Destination is closed", 409)
    start = room(c, wid, actor)
    queue, seen = deque([(start, [])]), {start}
    while queue:
        here, path = queue.popleft()
        if here == destination["entity_id"]:
            return path
        for key in entity(c, wid, here)["state"].get("exits", []):
            if key in seen:
                continue
            place = entity(c, wid, key)
            if place.get("owner_id") not in (None, actor["entity_id"]) or not place["state"].get("open", True):
                continue
            seen.add(key)
            queue.append((key, [*path, key]))
    raise WorldError("No accessible path to destination", 409)


def execute(c, w: dict, action_id: str, request: dict, catalog) -> list[dict]:
    wid, op, params = w["world_id"], request["operation"], request.get("params", {})
    actor = entity(c, wid, request["actor_id"])
    if actor["kind"] != "actor" or not actor["state"].get("active", True):
        raise WorldError("Actor is inactive or invalid", 409)
    target = entity(c, wid, request["target_id"])
    changes: dict[str, dict] = {}

    def change(value):
        changes[value["entity_id"]] = value

    def load(key: str):
        if not isinstance(key, str) or not key:
            raise WorldError("Required entity parameter is missing")
        return changes.get(key) or entity(c, wid, key)

    def require_quantity(value, count):
        if value["state"].get("quantity", 1) < count:
            raise WorldError("Insufficient quantity", 409)

    if op == "move":
        route(c, wid, actor, target)
        actor["parent_id"] = target["entity_id"]
        change(actor)
    else:
        accessible(c, wid, actor, target)
        if op not in {"inspect", "buy"}:
            owned(actor, target)
        if op == "inspect":
            pass
        elif op == "take":
            capability(target, "portable")
            require_quantity(target, 1)
            target["parent_id"], target["owner_id"] = actor["entity_id"], actor["entity_id"]
            change(target)
        elif op == "put":
            if target["parent_id"] != actor["entity_id"]:
                raise WorldError("Item must be directly held before putting it down", 409)
            destination = load(params.get("destination_id"))
            accessible(c, wid, actor, destination)
            if destination["kind"] != "place":
                capability(destination, "container")
                owned(actor, destination)
            if "openable" in destination["capabilities"] and not destination["state"].get("open", False):
                raise WorldError("Destination container is closed", 409)
            cursor = destination
            while True:
                if cursor["entity_id"] == target["entity_id"]:
                    raise WorldError("Containment cycle is not allowed", 409)
                if not cursor.get("parent_id"):
                    break
                cursor = load(cursor["parent_id"])
            target["parent_id"] = destination["entity_id"]
            change(target)
        elif op in {"open", "close"}:
            capability(target, "openable")
            target["state"]["open"] = op == "open"
            change(target)
        elif op in {"turn_on", "turn_off"}:
            capability(target, "toggleable")
            if op == "turn_on" and target["state"].get("broken"):
                raise WorldError("Broken equipment cannot be turned on", 409)
            target["state"]["on"] = op == "turn_on"
            change(target)
        elif op == "clean":
            capability(target, "washable")
            if not any("water_source" in x["capabilities"] and x["state"].get("water_available") for x in visible(c, wid, actor)):
                raise WorldError("No accessible water source", 409)
            target["state"]["dirty"] = False
            change(target)
        elif op == "consume":
            capability(target, "edible")
            count = integer(params, "quantity")
            require_quantity(target, count)
            target["state"]["quantity"] = target["state"].get("quantity", 1) - count
            change(target)
        elif op == "pour":
            capability(target, "container")
            destination = load(params.get("destination_id"))
            accessible(c, wid, actor, destination)
            owned(actor, destination)
            capability(destination, "container")
            if destination["entity_id"] == target["entity_id"]:
                raise WorldError("Source and destination must differ")
            amount = integer(params, "volume_ml")
            if target["state"].get("volume_ml", 0) < amount:
                raise WorldError("Insufficient liquid", 409)
            if destination["state"].get("volume_ml", 0) + amount > destination["state"].get("capacity_ml", 0):
                raise WorldError("Destination capacity is insufficient or unknown", 409)
            if destination["state"].get("volume_ml", 0) and destination["state"].get("liquid") != target["state"].get("liquid"):
                raise WorldError("Mixing different liquids is not supported", 409)
            destination["state"]["volume_ml"] = destination["state"].get("volume_ml", 0) + amount
            destination["state"]["liquid"] = target["state"]["liquid"]
            target["state"]["volume_ml"] -= amount
            if target["state"]["volume_ml"] == 0:
                target["state"]["liquid"] = None
            change(target); change(destination)
        elif op == "repair":
            capability(target, "repairable")
            if not target["state"].get("broken", False):
                raise WorldError("Target is not broken", 409)
            if target["state"].get("on", False):
                raise WorldError("Switch equipment off before repair", 409)
            tool, part = load(params.get("tool_id")), load(params.get("part_id"))
            for value in (tool, part):
                accessible(c, wid, actor, value); owned(actor, value)
            capability(tool, "repair_tool"); capability(part, "repair_part")
            require_quantity(part, 1)
            if part["state"].get("part_kind") != target["state"].get("required_part"):
                raise WorldError("The replacement part is incompatible", 409)
            target["state"]["broken"] = False
            part["state"]["quantity"] -= 1
            change(target); change(part)
        elif op == "read":
            capability(target, "readable")
            target["state"]["read_count"] = target["state"].get("read_count", 0) + 1
            change(target)
        elif op == "cook":
            capability(target, "cookware")
            if not any("cooking_station" in x["capabilities"] and x["state"].get("on") for x in visible(c, wid, actor)):
                raise WorldError("No accessible active cooking station", 409)
            ingredients = params.get("ingredients", [])
            if not isinstance(ingredients, list) or not 1 <= len(ingredients) <= 10:
                raise WorldError("Provide 1 to 10 ingredient entity IDs")
            if len(set(ingredients)) != len(ingredients):
                raise WorldError("Duplicate ingredients are not allowed")
            for key in ingredients:
                ingredient = load(key)
                accessible(c, wid, actor, ingredient); owned(actor, ingredient); capability(ingredient, "edible")
                require_quantity(ingredient, 1)
                ingredient["state"]["quantity"] -= 1
                change(ingredient)
            typ = catalog.get(c, w["catalog_version"], "neno:meal")
            meal = {"world_id": wid, "entity_id": stable_id("meal_", action_id), "kind": "item", "type_id": "neno:meal",
                    "label": "做好的饭", "parent_id": target["entity_id"], "owner_id": actor["entity_id"],
                    "state": {"quantity": 1, "ingredients": ingredients}, "capabilities": typ["capabilities"], "version": 0}
            target["state"]["dirty"] = True
            change(meal); change(target)
        elif op in {"split", "buy"}:
            capability(target, "portable")
            count = integer(params, "quantity")
            require_quantity(target, count)
            clone = copy.deepcopy(target)
            clone["entity_id"] = stable_id("item_", action_id)
            clone["version"] = 0
            clone["state"]["quantity"] = count
            if op == "buy":
                location = entity(c, wid, room(c, wid, actor))
                if not location["state"].get("open", True):
                    raise WorldError("Shop is closed", 409)
                price = target["state"].get("price_credits")
                if not isinstance(price, int) or price <= 0 or target.get("owner_id") in (None, actor["entity_id"]):
                    raise WorldError("Target is not for sale", 409)
                cost = price * count
                if actor["state"].get("credits", 0) < cost:
                    raise WorldError("Insufficient simulation credits", 409)
                seller = load(target["owner_id"])
                actor["state"]["credits"] -= cost
                seller["state"]["credits"] = seller["state"].get("credits", 0) + cost
                clone["owner_id"], clone["parent_id"] = actor["entity_id"], actor["entity_id"]
                clone["state"].pop("price_credits", None)
                change(actor); change(seller)
            elif count >= target["state"].get("quantity", 1):
                raise WorldError("Split must leave some quantity in the original stack", 409)
            target["state"]["quantity"] -= count
            change(target); change(clone)
        else:
            raise WorldError("Unknown operation")
    for value in changes.values():
        save_entity(c, value)
    return list(changes.values())
