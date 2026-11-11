"""她的一天（D1 种子世界 + D2 空档挑事）：义务按时、挑的事不越过义务、可重放、做不成就先不想、习惯会积累。"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.services.world.engine import WorldEngine
from app.services.world.models import Advance, WorldCreate
from app.storage.world_store import WorldStore, world

START = datetime.fromisoformat("2026-11-10T07:00:00+08:00")  # 周二


def make(tmp_path, seed="life-1", name="w.db"):
    engine = WorldEngine(WorldStore(tmp_path / name))
    engine.create(WorldCreate(world_id="w", seed=seed, template="life", rhythms="life", start_at=START))
    return engine


def run(engine, days):
    for i in range(days * 3):
        engine.advance("w", Advance(request_id=f"s{i}", seconds=8 * 3600, max_events=2000))
    return [e for e in engine.events("w", limit=50000)["items"] if e["actor_id"] == "neno"]


def _local(ts):
    return datetime.fromtimestamp(ts)


def test_obligations_happen_with_commute_and_choices_fill_the_gaps(tmp_path):
    events = run(make(tmp_path), 3)
    starts = [e for e in events if e["kind"] == "activity.started"]
    cls = next(e for e in starts if e["payload"]["activity"] == "class")
    commute = [e for e in starts if e["payload"]["activity"] == "commute" and e["payload"]["source"] == "routine:class"]
    assert commute and commute[0]["sim_time"] < cls["sim_time"]
    assert 8 * 60 + 50 <= _local(cls["sim_time"]).hour * 60 + _local(cls["sim_time"]).minute <= 9 * 60 + 10
    assert any(e["payload"]["activity"] == "meeting" for e in starts)
    assert sum(1 for e in starts if e["payload"]["activity"] == "sleep") >= 2
    chosen = [e for e in starts if e["payload"]["source"].startswith("life:") and e["payload"]["activity"] != "commute"]
    assert len({e["payload"]["source"] for e in chosen}) >= 6  # 不是天天一个样


def test_chosen_things_end_before_next_obligation(tmp_path):
    events = run(make(tmp_path, "gap"), 3)
    starts = [e for e in events if e["kind"] == "activity.started"]
    duty = [e for e in starts if e["payload"]["source"].startswith("routine:") and e["payload"]["activity"] != "commute"]
    for e in starts:
        if not e["payload"]["source"].startswith("life:"):
            continue
        nxt = next((d for d in duty if d["sim_time"] > e["sim_time"]), None)
        if nxt is not None:
            assert e["payload"]["until"] <= nxt["sim_time"] + 60


def test_replay_and_seed_divergence(tmp_path):
    pick = lambda ev: [(e["sim_time"], e["payload"].get("source")) for e in ev if e["kind"] == "activity.started"]  # noqa: E731
    a, b, c = run(make(tmp_path, "r", "a.db"), 2), run(make(tmp_path, "r", "b.db"), 2), run(make(tmp_path, "q", "c.db"), 2)
    assert pick(a) == pick(b) and pick(a) != pick(c)


def test_blocked_choice_is_not_retried_immediately(tmp_path):
    events = run(make(tmp_path, "block"), 3)
    blocked = [e for e in events if e["kind"] == "life.blocked" and e["payload"].get("choice")]
    for x, y in zip(blocked, blocked[1:]):
        if x["payload"]["choice"] == y["payload"]["choice"]:
            assert y["sim_time"] - x["sim_time"] >= 3 * 3600 - 1


def test_habits_accumulate_and_company_is_required(tmp_path):
    engine = make(tmp_path, "habit")
    events = run(engine, 4)
    with engine.store.transaction() as c:
        habits = world(c, "w")["settings"]["life_habits"]["neno"]
    assert max(habits.values()) > 2
    for e in [e for e in events if e["kind"] == "activity.started" and e["payload"]["activity"] == "chat"]:
        assert e["payload"]["source"] in {"habit"} or e["payload"]["source"].startswith("life:")


def _choose_at(engine, hour, minute=0):
    """把她放到空闲状态、指定时刻，直接跑一次挑事，返回 life.chose 事件。"""
    from app.services.world import life
    from app.storage.world_store import entity, save_entity
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        neno = entity(c, "w", "neno")
        neno["state"].pop("doing", None)
        neno["parent_id"] = "my_room"
        save_entity(c, neno)
        c.execute("UPDATE world_jobs SET status='cancelled' WHERE world_id='w' AND status='pending' AND kind!='routine'")
        key = f"life:neno:probe:{hour}:{minute}"
        life.choose(c, w, {"job_id": key, "payload": {"actor_id": "neno"}, "run_at": w["sim_time"]})
        engine._save_world(c, w)
        row = c.execute("SELECT payload FROM world_events WHERE world_id='w' AND event_key=?", (key + ":chose",)).fetchone()
    import json
    return json.loads(row[0]) if row else None


def test_noise_changes_ranking_between_draws(tmp_path):
    """同一时刻、同样状态，只换抽签 key：先后顺序会变（噪声在起作用）。"""
    import json
    from app.services.world import life
    from app.storage.world_store import entity
    engine = make(tmp_path, "noise")
    run(engine, 1)
    orders = set()
    with engine.store.transaction(write=True) as c:
        import copy
        base = world(c, "w")
        c.execute("SAVEPOINT probe")
        for n in range(10):
            w = copy.deepcopy(base)  # 习惯/冷却等内存状态每次都从同一起点开始
            c.execute("UPDATE world_entities SET state=json_remove(state,'$.doing') WHERE world_id='w' AND entity_id='neno'")
            life.choose(c, w, {"job_id": f"probe{n}", "payload": {"actor_id": "neno"}, "run_at": w["sim_time"]})
            row = c.execute("SELECT payload FROM world_events WHERE event_key=?", (f"probe{n}:chose",)).fetchone()
            orders.add(tuple((x[0], x[1]) for x in json.loads(row[0])["options"]))
            c.execute("ROLLBACK TO probe")
    assert len(orders) > 1   # 同样的状态，每次的分数（进而先后）不一样：噪声在起作用


def test_failed_choice_cools_down(tmp_path, monkeypatch):
    from app.services.world import life
    from app.storage.world_store import WorldError, entity, save_entity
    engine = make(tmp_path, "cool")
    run(engine, 1)

    def broke(*_a, **_k):
        raise WorldError("Not enough money", 409)

    monkeypatch.setattr(life, "_do", broke)
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        neno = entity(c, "w", "neno")
        neno["state"].pop("doing", None)
        save_entity(c, neno)
        life.choose(c, w, {"job_id": "life:neno:cool", "payload": {"actor_id": "neno"}, "run_at": w["sim_time"]})
        chose = c.execute("SELECT payload FROM world_events WHERE event_key='life:neno:cool:chose'").fetchone()
        picked = __import__("json").loads(chose[0])["choice"]
        until = w["settings"]["life_cooldown"]["neno"][picked]
        assert until >= w["sim_time"] + 3 * 3600 - 1
        assert picked not in [ch["id"] for ch, _, _ in life._candidates(c, w, "neno", entity(c, "w", "neno"))]


def test_chat_needs_someone_awake_there(tmp_path):
    """需要有人陪的事（聊天）只有身边有醒着的人才会进候选。她一个人住，所以平时在家不会有。"""
    from app.services.world import life
    from app.storage.world_store import entity, save_entity
    engine = make(tmp_path, "company")
    run(engine, 1)
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        w["sim_time"] += 6 * 3600  # 13:00
        w["settings"]["rhythms"]["choices"].append(
            {"id": "chat_here", "activity": "chat", "place": "riverside", "base": 0.5, "hours": [0, 24]})
        neno = entity(c, "w", "neno")
        neno["state"].pop("doing", None)
        save_entity(c, neno)

        def ids():
            return [ch["id"] for ch, _, _ in life._candidates(c, w, "neno", entity(c, "w", "neno"))]

        barista = entity(c, "w", "barista")
        barista["parent_id"] = "street"
        save_entity(c, barista)
        assert "chat_here" not in ids()
        barista["parent_id"] = "riverside"
        barista["state"].pop("doing", None)
        save_entity(c, barista)
        assert "chat_here" in ids()


def test_preferences_drift_with_experience(tmp_path):
    engine = make(tmp_path, "taste")
    events = run(engine, 5)
    tastes = [e for e in events if e["kind"] == "life.taste"]
    assert len(tastes) > 10
    assert all(abs(e["payload"]["to"] - e["payload"]["from"]) <= 0.08 + 1e-9 for e in tastes)
    assert {e["payload"]["to"] > e["payload"]["from"] for e in tastes} == {True, False}  # 有喜欢上的也有腻了的
    with engine.store.transaction() as c:
        prefs = world(c, "w")["settings"]["life_prefs"]["neno"]
    start = json.load(open("content/world/rhythms/life.json", encoding="utf-8"))["preferences"]
    assert any(abs(prefs[k] - start[k]) > 0.02 for k in start)


def test_rain_spoils_outdoor_things(tmp_path):
    from app.services.world import life
    from app.storage.world_store import entity, save_entity
    engine = make(tmp_path, "rain")
    run(engine, 1)
    deltas, signs = {}, set()
    for weather in ("clear", "rain"):
        total = 0.0
        for n in range(20):
            with engine.store.transaction(write=True) as c:
                c.execute("SAVEPOINT p")
                w = world(c, "w")
                w["sim_time"] += n * 600
                street = entity(c, "w", "street")
                street["state"]["weather"] = weather
                save_entity(c, street)
                neno = entity(c, "w", "neno")
                neno["parent_id"] = "riverside"
                save_entity(c, neno)
                before = w["settings"]["life_prefs"]["neno"]["riverside"]
                life.taste(c, w, "neno", {"source": "life:walk_river"}, "到时间了")
                step = w["settings"]["life_prefs"]["neno"]["riverside"] - before
                total += step
                if weather == "clear":
                    signs.add(step > 0)
                c.execute("ROLLBACK TO p")
        deltas[weather] = total
    assert deltas["rain"] < deltas["clear"] - 0.3
    assert signs == {True, False}  # 晴天去江边也不是每次都觉得好


def _break_laptop(engine, fix_chance):
    from app.storage.world_store import entity, save_entity
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        w["settings"]["rhythms"]["activities"]["fix"]["fix_chance"] = fix_chance
        laptop = entity(c, "w", "laptop")
        laptop["state"].update(broken=True, on=False, broken_at=w["sim_time"])
        save_entity(c, laptop)
        WorldEngine._save_world(c, w)


def test_broken_laptop_lowers_study_and_offers_fixing(tmp_path):
    """电脑坏了：要用电脑的事分数降（不是禁止），多出"修"这个选项；没坏时没有这个选项。"""
    from app.services.world import life
    from app.storage.world_store import entity, save_entity
    engine = make(tmp_path, "broke")
    run(engine, 1)
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        w["sim_time"] += 7 * 3600  # 14:00 左右，书桌和修东西都在时段里
        neno = entity(c, "w", "neno")
        neno["state"].pop("doing", None)
        neno["parent_id"] = "my_room"
        save_entity(c, neno)

        def scores():
            return {ch["id"]: s for ch, _, s in life._candidates(c, w, "neno", entity(c, "w", "neno"))}

        fine = scores()
        laptop = entity(c, "w", "laptop")
        laptop["state"]["broken"] = True
        save_entity(c, laptop)
        broken = scores()
    assert "fix_item" not in fine and "fix_item" in broken
    penalty = w["settings"]["rhythms"]["chooser"]["broken_penalty"]
    assert abs((fine["study_desk"] - broken["study_desk"]) - penalty) < 1e-9
    assert "study_desk" in broken  # 降分不是禁止
    assert broken["read_bed"] == fine["read_bed"]  # 不用电脑的事不受影响


def test_fixing_is_a_draw_not_a_guarantee(tmp_path):
    """修不修得好靠抽：概率 0 永远修不好（隔着冷却再试），概率 1 修好后就不再折腾。"""
    from app.storage.world_store import entity
    never = make(tmp_path, "fix", "never.db")
    run(never, 1)
    _break_laptop(never, 0.0)
    events = run(never, 3)
    tries = [e for e in events if e["kind"] == "activity.started" and e["payload"]["source"] == "life:fix_item"]
    assert len(tries) >= 2 and not any(e["kind"] == "life.fixed" for e in events)
    assert sum(e["kind"] == "life.fix_failed" for e in events) >= 1
    assert all(b["sim_time"] - a["sim_time"] >= 5 * 3600 - 60 for a, b in zip(tries, tries[1:]))
    with never.store.transaction() as c:
        assert entity(c, "w", "laptop")["state"]["broken"]

    always = make(tmp_path, "fix", "always.db")
    run(always, 1)
    _break_laptop(always, 1.0)
    events = run(always, 3)   # 坏了不一定当天修：给两天
    fixed = [e for e in events if e["kind"] == "life.fixed"]
    assert len(fixed) == 1 and fixed[0]["payload"]["item"] == "laptop"
    assert not any(e["kind"] == "activity.started" and e["payload"]["source"] == "life:fix_item"
                   and e["sim_time"] > fixed[0]["sim_time"] for e in events)
    with always.store.transaction() as c:
        assert not entity(c, "w", "laptop")["state"]["broken"]


def test_what_breaks_follows_fragility(tmp_path):
    """坏哪件按"多容易坏"抽：吹风机常坏、电脑难得坏，权重 0 的（手机）永远不坏。"""
    from collections import Counter
    from app.services.world import perturbations
    from app.storage.world_store import entity, save_entity
    engine = make(tmp_path, "fragile")
    picks = Counter()
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        entry = next(e for e in w["settings"]["rhythms"]["perturbations"]["pool"] if e["id"] == "thing_broke")
        entry["effects"][0]["fragility"]["lamp"] = 0.0
        neno = entity(c, "w", "neno")
        for n in range(400):
            c.execute("SAVEPOINT t")
            happened = perturbations._happen(c, w, f"k{n}", entry, neno)
            picks[happened["applied"][0]["broke"]] += 1
            c.execute("ROLLBACK TO t")
            c.execute("RELEASE t")
    assert picks["lamp"] == 0
    assert picks["hair_dryer"] > 4 * picks["laptop"] and picks["kettle"] > 2 * picks["laptop"]
    assert picks["laptop"] > 0  # 难得，但不是不会


# ── 一天的节奏：到点想做、做够了不想做、睡前洗澡 ─────────────────────────────

def _fresh(c):
    from app.storage.world_store import entity, save_entity
    neno = entity(c, "w", "neno")
    neno["state"].pop("doing", None)
    neno["parent_id"] = "my_room"
    save_entity(c, neno)
    return entity(c, "w", "neno")


def _scores_at(c, w):
    from app.services.world import life
    return {ch["id"]: s for ch, _, s in life._candidates(c, w, "neno", _fresh(c), "k", 10.0)}


def test_mealtimes_and_evenings_pull(tmp_path):
    """饭点更想吃饭，晚上更想看剧；别的钟点没有这份加成。"""
    from app.services.world import life
    engine = make(tmp_path, "clock")
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")           # 周二 07:00
        choices = {ch["id"]: ch for ch in w["settings"]["rhythms"]["choices"]}
        assert life.by_hour(choices["eat_out"], 12.0) == 0.55 and life.by_hour(choices["eat_out"], 15.0) == 0
        assert life.by_hour(choices["watch_show"], 20.5) == 0.35 and life.by_hour(choices["watch_show"], 14.0) == 0
        assert life.by_hour(choices["rest_sofa"], 12.0) == 0
        def hungry_at(hours):       # 同样的饿，只换钟点
            w["sim_time"] += hours * 3600
            c.execute("UPDATE world_need SET value=0.5, at=? WHERE world_id='w' AND actor_id='neno' AND need='hunger'",
                      (w["sim_time"],))
            return _scores_at(c, w)

        noon = hungry_at(5)         # 12:00
        tea = hungry_at(3)          # 15:00
    assert noon["eat_out"] - tea["eat_out"] == pytest.approx(0.55, abs=0.05)


def test_enough_for_today(tmp_path):
    """学了一阵之后今天就不太想再学了；周末的量更少；手上的事急了会多学。不是禁止。"""
    from app.services.world import life
    engine = make(tmp_path, "enough")
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        w["sim_time"] += 7 * 3600   # 周二 14:00
        w["settings"]["tasks"]["items"] = {}   # 手上没有要交的事（急的时候量会放大，另测）
        study = next(ch for ch in w["settings"]["rhythms"]["choices"] if ch["id"] == "study_desk")
        book = life._today(w, "neno")
        assert life.enough(w, "neno", study) == 0
        book["hours"]["study"] = 1.0
        assert life.enough(w, "neno", study) == 0                         # 才学了一小时：谈不上嫌多
        book["hours"]["study"] = 2.0
        some = life.enough(w, "neno", study)
        book["hours"]["study"] = 6.0
        lots = life.enough(w, "neno", study)
        assert 0 <= some < lots and lots > 0.5
        assert life.enough(w, "neno", study, pressed=1.0) < lots          # 急的时候多学一点也不嫌多
        calm = _scores_at(c, w)
        assert "study_desk" in calm                                       # 减分，不是不能
        book["hours"]["study"] = 0.0
        fresh = _scores_at(c, w)
        assert fresh["study_desk"] - calm["study_desk"] == pytest.approx(lots)
        assert fresh["read_bed"] == pytest.approx(calm["read_bed"])       # 学够了不影响看闲书
        w["sim_time"] += 4 * 86400  # 周六 14:00：换了一天，账本清零；同样学 2 小时，周末已经嫌多
        life._today(w, "neno")["hours"]["study"] = 2.0
        weekend = life.enough(w, "neno", study)
    assert weekend > some + 0.3


def test_hours_are_tallied_per_day_with_4am_rollover(tmp_path):
    from app.services.world import life
    engine = make(tmp_path, "tally")
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        w["sim_time"] += 16.5 * 3600      # 周二 23:30
        life.tally(w, "neno", {"activity": "study", "since": w["sim_time"] - 2 * 3600})
        w["sim_time"] += 2 * 3600         # 周三 01:30：熬夜还算前一天
        life.tally(w, "neno", {"activity": "study", "since": w["sim_time"] - 1.5 * 3600})
        assert life._today(w, "neno")["hours"]["study"] == pytest.approx(3.5)
        w["sim_time"] += 6 * 3600         # 07:30：新的一天
        assert life._today(w, "neno")["hours"] == {}
        assert w["settings"]["life_last_done"]["neno"]["study"] == pytest.approx(w["sim_time"] - 6 * 3600)


def test_shower_before_bed_unless_done_or_too_tired(tmp_path, monkeypatch):
    import json
    from app.services.world import rhythms
    from app.storage.world_store import entity
    engine = make(tmp_path, "bed")

    def bedtime(c, w, key, draw=0.1):
        monkeypatch.setattr(rhythms, "_draw", lambda w, k, purpose, low=0.0, high=1.0: low + (high - low) * draw)
        _fresh(c)
        rhythms._routine(c, w, {"job_id": key, "run_at": w["sim_time"],
                                "payload": {"actor_id": "neno", "entry": "sleep", "day": 0, "until": None, "begin": w["sim_time"]}})
        return entity(c, "w", "neno")["state"]["doing"]["activity"]

    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        w["sim_time"] += 17.7 * 3600      # 周三 00:42
        c.execute("UPDATE world_jobs SET status='cancelled' WHERE kind='routine'")
        assert bedtime(c, w, "b1") == "shower"                                  # 今天还没洗：先洗
        then = [json.loads(r[0]) for r in c.execute("SELECT payload FROM world_jobs WHERE job_id='b1:then'")]
        assert then and then[0]["prepared"] is True
        _fresh(c)
        rhythms._routine(c, w, {"job_id": "b1:then", "run_at": w["sim_time"], "payload": then[0]})
        assert entity(c, "w", "neno")["state"]["doing"]["activity"] == "sleep"  # 洗完去睡，不会再洗一遍
        w["settings"]["life_last_done"] = {"neno": {"shower": w["sim_time"] - 3 * 3600}}
        assert bedtime(c, w, "b2") == "sleep"                                   # 晚上洗过了：直接睡
        w["settings"]["life_last_done"] = {}
        assert bedtime(c, w, "b3", draw=0.95) == "sleep"                        # 这回懒得洗（抽签）
        c.execute("UPDATE world_need SET value=0.95, at=? WHERE world_id='w' AND actor_id='neno' AND need='fatigue'",
                  (w["sim_time"],))
        assert bedtime(c, w, "b4", draw=0.7) == "sleep"                         # 累瘫了：同样的签，平时会洗，这回倒头就睡


def test_not_hungry_does_not_eat(tmp_path):
    """刚吃饱不会又去吃；饿了这些选项才回来。"""
    engine = make(tmp_path, "full")
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        w["sim_time"] += 5 * 3600   # 12:00

        def at(hunger):
            c.execute("UPDATE world_need SET value=?, at=? WHERE world_id='w' AND actor_id='neno' AND need='hunger'",
                      (hunger, w["sim_time"]))
            return set(_scores_at(c, w))

        full, peckish, hungry = at(0.1), at(0.32), at(0.6)
    meals = {"eat_home", "eat_out", "cook", "snack"}
    assert not full & meals
    assert "eat_out" in peckish and "snack" not in peckish
    assert {"eat_out", "snack"} <= hungry
