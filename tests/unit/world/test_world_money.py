"""钱：每月到账、账单、每笔花销都记账；手头紧时贵的事不太想做（不是不能），钱不够才真的做不成；
家里的吃的会吃完，没了就得买菜；见底了有概率跟家里开口。"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.services.world import city, life, money
from app.services.world.engine import WorldEngine
from app.services.world.models import Advance, WorldCreate
from app.storage.world_store import WorldError, WorldStore, entity, save_entity, world

START = datetime.fromisoformat("2026-11-16T11:30:00+08:00")  # 周一中午


def make(tmp_path, seed="money", name="w.db"):
    e = WorldEngine(WorldStore(tmp_path / name))
    e.create(WorldCreate(world_id="w", seed=seed, template="life", rhythms="life", start_at=START))
    return e


@pytest.fixture()
def engine(tmp_path):
    return make(tmp_path)


def run(engine, days):
    for i in range(days * 3):
        engine.advance("w", Advance(request_id=f"s{i}", seconds=8 * 3600, max_events=2000))
    return [e for e in engine.events("w", limit=200000)["items"] if e["actor_id"] == "neno"]


def _set(c, balance=None, stock=None):
    neno = entity(c, "w", "neno")
    neno["state"].pop("doing", None)
    neno["parent_id"] = "my_room"
    if balance is not None:
        neno["state"]["credits"] = balance
    save_entity(c, neno)
    if stock is not None:
        food = entity(c, "w", "home_food")
        food["state"]["quantity"] = stock
        save_entity(c, food)
    return entity(c, "w", "neno")


def _scores(c, w, **kw):
    return {ch["id"]: s for ch, _, s in life._candidates(c, w, "neno", _set(c, **kw), "k", 10.0)}


def test_starts_with_money_prices_and_a_few_meals_at_home(engine):
    with engine.store.transaction() as c:
        w = world(c, "w")
        assert 30000 <= money.balance(c, w, "neno") <= 50000     # 家境宽裕：有存款
        assert money.feel(c, w, "neno")["word"] == "宽裕" and money.line(c, w, "neno").startswith("宽裕，不用算着花")
        assert entity(c, "w", "canteen_meal")["state"]["price_credits"] == 14
        assert 2 <= money.pantry(c, w) <= 6
        due = {r[0].split(":")[1] for r in c.execute("SELECT job_id FROM world_jobs WHERE kind='money_due' AND status='pending'")}
    assert due == {"family", "stipend", "phone", "membership", "utilities", "bike_pass"}


def test_a_month_of_income_bills_and_books_that_balance(tmp_path):
    engine = make(tmp_path, "ledger")
    with engine.store.transaction() as c:
        start = money.balance(c, world(c, "w"), "neno")
    events = run(engine, 35)
    income = [e["payload"] for e in events if e["kind"] == "money.income"]
    labels = [p["label"] for p in income]
    assert labels.count("妈妈转的生活费") == 1 and labels.count("学校发的补助") >= 1
    bills = {e["payload"]["what"] for e in events if e["kind"] == "money.bill"}
    assert {"话费", "水电燃气", "视频会员", "共享单车月卡"} <= bills
    spent = [e["payload"] for e in events if e["kind"] in ("money.spent", "money.bill")]
    assert {"吃饭", "买菜", "奶茶咖啡"} <= {p["category"] for p in spent}
    gifts = sum(x["amount"] for e in events if e["kind"] == "perturb.happened"
                for x in [{"amount": a["credits"]} for a in e["payload"]["applied"] if "credits" in a])
    with engine.store.transaction() as c:
        w = world(c, "w")
        end = money.balance(c, w, "neno")
        ledger = w["settings"]["money"]["ledger"]
    assert end == pytest.approx(start + sum(p["amount"] for p in income) - sum(p["amount"] for p in spent) + gifts, abs=0.5)
    assert sum(d["spent"] for d in ledger.values()) == pytest.approx(sum(p["amount"] for p in spent), abs=0.5)
    per_day = sum(p["amount"] for p in spent) / 35
    assert 50 <= per_day <= 220   # 不缺钱的学生一天的花销量级
    assert end > 25000            # 正常过日子花不穷


def test_tight_wallet_shifts_what_she_feels_like_but_does_not_forbid(engine):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        rich = _scores(c, w, balance=1400, stock=5)
        poor = _scores(c, w, balance=160, stock=5)
        assert money.feel(c, w, "neno")["word"] in ("有点紧", "很紧")
    assert poor["eat_out"] < rich["eat_out"] - 0.1      # 出去吃：舍不得（已经挑了便宜的那家，还是舍不得）
    assert poor["eat_home"] == pytest.approx(rich["eat_home"])   # 在家吃不花钱：不受影响
    assert poor["read_bed"] == pytest.approx(rich["read_bed"])
    assert "eat_out" in poor and "milk_tea" in poor     # 紧不等于不能
    assert rich["milk_tea"] > poor["milk_tea"] and rich["coffee"] > poor["coffee"]
    wallet = {"t": 1.0, "level": 3}
    assert money.pinch(wallet, 90) < money.pinch(wallet, 20) < 0   # 越贵的越舍不得
    assert money.pinch({"t": 0.0, "level": 0}, 20) > 0             # 刚到账：花点小钱痛快


def test_tight_wallet_picks_cheaper_places(engine):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        choice = next(ch for ch in w["settings"]["rhythms"]["choices"] if ch["id"] == "eat_out")
        prices = {"rich": [], "poor": []}
        for name, balance in (("rich", 1400), ("poor", 150)):
            _set(c, balance=balance)
            wallet = money.feel(c, w, "neno")
            for n in range(40):
                vid, _ = life._pick_venue(w, "neno", choice, "my_room", 12, f"k{n}", wallet)
                prices[name].append(city.price(city.data(w)["by_id"][vid]))
    assert sum(prices["poor"]) / 40 < sum(prices["rich"]) / 40 - 3
    assert max(prices["poor"]) <= 150


def test_cannot_do_what_she_cannot_pay_for(engine):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        broke = _scores(c, w, balance=6, stock=3)
        assert not set(broke) & {"eat_out", "coffee", "study_cafe", "milk_tea"}
        assert "eat_home" in broke
        with pytest.raises(WorldError):
            money.spend(c, w, "neno", 20, "面", "吃饭", "x", exact=True)
        assert money.spend(c, w, "neno", 39, "话费", "bill", "y", force=True, exact=True) == 6   # 账单：有多少扣多少
        assert money.balance(c, w, "neno") == 0


def test_empty_fridge_means_groceries_or_eating_out(engine):
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        stocked = _scores(c, w, balance=1000, stock=5)
        empty = _scores(c, w, balance=1000, stock=0)
        full = _scores(c, w, balance=1000, stock=12)
    assert "eat_home" in stocked and "eat_home" not in empty and "cook" not in empty
    assert empty["groceries"] - stocked["groceries"] == pytest.approx(0.7)
    assert "groceries" not in full


def test_groceries_restock_and_meals_use_it_up(tmp_path):
    engine = make(tmp_path, "pantry")
    events = run(engine, 12)
    with engine.store.transaction() as c:
        stock = money.pantry(c, world(c, "w"))
    home_meals = sum(e["kind"] == "activity.started" and e["payload"]["source"] in ("life:eat_home", "life:cook") for e in events)
    trips = sum(e["kind"] == "money.spent" and e["payload"]["category"] == "买菜" for e in events)
    assert home_meals >= 8 and trips >= 1 and 0 <= stock <= 16


def test_asks_family_only_when_really_low_and_not_every_time(engine, monkeypatch):
    def fix(ask):
        monkeypatch.setattr(money, "_draw", lambda w, key, purpose, low=0.0, high=1.0:
                            low + (high - low) * (ask if purpose == "ask" else 0.5))

    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        w["sim_time"] += 3 * 86400   # 周四：离 12 月初到账还有十来天
        fix(0.1)                     # 这个签：只要够条件就会开口
        _set(c, balance=800)
        assert money.ask_family(c, w, "neno", "a") is False          # 还有钱：不开口
        _set(c, balance=60)
        fix(0.9)
        assert money.ask_family(c, w, "neno", "b") is False          # 见底了，但这回没开口（抽签）
        fix(0.1)
        assert money.ask_family(c, w, "neno", "c") is True
        assert 300 <= money.balance(c, w, "neno") - 60 <= 500
        _set(c, balance=60)
        assert money.ask_family(c, w, "neno", "d") is False          # 刚开过口：冷却里
        w["sim_time"] += 9 * 86400   # 快到账了（只剩一两天）：熬一熬，不开口
        w["settings"]["money"].pop("asked_at")
        assert money.feel(c, w, "neno")["days"] < 4
        assert money.ask_family(c, w, "neno", "e") is False
