"""W5 扰动与调节器：分档抽签、状态放大权重、调节器只调频率、全部留痕可重放。"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

from app.services.world import perturbations, rhythms
from app.services.world.engine import WorldEngine
from app.services.world.models import Advance, WorldCreate
from app.storage.world_store import WorldStore, entity, save_entity, world

START = datetime.fromisoformat("2026-11-09T07:00:00+08:00")


def make(tmp_path, seed="neno-world-1", name="w.db"):
    engine = WorldEngine(WorldStore(tmp_path / name))
    engine.create(WorldCreate(world_id="w", seed=seed, rhythms="neighborhood", start_at=START))
    return engine


def run(engine, days):
    for i in range(days * 3):
        engine.advance("w", Advance(request_id=f"s{i}", seconds=8 * 3600, max_events=2000))
    return engine.events("w", limit=50000)["items"]


def test_every_opportunity_is_logged_and_replays(tmp_path):
    a = run(make(tmp_path, "r1", "a.db"), 4)
    b = run(make(tmp_path, "r1", "b.db"), 4)
    c = run(make(tmp_path, "r2", "c.db"), 4)
    pick = lambda ev: [(x["sim_time"], x["kind"], json.dumps(x["payload"], sort_keys=True))  # noqa: E731
                       for x in ev if x["kind"].startswith("perturb")]
    assert pick(a) == pick(b) and pick(a) != pick(c)
    opps = [x for x in a if x["kind"] == "perturb.opportunity"]
    gaps = [(y["sim_time"] - x["sim_time"]) / 3600 for x, y in zip(opps, opps[1:])]
    assert all(1 <= g <= 3 for g in gaps) and len(set(round(g, 2) for g in gaps)) > 5
    assert all(set(x["payload"]["draws"]) == {"small", "medium", "large"} for x in opps)
    assert not any(x["kind"] == "random.opportunity" for x in a)  # 旧随机机会停用


def test_tiers_are_rare_in_order(tmp_path):
    tiers = Counter()
    for seed in range(4):
        tiers.update(x["payload"]["tier"] for x in run(make(tmp_path, f"t{seed}", f"t{seed}.db"), 14)
                     if x["kind"] == "perturb.happened")
    assert tiers["small"] > 3 * tiers["medium"] > 0
    assert tiers["large"] < tiers["medium"]


def test_state_boosts_weight_without_forcing(tmp_path):
    engine = make(tmp_path)
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        cold = next(e for e in perturbations.config(w)["pool"] if e["id"] == "cold")
        neno = entity(c, "w", "neno")
        base = perturbations._boost(c, w, neno, cold["boost"])
        rows = rhythms.settle(c, w, "neno")
        rows["fatigue"]["value"] = 0.8
        rows["fatigue"]["data"]["mods"] = [{"mult": 1.4, "label": "熬过头了", "until": w["sim_time"] + 3600}]
        rhythms._reschedule(c, w, rows["fatigue"])
        tired = perturbations._boost(c, w, neno, cold["boost"])
    assert base == 1.0 and tired == 12.0


def test_regulator_raises_when_calm_and_lowers_when_busy(tmp_path):
    engine = make(tmp_path)
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        calm = perturbations.regulator(c, w)
        for n in range(6):
            c.execute("INSERT INTO world_events(world_id,event_key,sim_time,kind,payload) VALUES ('w',?,?,?,?)",
                      (f"fake{n}", w["sim_time"] - 3600, "perturb.happened", json.dumps({"tier": "medium"})))
        busy = perturbations.regulator(c, w)
    assert calm == 2.0 and busy < 0.5


def test_sleepers_do_not_lose_change_and_conditions_recover(tmp_path):
    engine = make(tmp_path, "sleep")
    events = run(engine, 14)
    asleep = {}
    for x in events:
        if x["kind"] == "activity.started" and x["payload"]["activity"] == "sleep":
            asleep[x["actor_id"]] = x["sim_time"]
        elif x["kind"] == "activity.ended" and x["payload"]["activity"] == "sleep":
            asleep.pop(x["actor_id"], None)
        elif x["kind"] == "perturb.happened" and x["actor_id"] in asleep:
            assert x["payload"]["id"] not in {"lost_change", "treat", "sprain", "thing_broke"}, x
    for x in [e for e in events if e["kind"] == "perturb.happened" and any("condition" in a for a in e["payload"]["applied"])]:
        recovered = [e for e in events if e["kind"] == "perturb.recovered" and e["actor_id"] == x["actor_id"]
                     and e["sim_time"] > x["sim_time"]]
        hours = next(a["hours"] for a in x["payload"]["applied"] if "condition" in a)
        if x["sim_time"] + hours * 3600 < events[-1]["sim_time"]:
            assert recovered


def test_condition_shows_in_body_feeling(tmp_path):
    engine = make(tmp_path)
    with engine.store.transaction(write=True) as c:
        w = world(c, "w")
        neno = entity(c, "w", "neno")
        neno["state"]["conditions"] = [{"label": "感冒了，嗓子疼", "until": w["sim_time"] + 3600}]
        save_entity(c, neno)
        assert rhythms.felt(c, w, "neno")["身体"] == "感冒了，嗓子疼"


def test_weather_follows_current_state(tmp_path):
    events = run(make(tmp_path, "wx"), 10)
    weather = "clear"
    for x in [e for e in events if e["kind"] == "perturb.happened" and e["payload"]["target"] == "street"]:
        label, new = x["payload"]["label"], x["payload"]["applied"][0]["weather"]
        if label == "雨停了，放晴":
            assert weather == "rain"
        assert new != weather
        weather = new


def test_regulator_multiplier_reaches_the_draw(tmp_path, monkeypatch):
    seen = []
    real = perturbations._candidates

    def spy(c, w, tier, scale):
        seen.append(tier)
        return real(c, w, tier, scale)

    monkeypatch.setattr(perturbations, "_candidates", spy)
    engine = make(tmp_path, "reg")
    monkeypatch.setattr(perturbations, "regulator", lambda c, w: 2.0)
    events = run(engine, 3)
    opps = [x["payload"] for x in events if x["kind"] == "perturb.opportunity"]
    # 平静时 small 概率 0.25×2：抽签值落在 [0.25, 0.5) 的机会也会去挑候选
    lifted = [p for p in opps if 0.25 <= p["draws"]["small"] < 0.5 and p["draws"]["medium"] >= 0.05 and p["draws"]["large"] >= 0.012]
    assert lifted and all(p["regulator"] == 2.0 for p in opps)
    assert seen.count("small") >= sum(1 for p in opps if p["draws"]["small"] < 0.5 and p["draws"]["medium"] >= 0.05 and p["draws"]["large"] >= 0.012)


def test_money_amounts_vary(tmp_path):
    amounts = set()
    for seed in range(5):
        for x in run(make(tmp_path, f"m{seed}", f"m{seed}.db"), 14):
            if x["kind"] == "perturb.happened":
                amounts.update(a["credits"] for a in x["payload"]["applied"] if "credits" in a and a["credits"] < 0)
    assert len(amounts) >= 3 and all(-8 <= a <= -2 for a in amounts)
