"""现实同步（R1）：把现实里正在发生的事接进世界——在映的电影、她的城市这周真实的展览和活动、大家在追的剧。

- 运行时每 6 小时取一次（runtime.real_loop，在事务外联网），落到 settings["real"]；哪一样没取到就沿用旧的，
  超过 FRESH_DAYS 天都没取到，世界退回生成的题目。测试 / 快进模拟不联网。
- 电影（猫眼在映）：每部变成一件世界事件（沿用 film 模板的"怎么知道、去不去"），片名、类型、主演、简介是真的。
- 城里的事（豆瓣同城）：展览、讲座读书会、音乐会、话剧。时间、地点、票价是真的；地点按坐标钉进城市，
  她要去得真的算路程坐地铁。营销味重的、太远的不收；每类只留几件。
- 剧（豆瓣热门）：她在家看剧时追的是一部真的剧，追几次换一部。
- 有真的就不再生成假的：模板标了 `unless_real` 的（生成的电影、特展、分享会）在真数据新鲜时不再排；
  已经排下、她还不知道的假事件撤掉，她已经知道的留着（说过的话不反悔）。

来源都是公开页面、无需密钥；外面来的文字只当数据：清洗、截短、过黑名单后才进世界。
"""
from __future__ import annotations

import hashlib
import html
import math
import re
from datetime import datetime

from app.services.world import city, rhythms
from app.services.world.randomness import sample
from app.storage.world_store import emit, save_entity

FRESH_DAYS = 3
FILM_DAYS = 8          # 一部片子从最近一次看到"在映"起，再算它放这么多天
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
MAOYAN = "https://m.maoyan.com/ajax/movieOnInfoList"
MAOYAN_DETAIL = "https://m.maoyan.com/ajax/detailmovie"
DOUBAN_TV = "https://movie.douban.com/j/search_subjects"
DOUBAN_ABSTRACT = "https://movie.douban.com/j/subject_abstract"
DOUBAN_EVENTS = "https://www.douban.com/location/{city}/events/week-{kind}"


def config(w) -> dict | None:
    return (rhythms.config(w) or {}).get("real")


def enabled(w) -> bool:
    return bool(config(w))


def state(w) -> dict:
    st = w["settings"].setdefault("real", {})
    for name, empty in (("at", {}), ("films", []), ("shows", []), ("events", []), ("places", {}), ("watching", {}),
                        ("watched", {})):
        st.setdefault(name, empty)
    return st


def has(w, part: str) -> bool:
    """这一样（films / events / shows）现在有没有新鲜的真数据。"""
    if not part or not enabled(w):
        return False
    st = state(w)
    at = st["at"].get(part)
    return bool(st[part]) and at is not None and w["sim_time"] - at < FRESH_DAYS * 86400


# ── 清洗 ─────────────────────────────────────────────────────────────────────

_LEAD = re.compile(r"^(?:\s*(?:\[[^\]]*\]|「[^」]*」))+")


def clean(text: str, limit: int = 48) -> str:
    """外面来的一行字：去掉不可见字符、营销括号，压成一行，截短。"""
    text = "".join(ch for ch in html.unescape(text or "") if ch.isprintable() and ch not in "​‌‍﻿")
    text = re.sub(r"【[^】]*】", "", text)
    text = _LEAD.sub("", re.sub(r"\s+", " ", text).strip()).strip(" |｜·-—!！")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _sentence(text: str, limit: int) -> str:
    """简介的头一两句，不超过 limit 字。"""
    text = clean(text, 400)
    cut = max((text.rfind(mark, 0, limit) for mark in "。！？"), default=-1)
    return text[:cut + 1] if cut >= 20 else (text if len(text) <= limit else text[:limit - 1] + "…")


# ── 坐标：豆瓣给的是国测局坐标（GCJ-02），城市数据是 WGS-84 ──────────────────

def _gcj_offset(lat: float, lon: float) -> tuple[float, float]:
    x, y = lon - 105.0, lat - 35.0
    pi = math.pi
    dlat = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    dlat += (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
    dlat += (20.0 * math.sin(y * pi) + 40.0 * math.sin(y / 3.0 * pi)) * 2.0 / 3.0
    dlat += (160.0 * math.sin(y / 12.0 * pi) + 320 * math.sin(y * pi / 30.0)) * 2.0 / 3.0
    dlon = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    dlon += (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
    dlon += (20.0 * math.sin(x * pi) + 40.0 * math.sin(x / 3.0 * pi)) * 2.0 / 3.0
    dlon += (150.0 * math.sin(x / 12.0 * pi) + 300.0 * math.sin(x / 30.0 * pi)) * 2.0 / 3.0
    a, ee = 6378245.0, 0.00669342162296594323
    rad = lat / 180.0 * pi
    magic = 1 - ee * math.sin(rad) ** 2
    root = math.sqrt(magic)
    return ((dlat * 180.0) / ((a * (1 - ee)) / (magic * root) * pi), (dlon * 180.0) / (a / root * math.cos(rad) * pi))


def gcj_to_wgs(lat: float, lon: float) -> tuple[float, float]:
    dlat, dlon = _gcj_offset(lat, lon)
    return (round(lat - dlat, 6), round(lon - dlon, 6))


# ── 解析（纯函数，测试用样本喂）──────────────────────────────────────────────

def parse_films(raw: dict, details: dict) -> list[dict]:
    """猫眼在映列表 + 每部的详情 → [{id, title, release, about, plot, score}]，按猫眼的排序（大致是热度）。"""
    films = []
    for m in raw.get("movieList", []):
        if not m.get("globalReleased") or not m.get("nm"):
            continue
        d = (details.get(str(m["id"])) or {}).get("detailMovie") or {}
        kinds = "、".join([x for x in re.split(r"[,，/\s]+", d.get("cat") or "") if x][:2])
        stars = "、".join((d.get("star") or m.get("star") or "").replace("，", ",").split(",")[:2])
        about = "，".join(x for x in (kinds + "片" if kinds else "", clean(d.get("dir") or "", 12) + "导演" if d.get("dir") else "",
                                    clean(stars, 30) + "主演" if stars else "") if x)
        films.append({"id": str(m["id"]), "title": clean(m["nm"], 30), "release": (m.get("rt") or "")[:10],
                      "about": about, "plot": _sentence(d.get("dra") or "", 90), "score": float(m.get("sc") or 0)})
    return films


def parse_shows(raw: dict, abstracts: dict) -> list[dict]:
    """豆瓣热门剧集 → [{id, title, about, rate}]。评分太低的不追。"""
    shows = []
    for s in raw.get("subjects", []):
        rate = float(s.get("rate") or 0)
        if not s.get("title") or 0 < rate < 6.5:
            continue
        a = (abstracts.get(str(s["id"])) or {}).get("subject") or {}
        bits = ["、".join(a.get("types", [])[:2]), a.get("region") or "",
                f"{a['episodes_count']} 集" if str(a.get("episodes_count") or "").isdigit() else ""]
        shows.append({"id": str(s["id"]), "title": clean(s["title"], 30), "rate": rate,
                      "about": clean("，".join(b for b in bits if b), 30)})
    return shows


_ENTRY = re.compile(r'<li class="list-entry"(.*?)</li>\s*(?=<li class="list-entry"|</ul>\s*<div|\Z)', re.S)


def _grab(pattern: str, text: str) -> str:
    found = re.search(pattern, text, re.S)
    return found.group(1).strip() if found else ""


def parse_events(page: str, kind: str) -> list[dict]:
    """豆瓣同城一页活动 → [{id, kind, title, raw_title, start, end, venue, where, lat, lon, cost, heat}]。"""
    events = []
    for chunk in _ENTRY.findall(page):
        raw_title = html.unescape(_grab(r'itemprop="summary">(.*?)</span>', chunk))
        start, end = _grab(r'startDate" datetime="(.*?)"', chunk), _grab(r'endDate" datetime="(.*?)"', chunk)
        where = html.unescape(_grab(r'itemprop="location" content="(.*?)"', chunk))
        lat, lon = _grab(r'latitude" content="(.*?)"', chunk), _grab(r'longitude" content="(.*?)"', chunk)
        eid = _grab(r"/event/(\d+)/", chunk)
        try:
            datetime.fromisoformat(start), datetime.fromisoformat(end)
            point = gcj_to_wgs(float(lat), float(lon))
        except ValueError:
            continue
        if not eid or not raw_title:
            continue
        fee = _grab(r'class="fee">.*?<strong>(.*?)</strong>', chunk)
        number = re.search(r"\d+(?:\.\d+)?", fee)
        heat = sum(int(n) for n in re.findall(r"<span>(\d+)人(?:参加|感兴趣)</span>", chunk))
        events.append({"id": eid, "kind": kind, "title": clean(raw_title), "raw_title": raw_title, "start": start,
                       "end": end, "venue": _venue_name(where), "where": where, "lat": point[0], "lon": point[1],
                       "cost": 0 if "免费" in fee else round(float(number.group())) if number else None, "heat": heat})
    return events


def _venue_name(where: str) -> str:
    """"杭州 拱墅区 大关 3号线香积寺地铁站附近--Waterloo" → Waterloo；"杭州 上城区 杭州大剧院 新业路39号" → 杭州大剧院。"""
    parts = where.split()
    rest = [p for p in parts[1:] if not (p.endswith(("区", "县")) and len(p) <= 4)] or parts[-1:]
    joined = " ".join(rest)
    name = joined.split("--")[-1] if "--" in joined else (rest[0] if rest else "")
    return clean(name, 18)


# ── 联网（在事务外）─────────────────────────────────────────────────────────

def _get(url: str, *, params=None, referer: str | None = None, timeout: float = 20):
    import requests
    headers = {"User-Agent": UA, **({"Referer": referer} if referer else {})}
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response


def _details(ids: list[str], prefix: str, cache: dict, feed: dict, load, pause: float) -> dict:
    """逐个取详情：取过的用缓存，取不到的留空（下轮再试）。"""
    import time
    result = {}
    for item in ids:
        found = cache.get(prefix + item)
        if found is None:
            try:
                time.sleep(pause)
                found = load(item)
            except Exception:
                found = None
        if found:
            feed["cache"][prefix + item] = found
        result[item] = found or {}
    return result


def fetch_all(cfg: dict, cache: dict | None = None, pause: float = 0.6) -> dict:
    """取一轮。每样各自成败：没取到的那样是 None（世界沿用旧的）。cache 是上轮取过的详情，不重复取。"""
    import time
    cache = cache or {}
    feed: dict = {"films": None, "shows": None, "events": None, "cache": {}, "errors": []}
    try:
        raw = _get(MAOYAN).json()
        ids = [str(m["id"]) for m in raw.get("movieList", []) if m.get("globalReleased")][:cfg.get("films", 3) + 2]

        def film(fid):
            d = _get(MAOYAN_DETAIL, params={"movieId": fid}).json().get("detailMovie") or {}
            return {"detailMovie": {k: d.get(k) for k in ("cat", "dir", "star", "dra")}}

        feed["films"] = parse_films(raw, _details(ids, "film:", cache, feed, film, pause))
    except Exception as error:
        feed["errors"].append(f"films: {str(error)[:80]}")
    try:
        raw = _get(DOUBAN_TV, params={"type": "tv", "tag": "热门", "page_limit": 20, "page_start": 0},
                   referer="https://movie.douban.com/").json()
        ids = [str(s["id"]) for s in raw.get("subjects", [])][:cfg.get("shows", 10)]
        raw["subjects"] = raw.get("subjects", [])[:len(ids)]

        def show(sid):
            s = _get(DOUBAN_ABSTRACT, params={"subject_id": sid}, referer="https://movie.douban.com/").json().get("subject") or {}
            return {"subject": {k: s.get(k) for k in ("types", "region", "episodes_count")}}

        feed["shows"] = parse_shows(raw, _details(ids, "show:", cache, feed, show, pause))
    except Exception as error:
        feed["errors"].append(f"shows: {str(error)[:80]}")
    events = []
    for kind in cfg.get("events", {}):
        try:
            time.sleep(pause)
            events += parse_events(_get(DOUBAN_EVENTS.format(city=cfg["city"], kind=kind)).text, kind)
        except Exception as error:
            feed["errors"].append(f"events/{kind}: {str(error)[:80]}")
    feed["events"] = events or None
    return feed


# ── 落进世界 ─────────────────────────────────────────────────────────────────

def _ts(w, iso: str) -> float:
    offset = w["settings"].get("utc_offset_minutes", 0) * 60
    return (datetime.fromisoformat(iso) - datetime(1970, 1, 1)).total_seconds() - offset


def apply(c, w, feed: dict) -> dict:
    """把取到的一轮落进世界：更新清单，电影和活动变成世界事件，撤掉她还不知道的假事件。返回各样新增/撤掉几件。"""
    st = state(w)
    for part in ("films", "shows", "events"):
        if feed.get(part):  # None / 空 = 这样没取到，沿用旧的
            st[part] = feed[part]
            st["at"][part] = w["sim_time"]
    if feed.get("cache"):
        st["cache"] = feed["cache"]
    result = {"films": _sync_films(c, w) if has(w, "films") else 0,
              "events": _sync_events(c, w) if has(w, "events") else 0}
    result["retired"] = _retire(w)
    _prune_places(c, w)
    return result


def _add(c, w, inst: dict) -> None:
    from app.services.world import happenings
    # 不是每件真事她都有兴趣：听说了、知道有这么回事，但不会去（丧尸片、不对胃口的展）
    inst["cold"] = sample(w["seed"], "real:" + inst["id"], "want") >= config(w).get("want", 0.65)
    happenings.state(w)["items"][inst["id"]] = inst
    emit(c, w, f"happen:{inst['id']}:{int(w['sim_time'])}", "world.happening", inst)  # 下线又回来的片子会再进一次
    # 已经开始好几天的事（上映一个月的电影、办了很久的展）：她多半早就知道
    chance = happenings.templates(w)[inst["tpl"]].get("channels", {}).get("scroll", 0.0) + 0.2
    if inst["start"] < w["sim_time"] - 3 * 86400 and sample(w["seed"], "real:" + inst["id"], "known") < chance:
        happenings.know(c, w, happenings.her(w), inst, "早就知道", "real", quiet=True)


def _sync_films(c, w) -> int:
    from app.services.world import happenings
    items = happenings.state(w)["items"]
    tpl = happenings.templates(w).get(config(w).get("film_template", "film"))
    if tpl is None:
        return 0
    now, made = w["sim_time"], 0
    for film in state(w)["films"][:config(w).get("films", 3)]:
        hid = "real:film:" + film["id"]
        if hid in items:
            items[hid]["end"] = max(items[hid]["end"], now + FILM_DAYS * 86400)  # 还在映：往后顺延
            continue
        where = happenings._place(c, w, tpl, hid)
        if where is None:
            continue
        start = min(now, _ts(w, film["release"] + "T00:00:00")) if film["release"] else now
        title = f"《{film['title']}》"
        _add(c, w, {"id": hid, "tpl": tpl["id"], "scope": tpl["scope"], "title": title,
                    "detail": "；".join(x for x in (f"{where[1]}在放", film["about"]) if x), "start": start,
                    "end": now + FILM_DAYS * 86400, "multi": True, "announce": start, "place": where[0],
                    "place_name": where[1], "slots": {}, "real": True, "real_kind": "film",
                    "doing": f"在{where[1]}看电影：{title}", "plot": film["plot"]})
        made += 1
    return made


_WEEKDAYS = "一二三四五六日"


def _sync_events(c, w) -> int:
    from app.services.world import happenings
    cfg, items, templates = config(w), happenings.state(w)["items"], happenings.templates(w)
    anchor = city.data(w)["anchors"]["home"]
    home, now, made = (anchor["lat"], anchor["lon"]), w["sim_time"], 0
    for kind, spec in cfg.get("events", {}).items():
        tpl = templates.get(spec["tpl"])
        if tpl is None:
            continue
        room = spec.get("max", 2) - sum(1 for i in items.values() if i.get("real_kind") == kind and i["end"] > now)
        pool = []
        for e in state(w)["events"]:
            if e["kind"] != kind or "real:" + e["id"] in items or len(e["title"]) < 4 or not e["venue"]:
                continue
            if any(word in e["raw_title"] for word in cfg.get("block", [])):
                continue  # 营销味重的、不是她会去的
            far = city.dist(home, (e["lat"], e["lon"]))
            if far > cfg.get("within_km", 22) * 1000:
                continue
            shape = _shape(w, e)
            if shape is not None:
                pool.append((-e["heat"], far, e["id"], e, shape))
        for _, _, _, e, shape in sorted(pool, key=lambda x: x[:3])[:max(0, room)]:
            place, name = _place_for(c, w, e)
            title = e["title"]
            fee = "免费" if e["cost"] == 0 else f"¥{e['cost']}" if e["cost"] else ""
            _add(c, w, {"id": "real:" + e["id"], "tpl": tpl["id"], "scope": tpl["scope"], "title": title,
                        "detail": "，".join(x for x in (f"在{name}", shape.pop("hint"), fee) if x),
                        "announce": now, "place": place, "place_name": name, "slots": {}, "real": True,
                        "real_kind": kind, "doing": tpl["attend"]["doing"].format(place=name, title=title),
                        **({"cost": e["cost"]} if e["cost"] is not None else {}), **shape})
            made += 1
    return made


def _shape(w, e: dict) -> dict | None:
    """这件事的时间形状：一场（到点去）还是持续一阵（空了去）。已经过去 / 来不及的返回 None。"""
    start, end, now = _ts(w, e["start"]), _ts(w, e["end"]), w["sim_time"]
    begin, finish = datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])
    low, high = begin.hour + begin.minute / 60, finish.hour + finish.minute / 60
    if begin.date() == finish.date() and end - start <= 5 * 3600:
        if start < now + 1800:
            return None
        return {"start": start, "end": max(end, start + 5400), "multi": False, "hint": ""}
    if end < now + 3600:
        return None
    shape = {"start": start, "end": end, "multi": True, "hint": ""}
    if high - low >= 0.75 and not (low < 0.5 and high > 23.4):   # 每天有固定钟点
        if high - low < 5:   # 每次就一两个小时：多半是每周一场，按第一场那天是周几算；开场前才出门
            shape["open"] = [max(0.0, low - 1.3), max(0.2, low - 0.2)]
            shape["weekdays"] = [begin.isoweekday()]
            shape["hint"] = f"每周{_WEEKDAYS[begin.weekday()]} {begin:%H:%M}"
        else:
            shape["open"] = [low, max(low + 1, high - 1.5)]
    return shape


def _place_for(c, w, e: dict) -> tuple[str, str]:
    """这件事在哪：城里已有的地方（名字对得上、离得近）就用它，否则按坐标新钉一个地点。"""
    for v in city.data(w)["venues"]:
        if len(v["name"]) >= 4 and v["name"] in e["where"] and city.dist((v["lat"], v["lon"]), (e["lat"], e["lon"])) < 1500:
            return v["id"], v["name"]
    pid = "rp" + hashlib.md5(e["venue"].encode("utf-8")).hexdigest()[:8]
    state(w)["places"][pid] = {"name": e["venue"], "lat": e["lat"], "lon": e["lon"]}
    if not c.execute("SELECT 1 FROM world_entities WHERE world_id=? AND entity_id=?", (w["world_id"], pid)).fetchone():
        save_entity(c, {"world_id": w["world_id"], "entity_id": pid, "kind": "place", "type_id": "neno:place",
                        "label": e["venue"], "parent_id": None, "owner_id": None,
                        "state": {"open": True, "city_kind": "venue"}, "capabilities": [], "version": 0})
    return pid, e["venue"]


def _retire(w) -> int:
    """有真的了：生成的同类事件里，她还不知道的撤掉（知道的留着，说过的话不反悔）。"""
    from app.services.world import happenings
    hst, templates = happenings.state(w), happenings.templates(w)
    known = hst["known"].get(happenings.her(w), {})
    gone = [hid for hid, inst in hst["items"].items()
            if not inst.get("real") and hid not in known
            and has(w, templates.get(inst["tpl"], {}).get("unless_real", ""))]
    for hid in gone:
        hst["items"].pop(hid)
    return len(gone)


def _prune_places(c, w) -> None:
    """没有事件再用到、她也不在那儿的临时地点，从坐标表里拿掉。"""
    from app.services.world import happenings
    used = {inst["place"] for inst in happenings.state(w)["items"].values()}
    for row in c.execute("SELECT parent_id, state FROM world_entities WHERE world_id=? AND kind='actor'", (w["world_id"],)):
        used.add(row[0])
        used.update(re.findall(r"rp[0-9a-f]{8}", row[1] or ""))  # 正在去的路上
    places = state(w)["places"]
    for pid in [p for p in places if p not in used]:
        places.pop(pid)


# ── 她在追的剧 ───────────────────────────────────────────────────────────────

def show(w, aid: str, key: str) -> str:
    """她这会儿看的是哪部（"看《…》（悬疑，中国大陆，24 集）"）。追几次换一部；从没有过真数据返回空串。"""
    if not enabled(w):
        return ""
    st = state(w)
    current = st["watching"].get(aid)
    if not current or current["left"] <= 0:
        seen = st["watched"].setdefault(aid, [])
        pool = [s for s in st["shows"] if s["id"] not in seen] if has(w, "shows") else []
        if pool:
            pick = pool[int(sample(w["seed"], f"real:show:{key}", "pick") * len(pool))]
            current = {**pick, "left": 3 + int(sample(w["seed"], f"real:show:{key}", "sessions") * 8)}
            st["watching"][aid] = current
            seen.append(pick["id"])
            del seen[:-40]
        elif not current:
            return ""
        else:
            current["left"] = 1  # 没有新的可换：接着看手上这部
    current["left"] -= 1
    return f"看《{current['title']}》" + (f"（{current['about']}）" if current.get("about") else "")
