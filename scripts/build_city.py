"""构建她的城市：OSM 真实骨架 + 按真实密度补生成的日常小店 → content/world/city/<城市>/map.json

    python scripts/build_city.py                    # 杭州；用缓存（data/tmp/osm/<城市>/），没有就去拉
    python scripts/build_city.py beijing            # 别的城市：先写好 content/world/city/beijing/pack.json 的 build 一节
    python scripts/build_city.py hangzhou --refresh # 重新拉 OSM

锚点（家、学校的坐标，生活圈半径，市区范围，有名的去处）都在城市包的 pack.json 里，这里不写死任何一座城。

一次构建、结果入库，之后城市固定（像真城市一样不会每天变）；生活里的随机（哪家好吃、今天去哪）由世界种子抽。
数据 © OpenStreetMap contributors，ODbL。
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from osm_query import query  # noqa: E402

CITY = next((a for a in sys.argv[1:] if not a.startswith("-")), "hangzhou") if __name__ == "__main__" else "hangzhou"
PACK = json.loads((ROOT / "content" / "world" / "city" / CITY / "pack.json").read_text(encoding="utf-8"))
SPEC = PACK["build"]
CACHE = ROOT / "data" / "tmp" / "osm" / CITY
OUT = ROOT / "content" / "world" / "city" / CITY / "map.json"

# 锚点：她住的小区、学校；生活圈（米）以学校东门为圆心；市区范围里找有名的去处
HOME = SPEC["home"]
CAMPUS = {**SPEC["campus"], **{k: tuple(SPEC["campus"][k]) for k in ("east_gate", "west", "library")}}
LIFE_RADIUS = SPEC["life_radius"]
METRO_BBOX = tuple(SPEC["metro_bbox"])
DOWNTOWN_BBOX = tuple(SPEC["downtown_bbox"])


def dist(a, b) -> float:
    lat = math.radians((a[0] + b[0]) / 2)
    return math.hypot((b[0] - a[0]) * 111320, (b[1] - a[1]) * 111320 * math.cos(lat))


def _cached(name: str, ql: str, refresh: bool) -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{name}.json"
    if refresh or not path.is_file():
        print("fetch", name)
        path.write_text(json.dumps(query(ql), ensure_ascii=False), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))["elements"]


def fetch(refresh: bool, center: tuple[float, float]) -> dict[str, list[dict]]:
    lat, lon = center
    r = LIFE_RADIUS
    b = ",".join(map(str, METRO_BBOX))
    d = ",".join(map(str, DOWNTOWN_BBOX))
    return {
        "metro": _cached("metro", f'[out:json][timeout:170];rel["route"="subway"]({b})->.r;.r out body;'
                                  'node(r.r:"stop");out;node(r.r:"stop_entry_only");out;node(r.r:"stop_exit_only");out;', refresh),
        "pois": _cached("pois", f'[out:json][timeout:170];(node(around:{r},{lat},{lon})["amenity"]["name"];'
                                f'node(around:{r},{lat},{lon})["shop"]["name"];way(around:{r},{lat},{lon})["amenity"]["name"];'
                                f'way(around:{r},{lat},{lon})["shop"]["name"];);out center tags;', refresh),
        "areas": _cached("areas", f'[out:json][timeout:170];(way(around:{r},{lat},{lon})["landuse"="residential"]["name"];'
                                  f'way(around:{r},{lat},{lon})["leisure"="park"]["name"];'
                                  f'way(around:{r},{lat},{lon})["waterway"~"river|canal"]["name"];'
                                  f'nwr(around:{r},{lat},{lon})["leisure"~"sports_centre|fitness_centre|stadium"]["name"];);out center tags;', refresh),
        # 市区去处分几次小查询（一次查太大公共服务器会超时）
        "town": sum((_cached(f"town_{name}", f'[out:json][timeout:120];nwr{sel}({d});out center tags;', refresh)
                     for name, sel in (("sights", '["tourism"~"attraction|museum|gallery"]["wikidata"]'),
                                       ("malls", '["shop"="mall"]["name"]'), ("cinemas", '["amenity"="cinema"]["name"]'),
                                       ("books", '["shop"="books"]["name"]'), ("parks", '["leisure"="park"]["wikidata"]'))), [])
                + _cached("town_famous", f'[out:json][timeout:120];nwr["name"~"^({"|".join(FAMOUS)})$"]({b});out center tags;', refresh),
    }


# ── 地铁 ─────────────────────────────────────────────────────────────────────

def metro(elements: list[dict]) -> dict:
    nodes = {e["id"]: e for e in elements if e["type"] == "node"}
    coords: dict[str, list[tuple[float, float]]] = {}
    lines, seen = [], set()
    for rel in (e for e in elements if e["type"] == "relation"):
        stops = []
        for m in rel["members"]:
            node = nodes.get(m["ref"]) if m["type"] == "node" and m["role"].startswith("stop") else None
            if node and node.get("tags", {}).get("name"):
                name = node["tags"]["name"]
                coords.setdefault(name, []).append((node["lat"], node["lon"]))
                if not stops or stops[-1] != name:
                    stops.append(name)
        ref = rel["tags"].get("ref") or rel["tags"].get("name", "?")
        if ":" in ref:      # 没有编号、名字是"地铁 12号线: 东坝北 → 四季青桥"这种：只留线路名
            ref = ref.split(":")[0].replace("地铁", "").strip()
        # OSM 里有的线用的是规划编号（北京的 "24;L2" 是亦庄线）：城市包的 build.line_names 给出平时的叫法
        line = LINE_NAMES.get(ref) or (ref if not ref[0].isdigit() or ref.endswith("线") else f"{ref}号线")
        key = (line, frozenset(stops))
        if len(stops) < 2 or key in seen:
            continue  # 反方向同一段只留一条
        seen.add(key)
        lines.append({"line": line, "stops": stops})
    stations = {name: [round(sum(p[0] for p in pts) / len(pts), 6), round(sum(p[1] for p in pts) / len(pts), 6)]
                for name, pts in coords.items()}
    return {"stations": stations, "lines": lines}


# ── 真实地点 ──────────────────────────────────────────────────────────────────

# OSM 标签 → 她生活里的去处类别（kind）
def kind_of(tags: dict) -> str | None:
    amenity, shop, cuisine = tags.get("amenity"), tags.get("shop"), tags.get("cuisine", "")
    if amenity == "cafe":
        return "milk_tea" if "bubble_tea" in cuisine or "tea" in cuisine else "cafe"
    if amenity == "restaurant":
        return "restaurant"
    if amenity == "fast_food":
        return "fast_food"
    if amenity in ("bar", "pub"):
        return "bar"
    if amenity == "cinema":
        return "cinema"
    if amenity == "library":
        return "library"
    if amenity == "pharmacy" or shop == "chemist":
        return "pharmacy"
    if amenity in ("hospital", "clinic"):
        return "clinic"
    if amenity == "post_office" or shop == "parcel_locker":
        return "express"
    return {"supermarket": "supermarket", "convenience": "convenience", "bakery": "bakery", "books": "bookstore",
            "hairdresser": "hairdresser", "beauty": "hairdresser", "greengrocer": "fruit", "mall": "mall",
            "copyshop": "print", "laundry": "laundry", "dry_cleaning": "laundry", "clothes": "clothes",
            "stationery": "stationery", "florist": "florist", "department_store": "mall"}.get(shop or "")


def _point(e: dict) -> tuple[float, float] | None:
    if "lat" in e:
        return (e["lat"], e["lon"])
    if "center" in e:
        return (e["center"]["lat"], e["center"]["lon"])
    return None


def _vid(*parts) -> str:
    return "v" + hashlib.sha1(":".join(map(str, parts)).encode()).hexdigest()[:10]


def real_venues(pois: list[dict]) -> list[dict]:
    result, seen = [], set()
    for e in pois:
        tags, p = e.get("tags", {}), _point(e)
        kind = kind_of(tags)
        if not kind or not p or not tags.get("name"):
            continue
        key = (tags["name"], round(p[0], 3), round(p[1], 3))
        if key in seen:
            continue
        seen.add(key)
        result.append({"id": _vid("osm", e["type"], e["id"]), "name": tags["name"].strip('"“” '), "kind": kind,
                       "lat": round(p[0], 6), "lon": round(p[1], 6), "source": "osm",
                       **({"hours": tags["opening_hours"]} if tags.get("opening_hours") else {})})
    return result


# ── 补生成的日常小店 ───────────────────────────────────────────────────────────
# (kind, 细类, 权重, 营业[开, 关]（关可过 24 点）, 人均价[低, 高], 名字模板)
# 名字模板：{p}=地名（站名/小区/路），{x}=个体店主前缀；"品牌|" 开头的是连锁，店名=品牌（{p}店）
SHOPS = [
    ("noodle", "片儿川", 6, [7, 21], [16, 28], ["{x}片儿川", "{p}片儿川", "{x}面馆"]),
    ("noodle", "拌川", 4, [7, 20.5], [14, 24], ["{x}拌川", "{p}拌川王"]),
    ("noodle", "兰州拉面", 4, [7, 22], [15, 26], ["品牌|兰州拉面"]),
    ("noodle", "牛肉面", 2, [8, 21], [18, 30], ["{x}牛肉面", "{x}重庆小面"]),
    ("snack", "沙县小吃", 4, [7, 22], [10, 20], ["品牌|沙县小吃"]),
    ("snack", "馄饨", 3, [6.5, 20], [10, 18], ["{x}馄饨", "{p}小笼包"]),
    ("snack", "煎饺", 2, [7, 20], [10, 18], ["{x}锅贴", "{x}生煎"]),
    ("breakfast", "早餐", 4, [6, 10.5], [5, 12], ["{x}早餐店", "{x}包子铺", "{p}早点"]),
    ("mala", "麻辣烫", 4, [10, 22], [20, 35], ["品牌|杨国福麻辣烫", "品牌|张亮麻辣烫", "{x}麻辣烫"]),
    ("fast_food", "盖浇饭", 4, [10, 21], [18, 30], ["{x}黄焖鸡米饭", "{x}盖浇饭", "{x}快餐"]),
    ("fast_food", "炸鸡汉堡", 2, [10, 22], [20, 35], ["品牌|华莱士", "品牌|塔斯汀"]),
    ("restaurant", "杭帮菜", 3, [10.5, 21.5], [55, 110], ["{x}杭帮菜", "{p}家常菜", "外婆家"]),
    ("restaurant", "川湘菜", 3, [10.5, 22], [50, 100], ["{x}川菜馆", "{x}湘菜馆", "{x}小炒"]),
    ("restaurant", "东北菜", 1, [10.5, 22], [50, 90], ["{x}东北饺子馆"]),
    ("restaurant", "日料", 1, [11, 22], [70, 150], ["{x}日式料理", "{x}寿司"]),
    ("restaurant", "火锅", 2, [11, 24], [80, 150], ["{x}火锅", "{x}串串香"]),
    ("restaurant", "烧烤", 2, [17, 26], [60, 120], ["{x}烧烤", "{p}夜宵烧烤"]),
    ("milk_tea", "奶茶", 6, [10, 22], [9, 22], ["品牌|古茗", "品牌|茶百道", "品牌|蜜雪冰城", "品牌|霸王茶姬", "品牌|沪上阿姨"]),
    ("cafe", "咖啡", 4, [7.5, 20], [12, 32], ["品牌|瑞幸咖啡", "品牌|库迪咖啡", "{x}咖啡", "{p}咖啡馆"]),
    ("bakery", "面包", 2, [8, 21], [10, 28], ["{x}烘焙", "{x}面包房"]),
    ("fruit", "水果", 3, [8, 22], [15, 40], ["{x}水果", "{p}鲜果"]),
    ("market", "菜市场", 1.2, [6, 18], [20, 60], ["{p}农贸市场", "{p}菜场"]),
    ("supermarket", "超市", 1.5, [8, 22], [30, 90], ["品牌|联华华商", "品牌|物美超市", "{p}生活超市"]),
    ("convenience", "便利店", 4, [0, 24], [8, 25], ["品牌|十足便利店", "品牌|美宜佳", "品牌|罗森", "品牌|全家"]),
    ("pharmacy", "药店", 1.5, [8, 22], [20, 60], ["品牌|老百姓大药房", "品牌|张同泰大药房"]),
    ("express", "快递站", 2, [9, 20.5], [0, 0], ["{p}菜鸟驿站", "{p}快递超市"]),
    ("hairdresser", "理发店", 2, [10, 21], [30, 80], ["{x}造型", "{x}美发"]),
    ("print", "打印店", 1, [8, 22], [2, 15], ["{x}打印复印", "{p}图文"]),
    ("laundry", "洗衣店", 0.8, [9, 21], [20, 50], ["{x}干洗店"]),
    ("gym", "健身房", 0.6, [7, 23], [30, 60], ["{x}健身", "品牌|乐刻运动"]),
    ("bookstore", "书店", 0.3, [10, 21], [30, 80], ["{p}书屋", "{x}旧书店"]),
    ("florist", "花店", 0.5, [9, 21], [30, 90], ["{x}花店"]),
]
# 小店名字的前缀。带地方味（周边城市名、"老杭州"）：别的城市在 pack.json 的 build.owners 里给自己的一份。
# 带本地口味的那几行（杭州：片儿川、拌川、杭帮菜）：别的城市在 pack.json 的 build.local_shops 里按"细类 → 新的一行"换掉
for _i, _row in enumerate(SHOPS):
    _swap = (SPEC.get("local_shops") or {}).get(_row[1])
    if _swap:
        SHOPS[_i] = (_row[0], _swap["label"], _row[2], _row[3], _row[4], _swap["names"])
OWNERS = SPEC.get("owners") or [
    "老王", "张记", "阿姨", "胖子", "小李", "老陈", "金华", "温州", "绍兴", "宁波", "衢州", "老街", "巷口", "一品",
    "好再来", "阿强", "老周", "小城", "满记", "福记", "阿婆", "东北", "蜀香", "湘味", "老杭州", "知味", "方老大", "邓记"]


def _rng(*parts) -> float:
    return int.from_bytes(hashlib.sha256(":".join(map(str, parts)).encode()).digest()[:8], "big") / 2**64


def _offset(p, meters: float, angle: float) -> tuple[float, float]:
    lat = p[0] + meters * math.cos(angle) / 111320
    lon = p[1] + meters * math.sin(angle) / (111320 * math.cos(math.radians(p[0])))
    return (round(lat, 6), round(lon, 6))


def generated_venues(spots: list[dict]) -> list[dict]:
    """每个"街口"（地铁站、小区门口、校门）按大小撒一批店：类别按权重抽，位置在街口几百米内。"""
    total = sum(s[2] for s in SHOPS)
    result, names = [], set()
    for spot in spots:
        for n in range(spot["size"]):
            cursor = _rng(spot["name"], n, "kind") * total
            shop = SHOPS[-1]
            for item in SHOPS:
                cursor -= item[2]
                if cursor < 0:
                    shop = item
                    break
            kind, sub, _, hours, price, templates = shop
            template = templates[int(_rng(spot["name"], n, "tpl") * len(templates))]
            owner = OWNERS[int(_rng(spot["name"], n, "owner") * len(OWNERS))]
            place = spot["short"]
            if template.startswith("品牌|"):
                name = f"{template[3:]}（{place}店）"
            else:
                name = template.format(p=place, x=owner)
            if name in names:
                continue  # 同一个街口不开两家同名店
            names.add(name)
            lat, lon = _offset(spot["point"], 40 + 360 * _rng(spot["name"], n, "r"), 2 * math.pi * _rng(spot["name"], n, "a"))
            low, high = price
            result.append({"id": _vid("gen", spot["name"], n), "name": name, "kind": kind, "sub": sub,
                           "lat": lat, "lon": lon, "source": "gen", "near": spot["name"],
                           "open": hours, "price": round(low + (high - low) * _rng(spot["name"], n, "price"))})
    return result


# ── 组装 ─────────────────────────────────────────────────────────────────────

CAMPUS_BOX = tuple(SPEC["campus"]["box"])  # 校区大致范围


def in_campus(lat: float, lon: float) -> bool:
    return CAMPUS_BOX[0] <= lat <= CAMPUS_BOX[2] and CAMPUS_BOX[1] <= lon <= CAMPUS_BOX[3]


def _short(name: str) -> str:
    for cut in ("站", "小区", "公寓", "花园", "家园"):
        if name.endswith(cut) and len(name) > len(cut) + 1:
            return name[: -len(cut)]
    return name


NOT_A_PLACE = ("电器", "来伊份", "娱乐", "有限公司", "苏宁", "国美")
FAMOUS = SPEC["famous"]      # 这座城里有名的去处（名字 → 类别）
LINE_NAMES = SPEC.get("line_names", {})


def town_places(elements: list[dict]) -> list[dict]:
    result, seen = [], set()
    for e in elements:
        tags, p = e.get("tags", {}), _point(e)
        if not p or not tags.get("name") or tags["name"] in seen or any(x in tags["name"] for x in NOT_A_PLACE):
            continue
        seen.add(tags["name"])
        kind = FAMOUS.get(tags["name"]) or ("museum" if tags.get("tourism") in ("museum", "gallery") else "attraction" if tags.get("tourism")
                else "park" if tags.get("leisure") == "park" else kind_of(tags))
        if kind:
            result.append({"id": _vid("town", e["type"], e["id"]), "name": tags["name"], "kind": kind,
                           "lat": round(p[0], 6), "lon": round(p[1], 6), "source": "osm", "town": True})
    return result


def build(refresh: bool = False) -> dict:
    center = CAMPUS["east_gate"]  # 生活圈圆心：学校东门（家离得不远，生活圈半径要罩得住）
    raw = fetch(refresh, center)
    net = metro(raw["metro"])
    home_e = next(e for e in raw["areas"] if e.get("tags", {}).get("name") == HOME["osm_name"])
    home = _point(home_e)
    near_station = min(net["stations"], key=lambda s: dist(home, net["stations"][s]))

    spots = [{"name": HOME["name"], "short": HOME["name"], "point": home, "size": 10}]
    for name, p in net["stations"].items():
        if dist(p, center) <= LIFE_RADIUS:
            spots.append({"name": f"{name}站", "short": name, "point": tuple(p), "size": 26})
    spots.append({"name": CAMPUS["gate_label"], "short": CAMPUS["gate_label"], "point": CAMPUS["east_gate"], "size": 14})
    areas = []
    for e in raw["areas"]:
        tags, p = e.get("tags", {}), _point(e)
        if not p:
            continue
        kind = ("residential" if tags.get("landuse") == "residential" else "park" if tags.get("leisure") == "park"
                else "river" if tags.get("waterway") else "gym")
        if tags["name"] in SPEC.get("skip_areas", []):
            continue  # 这座城里名字怪、不该当"江边"的水道之类
        if any(a["name"] == tags["name"] and a["kind"] == kind for a in areas):
            continue  # 一条河在 OSM 里是很多段，只留一个代表点
        areas.append({"name": tags["name"], "kind": kind, "lat": round(p[0], 6), "lon": round(p[1], 6),
                      **({"campus": True} if in_campus(*p) else {})})
        if kind == "residential" and tags["name"] != HOME["osm_name"] and dist(p, home) < 1500 and not in_campus(*p):
            spots.append({"name": tags["name"], "short": _short(tags["name"]), "point": p, "size": 3})

    venues = real_venues(raw["pois"])
    for v in venues:
        if in_campus(v["lat"], v["lon"]):
            v["campus"] = True
            if v["kind"] in ("restaurant", "fast_food") and ("食堂" in v["name"] or "餐厅" in v["name"]):
                v["kind"] = "canteen"
    gen = [v for v in generated_venues(spots) if not in_campus(v["lat"], v["lon"])]
    town = [t for t in town_places(raw["town"]) if dist((t["lat"], t["lon"]), center) > LIFE_RADIUS or t["name"] in FAMOUS]
    return {
        "version": SPEC["version"],
        "city": PACK["label"],
        "attribution": "地图数据 © OpenStreetMap contributors（ODbL）；日常小店为按真实密度补生成",
        "anchors": {"home": {"name": HOME["name"], "lat": round(home[0], 6), "lon": round(home[1], 6), "station": near_station},
                    "campus": {"name": CAMPUS["name"], "east_gate": CAMPUS["east_gate"], "west": CAMPUS["west"],
                               "library": CAMPUS["library"], "station": CAMPUS["station"]}},
        "stations": net["stations"],
        "lines": net["lines"],
        "venues": venues + gen + town,
        "areas": areas,
    }


if __name__ == "__main__":
    city = build("--refresh" in sys.argv)
    if "--check" in sys.argv:      # 只比对，不写：重构脚本后确认产物没变
        same = json.dumps(city, ensure_ascii=False, separators=(",", ":")) == OUT.read_text(encoding="utf-8")
        print("和现有的 map.json 一样" if same else "和现有的 map.json 不一样")
        sys.exit(0 if same else 1)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(city, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    from collections import Counter
    print(OUT, OUT.stat().st_size // 1024, "KB")
    print("venues", len(city["venues"]), Counter(v["source"] for v in city["venues"]))
    print(Counter(v["kind"] for v in city["venues"]).most_common())
