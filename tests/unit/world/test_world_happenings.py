"""世界事件：世界自己在发生事（按日历和种子，可重放）；她不是什么都知道；
知道了有时间的事会按兴趣打算去，到点看状态去不去；持续的事变成空档里的选项；
闭馆、装修、地铁故障、店关门/新开会改变她的日子。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.services.world import city, happenings, life
from app.services.world.engine import WorldEngine
from app.services.world.models import Advance, WorldCreate
from app.storage.world_store import WorldStore, entity, save_entity, world

START = datetime.fromisoformat("2026-11-16T08:00:00+08:00")  # 周一


def make(tmp_path, seed="hap", name="w.db"):
    e = WorldEngine(WorldStore(tmp_path / name))
    e.create(WorldCreate(world_id="w", seed=seed, template="life", rhythms="life", start_at=START))
    return e


@pytest.fixture()
def engine(tmp_path):
    return make(tmp_path)


def run(engine, days):
    for i in range(days * 3):
        engine.advance("w", Advance(request_id=f"s{i}", seconds=8 * 3600, max_events=2000))
    return engine.events("w", limit=100000)["items"]


def _items(engine):
    with engine.store.transaction() as c:
        return world(c, "w")["settings"]["happenings"]


def test_world_has_things_going_on_from_the_start(engine):
    st = _items(engine)
    titles = {i["title"] for i in st["items"].values()}
    assert "学校的银杏黄了" in titles and "北山街的梧桐和银杏黄了" in titles   # 11 月中：银杏季已经开始
    seasonal = ("campus_ginkgo", "ginkgo", "red_leaves")
    assert any(i["multi"] and i["tpl"] not in seasonal and i["start"] < START.timestamp()
               for i in st["items"].values())   # 建世界前就在进行的事（在放的电影、在办的展、新开的店）也补上了
    known = st["known"]["neno"]
    assert known and len(known) < len(st["items"])   # 住了几年：知道一些，但不是全知道


def test_replays_from_seed(tmp_path):
    a, b, other = (_items(make(tmp_path, s, n))["items"] for s, n in (("r", "a.db"), ("r", "b.db"), ("z", "c.db")))
    assert a == b and set(a) != set(other)


def test_she_only_goes_to_what_she_knows_about(tmp_path):
    events = run(make(tmp_path, "two-weeks"), 14)
    kinds = [e["kind"] for e in events]
    assert kinds.count("life.heard") >= 5 and kinds.count("life.attended") >= 2
    heard_at: dict[str, float] = {}
    with_plan = set()
    for e in events:
        p = e["payload"]
        if e["kind"] == "life.heard":
            heard_at.setdefault(p["hid"], e["sim_time"])
        elif e["kind"] == "life.planned":
            with_plan.add(p["hid"])
    st_events = {e["payload"]["id"] for e in events if e["kind"] == "world.happening"}
    assert len(heard_at) < len(st_events)   # 有的事她就是没看到
    lectures = [e["payload"] for e in events if e["kind"] == "world.happening" and e["payload"]["tpl"] == "lecture"]
    titles = [x["title"] for x in lectures]
    assert len(titles) == len(set(titles))  # 近期不重复同一个题目


def _force(monkeypatch, **fixed):
    """把指定用途的抽签钉死（兴趣、去不去），别的照旧按种子。"""
    original = happenings._draw

    def draw(w, key, purpose, low=0.0, high=1.0):
        if purpose in fixed:
            return low + (high - low) * fixed[purpose]
        return original(w, key, purpose, low, high)
    monkeypatch.setattr(happenings, "_draw", draw)


def _lecture_tomorrow(engine, hour=15):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        tpl = dict(happenings.templates(w)["lecture"])
        tpl["schedule"] = {**tpl["schedule"], "start": [hour]}
        inst = happenings._create(c, w, tpl, date(2026, 11, 18))
        happenings.know(c, w, "neno", inst, "学院群", "t")
        engine._save_world(c, w)
        return inst


def test_planned_event_she_leaves_in_time_and_attends(engine, monkeypatch):
    _force(monkeypatch, interest=0.0, go=0.0)
    inst = _lecture_tomorrow(engine)
    st = _items(engine)
    assert inst["id"] in st["plans"]["neno"]
    events = run(engine, 3)
    attended = [e for e in events if e["kind"] == "life.attended" and e["payload"]["hid"] == inst["id"]]
    assert len(attended) == 1
    assert inst["start"] - 20 * 60 <= attended[0]["sim_time"] <= inst["start"] + 5 * 60   # 算好路程出门，没迟到
    started = next(e for e in events if e["kind"] == "activity.started" and e["payload"]["source"] == "life:happen:" + inst["id"]
                   and e["payload"]["activity"] == "attend")
    assert started["payload"]["until"] == pytest.approx(inst["end"], abs=120)


def test_she_may_not_go_after_all(engine, monkeypatch):
    _force(monkeypatch, interest=0.0, go=0.6)   # 平常这个签会去（0.6 < 0.88）
    inst = _lecture_tomorrow(engine)
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        neno = entity(c, "w", "neno")
        neno["state"]["conditions"] = [{"label": "感冒了，嗓子疼", "until": w["sim_time"] + 5 * 86400}]
        save_entity(c, neno)
    events = run(engine, 3)
    skipped = [e["payload"] for e in events if e["kind"] == "life.skipped_plan" and e["payload"]["hid"] == inst["id"]]
    assert skipped and skipped[0]["reason"] in ("不舒服", "在睡觉", "有课走不开", "在开组会")
    assert not any(e["kind"] == "life.attended" and e["payload"]["hid"] == inst["id"] for e in events)


def test_not_interested_means_no_plan(engine, monkeypatch):
    _force(monkeypatch, interest=0.99)
    inst = _lecture_tomorrow(engine)
    assert inst["id"] not in _items(engine)["plans"].get("neno", {})


# ── 对世界的影响 ──────────────────────────────────────────────────────────────

def _home(c, w):
    neno = entity(c, "w", "neno")
    neno["state"].pop("doing", None)
    neno["parent_id"] = "my_room"
    save_entity(c, neno)
    return entity(c, "w", "neno")


def _scores(c, w, room=10.0):
    return {ch["id"]: s for ch, _, s in life._candidates(c, w, "neno", _home(c, w), "k", room)}


def _add(c, w, tpl_id, day=date(2026, 11, 16), **schedule):
    tpl = dict(happenings.templates(w)[tpl_id])
    if schedule:
        tpl["schedule"] = {**tpl["schedule"], **schedule}
    return happenings._create(c, w, tpl, day)


def _drop(w, tpl_id):
    """这个种子的世界里碰巧已经有的同类事先拿掉，免得干扰。"""
    st = happenings.state(w)
    for hid in [h for h, i in st["items"].items() if i["tpl"] == tpl_id]:
        st["items"].pop(hid)
        st["known"].get("neno", {}).pop(hid, None)


def test_library_closure_takes_it_off_the_table(engine):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        w["sim_time"] += 2 * 3600   # 10:00
        assert "study_library" in _scores(c, w)
        _add(c, w, "library_closed", start=[8], hours=[5, 5])
        assert "study_library" not in _scores(c, w)
        w["sim_time"] += 4 * 3600   # 14:00，开馆了
        assert "study_library" in _scores(c, w)


def test_renovation_upstairs_drives_her_out_in_daytime_only(engine):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        w["sim_time"] += 2 * 3600   # 周一 10:00
        _drop(w, "renovation")
        quiet = _scores(c, w)
        _add(c, w, "renovation")
        noisy = _scores(c, w)       # 她还不知道是哪家在装修，但吵是真的吵
        assert noisy["study_desk"] - quiet["study_desk"] == pytest.approx(-0.45)
        assert noisy["study_library"] - quiet["study_library"] == pytest.approx(0.25)
        w["sim_time"] += 10 * 3600  # 20:00，收工了
        boosts = happenings.boosts(w, "neno")
        assert "study_desk" not in boosts


def test_metro_fault_delays_the_ride(engine):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        _drop(w, "metro_delay")
        far = next(v for v in city.data(w)["venues"] if v["name"] == "河坊街")
        before = city.route(w, "my_room", far["id"])
        assert before["mode"] == "metro"
        inst = _add(c, w, "metro_delay", start=[8], hours=[2, 2])
        line = before["lines"][0]
        inst["slots"]["line"] = line   # 正好是她要坐的那条
        after = city.route(w, "my_room", far["id"])
        assert after["minutes"] == pytest.approx(before["minutes"] + inst["delay"], abs=0.2)
        assert "故障晚点" in after["label"] and after["delay"] == inst["id"]
        assert inst["id"] not in happenings.state(w)["known"].get("neno", {})
        life.go(c, w, "neno", {"id": "go_out", "activity": "outing"}, far["id"], 1.0, "trip")
        assert happenings.state(w)["known"]["neno"][inst["id"]]["via"] == "坐地铁碰上了"   # 坐上了才知道


def test_closed_shop_is_gone_for_good_and_new_shop_tempts(engine):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        w["sim_time"] += 4 * 3600   # 12:00
        _home(c, w)
        _drop(w, "new_shop")
        choice = next(ch for ch in w["settings"]["rhythms"]["choices"] if ch["id"] == "eat_out")
        usual, _ = life._pick_venue(w, "neno", choice, "my_room", 12, "k")
        happenings.state(w)["closed_venues"].append(usual)
        other, _ = life._pick_venue(w, "neno", choice, "my_room", 12, "k")
        assert other != usual
        memory = life._venue_memory(w, "neno")
        unknown = next(v for v in city.nearby(w, "my_room", choice["venue"]["kinds"], 1200, limit=200)
                       if v["id"] not in memory and city.open_at(v, 12))
        inst = {"id": "new_shop:t", "tpl": "new_shop", "scope": "nearby", "title": unknown["name"] + "新开张", "detail": "",
                "start": w["sim_time"] - 3600, "end": w["sim_time"] + 86400, "multi": True, "announce": w["sim_time"] - 3600,
                "place": unknown["id"], "place_name": unknown["name"], "slots": {}}
        happenings.state(w)["items"][inst["id"]] = inst
        assert happenings.fresh_venues(w, "neno") == {}                      # 不知道就不会特意去
        happenings.know(c, w, "neno", inst, "路过", "t")
        assert happenings.fresh_venues(w, "neno") == {unknown["id"]: 0.4}    # 知道了：想去试试


def test_ongoing_thing_becomes_an_option_until_she_has_been(engine):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        w["sim_time"] += 3 * 3600   # 11:00
        inst = next(i for i in happenings.state(w)["items"].values() if i["tpl"] == "ginkgo")
        happenings.state(w)["known"].setdefault("neno", {}).pop(inst["id"], None)
        cid = "happen:" + inst["id"]
        assert cid not in _scores(c, w)                 # 不知道北山街银杏黄了
        happenings.know(c, w, "neno", inst, "小红书", "t")
        assert cid in _scores(c, w)
        happenings.attended(c, w, "neno", inst["id"], "t1")
        assert cid in _scores(c, w)                     # 银杏可以去两回
        happenings.attended(c, w, "neno", inst["id"], "t2")
        assert cid not in _scores(c, w)


def test_hearing_about_things_is_a_matter_of_chance(engine):
    """群通知多半能看到，刷手机刷到是小概率——同一件事，换一百次机会数一数。"""
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        inst = _add(c, w, "lecture", day=date(2026, 11, 25))
        st = happenings.state(w)
        st["known"]["neno"] = {}
        assert inst["announce"] > w["sim_time"]
        assert not any(h["id"] == inst["id"] for h in happenings.learn(c, w, "neno", "group", "early", scale=99))  # 还没公布：无从知道
        w["sim_time"] = inst["announce"] + 60
        hits = {"group": 0, "scroll": 0}
        for channel in hits:
            for n in range(200):
                st["known"]["neno"] = {}
                c.execute("SAVEPOINT t")
                heard = happenings.learn(c, w, "neno", channel, f"try{n}")
                hits[channel] += any(h["id"] == inst["id"] for h in heard)
                c.execute("ROLLBACK TO t")
                c.execute("RELEASE t")
    assert 0.8 < hits["group"] / 200 < 0.97      # 学院群 0.9
    assert 0.15 < hits["scroll"] / 200 < 0.36    # 刷到 0.25
