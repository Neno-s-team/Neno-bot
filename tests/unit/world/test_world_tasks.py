"""手上的事：按规律布置下来；学习才涨进度（不急时多半在看别的）；越急越想学、越没心思玩；
做完算交了；到期没做完有的就过去了、有的欠着被导师问；截止前夜可能熬夜赶。"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.services.world import life, rhythms, tasks
from app.services.world.engine import WorldEngine
from app.services.world.models import Advance, WorldCreate
from app.storage.world_store import WorldStore, entity, save_entity, world

START = datetime.fromisoformat("2026-11-16T10:00:00+08:00")  # 周一


def make(tmp_path, seed="tasks", name="w.db"):
    e = WorldEngine(WorldStore(tmp_path / name))
    e.create(WorldCreate(world_id="w", seed=seed, template="life", rhythms="life", start_at=START))
    return e


@pytest.fixture()
def engine(tmp_path):
    return make(tmp_path)


def run(engine, days):
    for i in range(days * 3):
        engine.advance("w", Advance(request_id=f"s{i}", seconds=8 * 3600, max_events=2000))
    return [e for e in engine.events("w", limit=300000)["items"] if e["actor_id"] == "neno"]


def _only(w, effort=6.0, done=0.0, due_in_hours=72.0, after_due="close", npc=None):
    """清掉这个种子自带的事，放一件确定的。"""
    item = {"id": "t:x", "src": "advisor_task", "title": "补文献综述", "from": "导师", "assigned": w["sim_time"] - 86400,
            "due": w["sim_time"] + due_in_hours * 3600, "effort": effort, "done": done, "status": "open",
            "after_due": after_due, "npc": npc, "known": True}
    tasks.state(w)["items"] = {"t:x": item}
    return item


def _free(c):
    neno = entity(c, "w", "neno")
    neno["state"].pop("doing", None)
    neno["parent_id"] = "my_room"
    save_entity(c, neno)
    return entity(c, "w", "neno")


def _scores(c, w):
    return {ch["id"]: s for ch, _, s in life._candidates(c, w, "neno", _free(c), "k", 10.0)}


def test_things_get_assigned_on_their_own_rhythm(tmp_path):
    events = run(make(tmp_path, "rhythm"), 28)
    assigned = [e["payload"] for e in events if e["kind"] == "task.assigned"]
    titles = [p["title"] for p in assigned]
    assert titles.count("周二课的阅读材料") in (3, 4, 5)          # 每周一次
    assert 1 <= titles.count("组会汇报（轮到你讲）") <= 2         # 三周轮到一次
    for p in assigned:
        due = datetime.fromtimestamp(p["due"])
        if p["title"] == "周二课的阅读材料":
            assert due.isoweekday() == 2 and due.hour == 9
        if p["title"].startswith("组会汇报"):
            assert due.isoweekday() == 4 and due.hour == 14
    assert "开题报告" in {i["title"] for i in _state(tmp_path, "rhythm")}   # 长线的事也在手上


def _state(tmp_path, seed):
    e = make(tmp_path, seed, "probe.db")
    run(e, 20)
    with e.store.transaction() as c:
        return list(world(c, "w")["settings"]["tasks"]["items"].values())


def test_pressure_grows_as_the_deadline_nears(engine):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        item = _only(w, effort=6, due_in_hours=10 * 24)
        far = tasks.pressure(w, item)
        item["due"] = w["sim_time"] + 3 * 24 * 3600
        mid = tasks.pressure(w, item)
        item["due"] = w["sim_time"] + 20 * 3600
        near = tasks.pressure(w, item)
        item["due"] = w["sim_time"] - 3600
        over = tasks.pressure(w, item)
        item["done"] = 6
        assert tasks.pressure(w, item) == 0
    assert far == 0 and 0 < mid < near and near >= 0.9 and over == 1.2


def test_urgency_pulls_her_to_study_and_off_leisure(engine, monkeypatch):
    monkeypatch.setattr(tasks, "_draw", lambda w, key, purpose, low=0.0, high=1.0: low + (high - low) * 0.5)
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        tasks.state(w)["items"] = {}
        calm = _scores(c, w)
        _only(w, effort=8, due_in_hours=30)
        pressed = _scores(c, w)
        push = tasks.drive(w, "neno")
    assert push["study"] > 0.4 and push["leisure"] > 0.1
    assert pressed["study_desk"] - calm["study_desk"] == pytest.approx(push["study"])
    assert calm["scroll"] - pressed["scroll"] == pytest.approx(push["leisure"])
    assert pressed["eat_home"] == pytest.approx(calm["eat_home"])    # 吃饭不受影响
    assert "scroll" in pressed                                       # 急也还是可能刷手机


def test_some_days_she_just_does_not_feel_like_it_until_it_burns(engine, monkeypatch):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        _only(w, effort=8, due_in_hours=60)
        monkeypatch.setattr(tasks, "_draw", lambda w, key, purpose, low=0.0, high=1.0: low)       # 今天最不想碰
        lazy = tasks.drive(w, "neno")["study"]
        monkeypatch.setattr(tasks, "_draw", lambda w, key, purpose, low=0.0, high=1.0: high)
        keen = tasks.drive(w, "neno")["study"]
        _only(w, effort=8, due_in_hours=12)                                                        # 火烧眉毛
        monkeypatch.setattr(tasks, "_draw", lambda w, key, purpose, low=0.0, high=1.0: low)
        burning = tasks.drive(w, "neno")["study"]
    assert 0 < lazy < keen and burning > 2 * lazy


def _study(c, w, hours, key):
    _free(c)
    rhythms.start_activity(c, w, "neno", "study", source="life:study_desk", hours=hours, key=key)
    tasks.begin(c, w, "neno", key)
    doing = entity(c, "w", "neno")["state"]["doing"]
    w["sim_time"] += hours * 3600
    rhythms.end_activity(c, w, "neno")
    return doing


def test_studying_moves_the_task_and_finishing_submits_it(engine, monkeypatch):
    monkeypatch.setattr(tasks, "_draw", lambda w, key, purpose, low=0.0, high=1.0: low + (high - low) * 0.5)
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        item = _only(w, effort=3, due_in_hours=20)
        doing = _study(c, w, 2.0, "s1")
        assert doing["task"] == "t:x" and doing["what"] == "在弄：补文献综述"
        assert item["done"] == pytest.approx(2.0 * 0.8)             # 时长 × 效率（区间中点 0.8）
        assert item["status"] == "open"
        _study(c, w, 2.5, "s2")
        assert item["status"] == "done" and item["done"] == pytest.approx(3.0)
        kinds = [r[0] for r in c.execute("SELECT kind FROM world_events WHERE kind LIKE 'task.%' ORDER BY id")]
    assert kinds == ["task.progress", "task.progress", "task.done"]


def test_when_nothing_is_urgent_she_mostly_reads_her_own_stuff(engine):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        _only(w, effort=6, due_in_hours=15 * 24)        # 还有半个月：不急
        relaxed = 0
        for n in range(60):
            c.execute("SAVEPOINT t")
            _free(c)
            rhythms.start_activity(c, w, "neno", "study", source="life:study_desk", hours=1, key=f"r{n}")
            tasks.begin(c, w, "neno", f"r{n}")
            relaxed += "task" in entity(c, "w", "neno")["state"]["doing"]
            c.execute("ROLLBACK TO t")
            c.execute("RELEASE t")
        _only(w, effort=6, due_in_hours=18)             # 明天就要
        urgent = 0
        for n in range(60):
            c.execute("SAVEPOINT t")
            _free(c)
            rhythms.start_activity(c, w, "neno", "study", source="life:study_desk", hours=1, key=f"u{n}")
            tasks.begin(c, w, "neno", f"u{n}")
            urgent += "task" in entity(c, "w", "neno")["state"]["doing"]
            c.execute("ROLLBACK TO t")
            c.execute("RELEASE t")
    assert 4 <= relaxed <= 22 and urgent == 60


def _jobs(c, like):
    import json
    return [(r[0], json.loads(r[1])) for r in c.execute(
        "SELECT job_id, payload FROM world_jobs WHERE job_id LIKE ? AND status='pending'", (like,))]


def test_due_without_finishing_some_pass_some_linger(engine):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        item = _only(w, effort=10, done=7, due_in_hours=-0.1)           # 做了七成：勉强应付过去
        tasks._due(c, w, {"job_id": "d1", "payload": {"tid": "t:x"}})
        assert item["status"] == "scraped"
        item = _only(w, effort=10, done=2, due_in_hours=-0.1)           # 只做了两成：没弄完
        tasks._due(c, w, {"job_id": "d2", "payload": {"tid": "t:x"}})
        assert item["status"] == "missed" and tasks.open_items(w, "neno") == []
        item = _only(w, effort=10, done=2, due_in_hours=-0.1, after_due="overdue", npc="advisor")
        tasks._due(c, w, {"job_id": "d3", "payload": {"tid": "t:x"}})
        assert item["status"] == "open" and item["overdue"] and tasks.pressure(w, item) == 1.2   # 欠着，最急
        chase = _jobs(c, "npc_contact:advisor:task:chase:%")
        assert len(chase) == 1 and "补文献综述怎么还没交" in chase[0][1]["about"]                 # 导师会来问
        outcomes = [__import__("json").loads(r[0])["outcome"] for r in c.execute(
            "SELECT payload FROM world_events WHERE kind='task.missed' ORDER BY id")]
    assert outcomes == ["scraped", "missed", "overdue"]


def test_finished_in_time_nothing_happens_at_due(engine):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        item = _only(w, effort=5, done=5, due_in_hours=-0.1)
        tasks._due(c, w, {"job_id": "d", "payload": {"tid": "t:x"}})
        assert item["status"] == "open"   # 进度满了由 finish 负责了结；到期这里不再记"没交"
        assert c.execute("SELECT count(*) FROM world_events WHERE kind='task.missed'").fetchone()[0] == 0


def test_advisor_assigns_in_their_own_words(engine):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        item = _only(w, due_in_hours=96, npc="advisor")
        item["known"] = False
        tasks._announce(c, w, {"job_id": "a1", "payload": {"tid": "t:x"}})
        said = _jobs(c, "npc_contact:advisor:task:assign:%")
        assert item["known"] and len(said) == 1 and said[0][1]["about"].startswith("给她布置：补文献综述，")
        assert tasks.open_items(w, "neno")[0]["id"] == "t:x"


def test_night_before_she_may_stay_up(engine, monkeypatch):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        _only(w, effort=6, done=2, due_in_hours=10)
        monkeypatch.setattr(tasks, "_draw", lambda w, key, purpose, low=0.0, high=1.0: low + (high - low) * 0.9)
        assert tasks.crunch(c, w, "neno", "n1") == 0            # 这回没熬（抽签）
        monkeypatch.setattr(tasks, "_draw", lambda w, key, purpose, low=0.0, high=1.0: low + (high - low) * 0.1)
        assert tasks.crunch(c, w, "neno", "n2") == pytest.approx(3.0)   # 剩 4 小时的活，最多熬 3 小时
        _only(w, effort=6, done=2, due_in_hours=72)
        assert tasks.crunch(c, w, "neno", "n3") == 0            # 还有三天：不熬
        _only(w, effort=6, done=5.5, due_in_hours=10)
        assert tasks.crunch(c, w, "neno", "n4") == 0            # 只剩半小时的活：明早再说


def test_crunch_pushes_sleep_back_and_she_works(engine, monkeypatch):
    monkeypatch.setattr(tasks, "_draw", lambda w, key, purpose, low=0.0, high=1.0: low + (high - low) * 0.1)
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        w["sim_time"] += 14.7 * 3600    # 周二 00:42，该睡了
        item = _only(w, effort=6, done=2, due_in_hours=9)
        _free(c)
        entry = next(e for e in w["settings"]["rhythms"]["actors"]["neno"]["routine"] if e["id"] == "sleep")
        task = {"job_id": "routine:neno:sleep:x", "run_at": w["sim_time"],
                "payload": {"actor_id": "neno", "entry": "sleep", "day": 0, "until": None, "begin": w["sim_time"]}}
        rhythms._routine(c, w, task)
        doing = entity(c, "w", "neno")["state"]["doing"]
        assert doing["activity"] == "study" and doing["task"] == "t:x"
        late = _jobs(c, "routine:neno:sleep:x:late")
        assert len(late) == 1 and late[0][1]["crunched"] is True
        w["sim_time"] += 3 * 3600 + 60
        rhythms._routine(c, w, {**task, "job_id": "routine:neno:sleep:x:late", "payload": late[0][1]})
        assert entity(c, "w", "neno")["state"]["doing"]["activity"] == "sleep"      # 熬完去睡，不会再熬一轮
        assert item["done"] > 2


def test_world_starts_with_work_already_in_hand(engine):
    with engine.store.transaction() as c:
        w = world(c, "w")
        items = tasks.open_items(w, "neno")
    assert items and all(i["known"] for i in items)
    assert any(i["done"] > 0 for i in items) or len(items) == 1   # 不是今天才开始读研：有的已经动过了
