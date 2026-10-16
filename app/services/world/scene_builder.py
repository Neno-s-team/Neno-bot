from __future__ import annotations

import copy
import json

from app.services.world.randomness import sample
from app.storage.world_store import WorldError


def build_scene(c, catalog, w: dict, template_name: str) -> tuple[list[dict], dict]:
    path = catalog.directory / (template_name + ".json")
    if not path.is_file():
        raise WorldError("Scene template not found", 404)
    template = json.loads(path.read_text(encoding="utf-8"))
    specifications = copy.deepcopy(template["entities"])
    for index, group in enumerate(template.get("variants", [])):
        draw = sample(w["seed"], template["version"], f"group:{index}")
        count = group["min"] + int(draw * (group["max"] - group["min"] + 1))
        for number in range(count):
            i = int(sample(w["seed"], template["version"], f"type:{index}:{number}") * len(group["types"]))
            specifications.append({"entity_id": f"variant_{index}_{number}", "kind": "item", "type_id": group["types"][i],
                                   "parent_id": group["parent_id"], "owner_id": group.get("owner_id")})
    result = []
    for spec in specifications:
        typ = catalog.get(c, w["catalog_version"], spec["type_id"])
        result.append({"world_id": w["world_id"], "entity_id": spec["entity_id"], "kind": spec["kind"],
                       "type_id": typ["type_id"], "label": spec.get("label", typ["label"]),
                       "parent_id": spec.get("parent_id"), "owner_id": spec.get("owner_id"),
                       "state": {**typ.get("properties", {}).get("default_state", {}), **spec.get("state", {})},
                       "capabilities": typ["capabilities"], "version": 0})
    ids = {e["entity_id"] for e in result}
    if len(ids) != len(result):
        raise WorldError("Duplicate scene entity IDs")
    for e in result:
        if e["parent_id"] and e["parent_id"] not in ids:
            raise WorldError("Scene references a missing parent")
        if any(x not in ids for x in e["state"].get("exits", [])):
            raise WorldError("Scene references a missing exit")
    return result, template
