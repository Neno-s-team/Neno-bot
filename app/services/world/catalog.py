from __future__ import annotations

import json
from pathlib import Path

from app.storage.world_store import WorldError, WorldStore

CONTENT = Path(__file__).resolve().parents[3] / "content/world"


class Catalog:
    def __init__(self, store: WorldStore, directory: Path = CONTENT):
        self.store = store
        self.directory = directory
        document = json.loads((directory / "catalog.json").read_text(encoding="utf-8"))
        self.version = document["version"]
        self.report = store.import_catalog(document)

    def get(self, c, version: str, type_id: str) -> dict:
        row = c.execute("SELECT data FROM world_catalog WHERE version=? AND type_id=?", (version, type_id)).fetchone()
        if not row:
            raise WorldError(f"Unknown type: {type_id}", 404)
        return json.loads(row[0])

    def search(self, query: str = "", limit: int = 20, offset: int = 0, version: str | None = None) -> dict:
        version = version or self.version
        escaped = query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args = (version, "%" + escaped + "%")
        with self.store.transaction() as c:
            count = c.execute("SELECT count(*) FROM world_catalog WHERE version=? AND search_text LIKE ? ESCAPE '\\'", args).fetchone()[0]
            rows = c.execute("SELECT data FROM world_catalog WHERE version=? AND search_text LIKE ? ESCAPE '\\' ORDER BY type_id LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return {"version": version, "total": count, "offset": offset, "items": [json.loads(x[0]) for x in rows]}
