"""小事：挂在她正在做的事上的小插曲。表里的每一行都合规；多数时候什么都不发生；
发生的总和情境对得上；后果只用那几种；见多了就不稀奇；她的脑子看得到最近几件。"""
from __future__ import annotations

from collections import Counter
from datetime import datetime

import pytest

from app.services.world import life, moments, money, rhythms
from app.services.world.engine import WorldEngine
from app.services.world.models import Advance, WorldCreate
from app.storage.world_store import WorldStore, entity, save_entity, world

START = datetime.fromisoformat("2026-11-16T12:00:00+08:00")  # 周一中午
HOOKS = {"go", "arrive", "finish"}
EFFECTS = {"need", "money", "what", "minutes", "venue_like", "pref", "mood"}


def make(tmp_path, seed="moments", name="w.db"):
    e = WorldEngine(WorldStore(tmp_path / name))
    e.create(WorldCreate(world_id="w", seed=seed, template="life", rhythms="life", start_at=START))
    return e


@pytest.fixture()
def engine(tmp_path):
    return make(tmp_path)


def _always(w, **chance):
    w["settings"]["rhythms"]["moments"]["chance"] = {"go": 1.0, "arrive": 1.0, "finish": 1.0, **chance}


def _rows(monkeypatch, *rows):
    monkeypatch.setattr(moments, "load", lambda pool: list(rows))


def test_every_row_in_the_table_is_well_formed():
    rows = moments.load("life")
    assert len(rows) >= 150
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))
    blank = {"place": "x", "place_kind": "", "area": "", "place_name": "某处", "hour": 12.0, "weather": None, "feels": None,
             "tags": set(), "month": 11, "needs": {}, "tight": False, "pressed": 0.0, "first_visit": False, "sick": False}
    for r in rows:
        assert r["on"] in HOOKS and r.get("kind", "notice") in ("notice", "hiccup", "luck"), r["id"]
        moments._match(r.get("if", {}), blank)                      # 条件词都认识（不认识会抛错）
        assert set(r.get("effects", {})) <= EFFECTS, r["id"]
        assert 0 <= r.get("share", 0.3) <= 1 and r.get("weight", 1) > 0, r["id"]
        texts = r.get("variants") or [r["text"]]
        assert texts and all(t and 4 <= len(t.format(place="某店")) <= 40 for t in texts), r["id"]
        if r.get("kind", "notice") == "notice":
            assert not {"minutes"} & set(r.get("effects", {})), r["id"]   # "看到的"不耽误事
    assert Counter(r["on"] for r in rows)["go"] >= 25


def test_conditions_tie_moments_to_the_situation():
    base = {"place_kind": "canteen", "area": "campus", "hour": 12.0, "weather": "rain", "feels": 6.0, "tags": {"workday", "term"},
            "month": 11, "needs": {"fatigue": 0.7, "hunger": 0.2}, "tight": False, "pressed": 0.5, "first_visit": True,
            "sick": False, "activity": "eat", "mode": "bike", "hours": 2.0, "minutes": 12.0, "choice": "eat_canteen"}
    yes = [{"place_kind": ["canteen"], "hour": [11.3, 12.8]}, {"weather": ["rain"]}, {"weather_not": ["clear"]},
           {"feels_below": 9}, {"tags": ["term"]}, {"tags_not": ["weekend"]}, {"months": [11, 12]},
           {"need_above": {"fatigue": 0.6}}, {"need_below": {"hunger": 0.5}}, {"min_hours": 1.5}, {"min_minutes": 8},
           {"pressed_above": 0.3}, {"first_visit": True}, {"tight": False}, {"hour": [22, 13]}, {"mode": ["bike", "walk"]}]
    no = [{"place_kind": ["library"]}, {"hour": [13, 19]}, {"weather": ["clear"]}, {"weather_not": ["rain"]},
          {"feels_below": 5}, {"feels_above": 30}, {"tags": ["weekend"]}, {"tags_not": ["term"]}, {"months": [6]},
          {"need_above": {"hunger": 0.6}}, {"need_below": {"fatigue": 0.5}}, {"min_hours": 3}, {"min_minutes": 20},
          {"pressed_above": 0.8}, {"first_visit": False}, {"tight": True}, {"sick": True}, {"hour": [22, 3]}, {"mode": ["metro"]}]
    assert all(moments._match(cond, base) for cond in yes)
    assert not any(moments._match(cond, base) for cond in no)
    with pytest.raises(ValueError):
        moments._match({"moon_phase": "full"}, base)


def test_most_of_the_time_nothing_happens(engine):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        hits = 0
        for n in range(300):
            c.execute("SAVEPOINT t")
            w["settings"].get("moments", {}).pop("seen", None)   # 每次都当没见过，只数"有没有事"
            moments.state(w).setdefault("seen", {})
            hits += moments.trigger(c, w, "neno", "finish", {"activity": "scroll", "hours": 0.5, "place": "my_room"}, f"k{n}") is not None
            c.execute("ROLLBACK TO t")
            c.execute("RELEASE t")
        _always(w)
        assert all(moments.trigger(c, w, "mom", "finish", {"activity": "rest", "hours": 1, "place": "parents_home"}, f"npc{n}")
                   is None for n in range(5))      # 小事只发生在她身上，NPC 不抽
    assert 15 <= hits <= 60     # 配置 0.12：三百次里三十来次


def test_what_happens_fits_where_she_is(engine):
    by_id = {r["id"]: r for r in moments.load("life")}
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        _always(w)
        seen = {"canteen": set(), "library": set()}
        for n in range(80):
            for place, extra in (("canteen", {"activity": "eat", "place": "canteen", "choice": "eat_canteen"}),
                                 ("library", {"activity": "study", "place": "library", "choice": "study_library"})):
                c.execute("SAVEPOINT t")
                got = moments.trigger(c, w, "neno", "arrive", extra, f"{place}{n}")
                c.execute("ROLLBACK TO t")
                c.execute("RELEASE t")
                if got:
                    seen[place].add(got["id"])
    assert "canteen_queue" in seen["canteen"] and "lib_window_taken" in seen["library"]
    for place, ids in seen.items():
        for mid in ids:
            assert by_id[mid]["on"] == "arrive", mid                      # 到达时不会冒出"做完才有"的事
            kinds = by_id[mid].get("if", {}).get("place_kind")
            assert kinds is None or place in kinds, (place, mid)        # 食堂的事不会发生在图书馆
    assert not seen["canteen"] & {"lib_window_taken", "lib_full"} and not seen["library"] & {"canteen_queue"}


def test_consequences_land(engine, monkeypatch):
    _rows(monkeypatch, {"id": "t", "on": "arrive", "kind": "hiccup", "text": "{place}出了点事", "share": 0.4,
                        "effects": {"need": {"fatigue": 0.1}, "money": -7, "what": "测试", "minutes": 6, "pref": {"cafe": 0.05},
                                    "mood": -0.2}})
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        _always(w)
        before = {"fatigue": rhythms._value_at(rhythms._rows(c, "w", "neno")["fatigue"], w["sim_time"]),
                  "money": money.balance(c, w, "neno"), "cafe": w["settings"]["rhythms"]["preferences"]["cafe"]}
        got = moments.trigger(c, w, "neno", "arrive", {"activity": "eat", "place": "canteen"}, "k")
        assert got["text"] == "食堂出了点事" and got["minutes"] == 6 and got["money"] == -7 and got["mood"] == -0.2
        assert rhythms._value_at(rhythms._rows(c, "w", "neno")["fatigue"], w["sim_time"]) == pytest.approx(before["fatigue"] + 0.1)
        assert money.balance(c, w, "neno") == pytest.approx(before["money"] - 7)
        assert w["settings"]["life_prefs"]["neno"]["cafe"] == pytest.approx(before["cafe"] + 0.05)
        assert entity(c, "w", "neno")["state"]["recent_moments"][-1]["text"] == "食堂出了点事"
        event = c.execute("SELECT payload FROM world_events WHERE kind='life.moment'").fetchone()
    assert "食堂出了点事" in event[0]


def test_trouble_on_the_way_makes_the_trip_longer(engine, monkeypatch):
    _rows(monkeypatch, {"id": "slow", "on": "go", "kind": "hiccup", "text": "路上耽误了", "effects": {"minutes": 9}})
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        neno = entity(c, "w", "neno")
        neno["state"].pop("doing", None)
        neno["parent_id"] = "my_room"
        save_entity(c, neno)
        choice = {"id": "study_library", "activity": "study"}
        w["settings"]["rhythms"]["moments"]["chance"] = {"go": 0.0}
        c.execute("SAVEPOINT t")
        life.go(c, w, "neno", choice, "library", 1.0, "a")
        plain = entity(c, "w", "neno")["state"]["doing"]["until"]
        c.execute("ROLLBACK TO t")
        c.execute("RELEASE t")
        _always(w)
        life.go(c, w, "neno", choice, "library", 1.0, "a")
        slow = entity(c, "w", "neno")["state"]["doing"]["until"]
    assert slow - plain == pytest.approx(9 * 60)


def test_seen_too_often_stops_being_noticed(engine, monkeypatch):
    _rows(monkeypatch, {"id": "old", "on": "finish", "text": "见惯了的事", "cooldown_hours": 10},
          {"id": "new", "on": "finish", "text": "没见过的事", "cooldown_hours": 10})
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        _always(w)
        now = w["sim_time"]
        st = moments.state(w)
        for n in range(30):                                                       # 冷却里：刚发生过的不会马上再来
            st["seen"] = {"old": [now - 3600]}
            assert moments.trigger(c, w, "neno", "finish", {"activity": "rest", "place": "my_room"}, f"c{n}")["id"] == "new"
        picks = Counter()
        for n in range(200):
            st["seen"] = {"old": [now - 5 * 86400, now - 3 * 86400, now - 86400]}   # 这个月见过三回了
            c.execute("SAVEPOINT t")
            picks[moments.trigger(c, w, "neno", "finish", {"activity": "rest", "place": "my_room"}, f"n{n}")["id"]] += 1
            c.execute("ROLLBACK TO t")
            c.execute("RELEASE t")
        st["seen"] = {"old": [now - 40 * 86400] * 3}                                 # 隔了一个多月：又新鲜了
        fresh = Counter()
        for n in range(200):
            c.execute("SAVEPOINT t")
            fresh[moments.trigger(c, w, "neno", "finish", {"activity": "rest", "place": "my_room"}, f"f{n}")["id"]] += 1
            st["seen"] = {"old": [now - 40 * 86400] * 3}
            c.execute("ROLLBACK TO t")
            c.execute("RELEASE t")
    assert picks["new"] > 5 * picks["old"] > 0      # 权重 1 : 1/8
    assert 0.6 < fresh["new"] / fresh["old"] < 1.6  # 重新对半开


def test_recent_moments_for_her_mind(engine, monkeypatch):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        now = w["sim_time"]
        neno = entity(c, "w", "neno")
        neno["state"]["recent_moments"] = [
            {"at": now - 20 * 3600, "text": "昨天的事", "share": 0.9, "mood": 0},
            {"at": now - 5 * 3600, "text": "排了队", "share": 0.2, "mood": -0.1},
            {"at": now - 4 * 3600, "text": "看到一只胖橘猫", "share": 0.7, "mood": 0.3},
            {"at": now - 3 * 3600, "text": "鞋带散了", "share": 0.05, "mood": 0},
            {"at": now - 1 * 3600, "text": "老板多给了个煎蛋", "share": 0.6, "mood": 0.3}]
        save_entity(c, neno)
        got = [m["text"] for m in moments.recent(c, w, "neno")]
    assert got == ["排了队", "看到一只胖橘猫", "老板多给了个煎蛋"]   # 十小时内、最值得说的三件、按先后


def test_moments_replay_from_seed(tmp_path):
    def lived(name):
        e = make(tmp_path, "replay", name)
        for i in range(9):
            e.advance("w", Advance(request_id=f"s{i}", seconds=8 * 3600, max_events=2000))
        return [(x["sim_time"], x["payload"]["id"]) for x in e.events("w", limit=100000)["items"] if x["kind"] == "life.moment"]
    a, b = lived("a.db"), lived("b.db")
    assert a == b and len(a) >= 2


def test_moments_come_from_all_three_hooks_in_real_life(tmp_path):
    """真跑两天（把概率调到每次都有）：路上、到达、做完三个触发点都接上了，而且每件都对得上当时在做的事。"""
    engine = make(tmp_path, "hooks")
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        _always(w)
        engine._save_world(c, w)
    for i in range(6):
        engine.advance("w", Advance(request_id=f"s{i}", seconds=8 * 3600, max_events=2000))
    events = [e for e in engine.events("w", limit=100000)["items"] if e["kind"] == "life.moment"]
    assert {e["payload"]["hook"] for e in events} == {"go", "arrive", "finish"}
    assert all(e["actor_id"] == "neno" for e in events) and len({e["payload"]["id"] for e in events}) >= 12
