"""真实城市（C2）：地点坐标、路程（走路 / 骑车 / 地铁）、按类别找附近的店。不调模型，可重放。

一座城市是一个"城市包" content/world/city/<name>/：pack.json（叫什么、同城活动用哪个城市名、用哪些事件模板、
构建地图用的锚点）+ map.json（scripts/build_city.py 构建）。世界建的时候定下住哪座城（settings.city，
默认取环境变量 WORLD_CITY，再默认杭州），之后不变——中途换城等于搬家，现在不支持。
apply_pack() 把这一个选择落到作息配置的三处：`city.name`、`happenings.pool`、`real.city`。
`city.places` 把模板里的固定地点（家、学校、楼下咖啡店……）钉到城市坐标上，锚点名各城市一样。
城里的店用到才在世界里实体化（entity_id 就是店的 id，"v" 开头）。
"""
from __future__ import annotations

import copy
import heapq
import json
import math
import os
from functools import lru_cache
from pathlib import Path

CONTENT = Path(__file__).resolve().parents[3] / "content" / "world" / "city"

WALK_M_PER_MIN = 75        # 4.5 km/h
BIKE_M_PER_MIN = 220       # 共享单车约 13 km/h
BIKE_OVERHEAD = 4          # 找车、停车
METRO_M_PER_MIN = 600      # 含加减速约 36 km/h
METRO_DWELL = 0.6          # 每站停靠
METRO_WAIT = 4             # 进站、等车
TRANSFER = 5               # 换乘
DETOUR = 1.3               # 直线 → 实际路程
COMFORT = {"walk": 25, "bike": 30}


def dist(a, b) -> float:
    lat = math.radians((a[0] + b[0]) / 2)
    return math.hypot((b[0] - a[0]) * 111320, (b[1] - a[1]) * 111320 * math.cos(lat))


DEFAULT = "hangzhou"


def packs() -> list[str]:
    """有哪些城市可选。"""
    return sorted(p.parent.name for p in CONTENT.glob("*/pack.json") if (p.parent / "map.json").is_file())


@lru_cache(maxsize=16)
def pack(name: str) -> dict:
    path = CONTENT / name / "pack.json"
    if not path.is_file():
        raise ValueError(f"没有这座城市的城市包：{name}（可选：{'、'.join(packs())}）")
    return json.loads(path.read_text(encoding="utf-8"))


def default_name() -> str:
    """新建世界时她住哪：环境变量 WORLD_CITY，没设就是杭州。"""
    return os.getenv("WORLD_CITY", "").strip().lower() or DEFAULT


def apply_pack(cfg: dict, name: str | None = None) -> dict:
    """把"住哪座城"落到作息配置里用到城市的三处。没有 city 这一节的配置（不是生活世界）原样返回。"""
    if "city" not in cfg:
        return cfg
    info = pack(name or default_name())
    result = copy.deepcopy(cfg)
    result["city"]["name"] = info["name"]
    if "happenings" in result:
        result["happenings"]["pool"] = list(info["events"])
    if "real" in result:
        result["real"]["city"] = info["douban"]
    return result


@lru_cache(maxsize=4)
def load(name: str) -> dict:
    data = json.loads((CONTENT / name / "map.json").read_text(encoding="utf-8"))
    data["by_id"] = {v["id"]: v for v in data["venues"]}
    graph: dict[str, list[tuple[str, str, float]]] = {}
    for line in data["lines"]:
        stops = [s for s in line["stops"] if s in data["stations"]]
        for a, b in zip(stops, stops[1:]):
            minutes = dist(data["stations"][a], data["stations"][b]) / METRO_M_PER_MIN + METRO_DWELL
            graph.setdefault(a, []).append((b, line["line"], minutes))
            graph.setdefault(b, []).append((a, line["line"], minutes))
    data["graph"] = graph
    return data


def config(w) -> dict | None:
    from app.services.world import rhythms
    cfg = rhythms.config(w) or {}
    return cfg.get("city")


def enabled(w) -> bool:
    return bool(config(w))


def data(w) -> dict:
    return load(config(w)["name"])


# ── 地点 → 坐标 ───────────────────────────────────────────────────────────────

def _resolve(city: dict, spec: str):
    """模板固定地点的坐标说明：home / campus.library / venue:<id> / 直接 [lat, lon]。"""
    if isinstance(spec, list):
        return tuple(spec)
    if spec == "home":
        a = city["anchors"]["home"]
        return (a["lat"], a["lon"])
    if spec.startswith("campus."):
        return tuple(city["anchors"]["campus"][spec[7:]])
    if spec.startswith("venue:"):
        v = city["by_id"][spec[6:]]
        return (v["lat"], v["lon"])
    if spec.startswith("nearest:"):
        v = nearest_to_home(city, spec[8:])
        return (v["lat"], v["lon"]) if v else None
    return None


def anchor_labels(w) -> dict[str, str]:
    """模板固定地点在城里的真名："楼下咖啡店"→ 政苑咖啡馆，"江边"→ 东环河边。"""
    cfg = config(w) or {}
    if not cfg:
        return {}
    data_ = load(cfg["name"])
    result = {}
    for place, spec in cfg.get("places", {}).items():
        if isinstance(spec, str) and spec.startswith("nearest:"):
            v = nearest_to_home(data_, spec[8:])
            if v:
                result[place] = v["name"] + ("边" if v["kind"] in ("river", "park") and not v["name"].endswith("边") else "")
    return result


def home_line(w) -> str:
    """她住哪、在哪上学（给她的脑子：被问到住哪个城市要答得上来）。"""
    cfg = config(w) or {}
    if not cfg:
        return ""
    data_ = load(cfg["name"])
    home, campus = data_["anchors"]["home"], data_["anchors"]["campus"]
    return f"你住{data_['city']}{home['name']}（{home['station']}地铁站附近），学校在{campus['name']}"


def nearest_to_home(city: dict, kind: str) -> dict | None:
    """模板里的"楼下咖啡店""便利店""江边"钉到家附近真实/补生成的那一家（河、公园在 areas 里）。"""
    home = (city["anchors"]["home"]["lat"], city["anchors"]["home"]["lon"])
    pool = [v for v in city["venues"] if v["kind"] == kind and not v.get("town")] +            [a for a in city.get("areas", []) if a["kind"] == kind and not a.get("campus")]
    return min(pool, key=lambda v: dist(home, (v["lat"], v["lon"])), default=None)


def point(w, place_id: str):
    cfg = config(w)
    city = load(cfg["name"])
    if place_id in city["by_id"]:
        v = city["by_id"][place_id]
        return (v["lat"], v["lon"])
    spec = cfg.get("places", {}).get(place_id)
    if spec:
        return _resolve(city, spec)
    extra = w["settings"].get("real", {}).get("places", {}).get(place_id)  # 现实同步钉进来的地点（剧院、书店）
    return (extra["lat"], extra["lon"]) if extra else None


def area(w, place_id: str) -> str:
    """home / campus / out / away（在别的城市）。"""
    cfg = config(w)
    if place_id in cfg.get("away", []):
        return "away"
    spec = cfg.get("places", {}).get(place_id)
    if spec == "home":
        return "home"
    if isinstance(spec, str) and spec.startswith("campus."):
        return "campus"
    v = load(cfg["name"])["by_id"].get(place_id)
    if v and v.get("campus"):
        return "campus"
    return "out"


# ── 路程 ─────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=4096)
def _ride(name: str, start: str, goal: str) -> tuple[float, tuple[str, ...]]:
    """站到站：最短时间（含换乘）和经过的线路。"""
    graph = load(name)["graph"]
    queue = [(0.0, start, "", ())]
    best: dict[tuple[str, str], float] = {}
    while queue:
        minutes, station, line, used = heapq.heappop(queue)
        if station == goal:
            return minutes, used
        if best.get((station, line), math.inf) <= minutes:
            continue
        best[(station, line)] = minutes
        for nxt, nline, cost in graph.get(station, []):
            extra = TRANSFER if line and nline != line else 0
            heapq.heappush(queue, (minutes + cost + extra, nxt, nline, used if nline in used else used + (nline,)))
    return math.inf, ()


def _nearest_station(city: dict, p) -> tuple[str, float]:
    name = min(city["stations"], key=lambda s: dist(p, city["stations"][s]))
    return name, dist(p, city["stations"][name])


def route(w, here: str, there: str) -> dict:
    """怎么去、要多久。同一处（家里的房间之间）为 0。"""
    if here == there:
        return {"minutes": 0.0, "mode": "none"}
    a, b = area(w, here), area(w, there)
    if a == b == "home":
        return {"minutes": 0.0, "mode": "none"}
    if "away" in (a, b):
        return {"minutes": 240.0, "mode": "train", "label": "坐高铁"}
    p, q = point(w, here), point(w, there)
    if p is None or q is None:
        return {"minutes": 10.0, "mode": "walk", "label": "走过去"}
    d = dist(p, q) * DETOUR
    options = [{"minutes": d / WALK_M_PER_MIN, "mode": "walk", "label": "走过去"}]
    if d > 500:
        options.append({"minutes": d / BIKE_M_PER_MIN + BIKE_OVERHEAD, "mode": "bike", "label": "骑共享单车"})
    if d > 2500:
        city = data(w)
        s1, w1 = _nearest_station(city, p)
        s2, w2 = _nearest_station(city, q)
        if s1 != s2:
            ride, lines = _ride(config(w)["name"], s1, s2)
            if ride < math.inf:
                walk = (w1 + w2) * DETOUR / WALK_M_PER_MIN
                option = {"minutes": walk + METRO_WAIT + ride, "mode": "metro",
                          "label": "坐" + "转".join(lines) + f"（{s1}→{s2}）", "lines": list(lines)}
                from app.services.world import happenings
                for line, (late, hid) in happenings.metro_delays(w).items():
                    if line in lines:  # 这条线正好故障：晚点
                        option["minutes"] += late
                        option["label"] += f"，{line}故障晚点了"
                        option["delay"] = hid
                options.append(option)
    # 走路超过 25 分钟、骑车超过 30 分钟就嫌累：有地铁坐地铁，哪怕慢几分钟
    comfortable = [o for o in options if o["mode"] == "metro" or o["minutes"] <= COMFORT[o["mode"]]]
    best = min(comfortable or options, key=lambda o: o["minutes"])
    best["minutes"] = round(best["minutes"], 1)
    return best


# ── 找店 ─────────────────────────────────────────────────────────────────────

def open_at(venue: dict, hour: float) -> bool:
    low, high = venue.get("open") or DEFAULT_HOURS.get(venue["kind"], [9, 21])
    return low <= hour < high or low <= hour + 24 < high


DEFAULT_HOURS = {"restaurant": [10.5, 21.5], "fast_food": [9, 22], "cafe": [8, 20], "milk_tea": [10, 22],
                 "canteen": [6.5, 20], "supermarket": [8, 22], "convenience": [0, 24], "bakery": [8, 21],
                 "bookstore": [10, 21], "cinema": [10, 24], "mall": [10, 22], "museum": [9, 17],
                 "attraction": [6, 22], "park": [5, 22], "library": [8, 22], "pharmacy": [8, 22],
                 "clinic": [8, 17], "express": [9, 20.5], "hairdresser": [10, 21], "print": [8, 22],
                 "fruit": [8, 22], "bar": [19, 26], "clothes": [10, 22], "stationery": [9, 21], "florist": [9, 21],
                 "laundry": [9, 21], "gym": [7, 23]}

DEFAULT_PRICE = {"restaurant": 70, "fast_food": 25, "cafe": 25, "milk_tea": 15, "canteen": 15, "bakery": 18,
                 "cinema": 50, "museum": 0, "attraction": 0, "park": 0, "mall": 0, "bookstore": 40}


def price(venue: dict) -> int:
    return int(venue.get("price", DEFAULT_PRICE.get(venue["kind"], 30)))


def nearby(w, here: str, kinds: list[str], within_m: float, *, town: bool = False, limit: int = 30) -> list[dict]:
    """here 附近 within_m 米（直线）内这些类别的店，近的在前；town=True 时也算市区去处。"""
    city = data(w)
    p = point(w, here)
    if p is None:
        return []
    found = []
    for v in city["venues"]:
        if v["kind"] not in kinds or (v.get("town") and not town):
            continue
        d = dist(p, (v["lat"], v["lon"]))
        if d <= within_m:
            found.append((d, v))
    found.sort(key=lambda x: x[0])
    return [v for _, v in found[:limit]]


def label(c, w, place_id: str) -> str:
    """地点名：世界里有就用世界里的，城里还没去过的店用店名。"""
    row = c.execute("SELECT label FROM world_entities WHERE world_id=? AND entity_id=?", (w["world_id"], place_id)).fetchone()
    if row:
        return row[0]
    v = data(w)["by_id"].get(place_id) if enabled(w) else None
    return v["name"] if v else place_id


def ensure_place(c, w, venue_id: str) -> None:
    """店第一次被她去到时在世界里实体化。"""
    from app.storage.world_store import save_entity
    if c.execute("SELECT 1 FROM world_entities WHERE world_id=? AND entity_id=?", (w["world_id"], venue_id)).fetchone():
        return
    v = data(w)["by_id"][venue_id]
    save_entity(c, {"world_id": w["world_id"], "entity_id": venue_id, "kind": "place", "type_id": "neno:place",
                    "label": v["name"], "parent_id": None, "owner_id": None,
                    "state": {"open": True, "city_kind": v["kind"]}, "capabilities": [], "version": 0})
