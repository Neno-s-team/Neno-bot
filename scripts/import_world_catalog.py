"""Import pinned BDDL metadata and Neno-authored capabilities, without a simulator."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "6cbf70b075816096e9be53958780769f3264d25d"
BASE = f"https://raw.githubusercontent.com/StanfordVL/BEHAVIOR-1K/{COMMIT}/bddl3"
FILES = {"taxonomy.json": "bddl/generated_data/output_hierarchy_properties.json", "LICENSE": "LICENSE"}
CAPABILITIES = {"openable": "openable", "toggleable": "toggleable", "edible": "edible"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(url: str, destination: Path) -> bytes:
    if destination.exists():
        return destination.read_bytes()
    data = bytearray()
    with requests.get(url, timeout=(10, 45), stream=True) as response:
        response.raise_for_status()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) > 12 * 1024 * 1024:
                raise ValueError("Metadata exceeds 12 MiB; refusing an asset download")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return bytes(data)


def build(output: Path, offline: bool = False) -> dict:
    cache = ROOT / "data/world-source" / COMMIT
    sources, raw = [], {}
    for name, path in FILES.items():
        target = cache / name
        if offline and not target.exists():
            raise ValueError(f"Missing offline source: {target}")
        raw[name] = download(f"{BASE}/{path}", target)
        sources.append({"file": name, "url": f"{BASE}/{path}", "commit": COMMIT,
                        "sha256": digest(raw[name]), "bytes": len(raw[name]), "license": "MIT"})
    tree = json.loads(raw["taxonomy.json"])
    items: dict[str, dict] = {}
    stack = [(tree, None)]
    while stack:
        node, parent = stack.pop()
        key = "bddl:" + node["name"]
        abilities = node.get("abilities", {})
        capabilities = sorted({CAPABILITIES[x] for x in abilities if x in CAPABILITIES})
        if key not in items:
            aliases = sorted(set(node.get("categories", []) + node.get("substances", [])))
            items[key] = {"type_id": key, "label": node["name"].split(".n.")[0].replace("_", " "),
                          "aliases": aliases, "parents": [], "capabilities": capabilities,
                          "properties": {"source_abilities": abilities},
                          "support": "interactive" if capabilities else "metadata_only",
                          "source": {"name": "BDDL", "commit": COMMIT, "id": node["name"]}}
        if parent and parent not in items[key]["parents"]:
            items[key]["parents"].append(parent)
        stack.extend((child, key) for child in node.get("children", []))
    local_path = ROOT / "content/world/local_catalog.json"
    local_raw = local_path.read_bytes()
    local = json.loads(local_raw)
    for item in local["types"]:
        if item["type_id"] in items:
            raise ValueError(f"Duplicate type_id: {item['type_id']}")
        item = {"parents": [], "aliases": [], "properties": {}, "support": "interactive", **item}
        item["source"] = {"name": "Neno authored content", "version": local["version"]}
        items[item["type_id"]] = item
    version = "catalog-" + digest(raw["taxonomy.json"] + local_raw)[:16]
    document = {"version": version, "sources": sources, "local_sha256": digest(local_raw),
                "types": sorted(items.values(), key=lambda x: x["type_id"])}
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if output.exists() and output.read_bytes() != encoded:
        shutil.copy2(output, output.with_suffix(output.suffix + ".work.bak"))
    temporary = output.with_suffix(output.suffix + ".work")
    temporary.write_bytes(encoded)
    temporary.replace(output)
    (output.parent / "BDDL-LICENSE.txt").write_bytes(raw["LICENSE"])
    report = {"version": version, "types": len(items), "source_synsets": len(items) - len(local["types"]),
              "authored_types": len(local["types"]),
              "interactive": sum(x["support"] == "interactive" for x in items.values()),
              "metadata_only": sum(x["support"] == "metadata_only" for x in items.values()),
              "bytes": len(encoded), "sources": sources,
              "limitations": ["Only explicitly mapped abilities execute; source metadata is not a physics engine.",
                              "English source labels are preserved; authored content supplies Chinese aliases."]}
    (output.parent / "import-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {k: v for k, v in report.items() if k not in {"sources", "limitations"}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "content/world/catalog.json")
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.offline), ensure_ascii=False))
