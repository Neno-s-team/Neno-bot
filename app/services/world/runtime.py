from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from app.services.world.engine import WorldEngine
from app.services.world.cognition import WorldCognition
from app.services.world.cognition_config import WorldLLMConfig
from app.storage.world_store import WorldStore

log = logging.getLogger(__name__)


async def start_world(app, path: str | Path, enabled: bool | None = None, admin_token: str | None = None) -> None:
    if enabled is None:
        enabled = os.getenv("WORLD_ENABLED", "false").lower() in {"true", "1", "yes", "on"}
    app.state.world_engine = None
    app.state.world_error = None
    app.state.world_task = None
    app.state.world_cognition = None
    app.state.world_cognition_task = None
    app.state.world_cognition_error = None
    app.state.world_npc_task = None
    app.state.world_stop = asyncio.Event()
    app.state.world_rules_enabled = os.getenv("WORLD_RULE_SCHEDULER_ENABLED", "false").lower() in {"true", "1", "yes", "on"}
    app.state.world_cognition_autostart = os.getenv("WORLD_COGNITION_AUTOSTART", "false").lower() in {"true", "1", "yes", "on"}
    app.state.world_admin_token = admin_token if admin_token is not None else os.getenv("ADMIN_TOKEN", "").strip()
    if not enabled:
        return
    try:
        engine = await asyncio.to_thread(WorldEngine, WorldStore(path))
    except Exception as error:
        app.state.world_error = str(error)[:500]
        log.exception("World backend initialization failed; chat lifecycle remains independent")
        return
    app.state.world_engine = engine
    life_world = os.getenv("MIND_WORLD_ID", "").strip()
    if life_world:
        try:
            await asyncio.to_thread(_ensure_life_world, engine, life_world)
        except Exception as error:
            log.warning("Life world bootstrap failed: %s", str(error)[:200])
    try:
        app.state.world_cognition = WorldCognition(engine, WorldLLMConfig.from_env())
    except (ValueError, TypeError) as error:
        app.state.world_cognition_error = str(error)[:300]
        log.warning("World LLM configuration is invalid; rule scheduler remains available")

    async def loop():
        while not app.state.world_stop.is_set():
            try:
                if app.state.world_rules_enabled:
                    app.state.world_tick = await asyncio.to_thread(engine.tick)
            except Exception:
                log.exception("World scheduler iteration failed")
            try:
                await asyncio.wait_for(app.state.world_stop.wait(), timeout=1)
            except TimeoutError:
                pass

    app.state.world_task = asyncio.create_task(loop(), name="independent-world-scheduler")

    async def cognition_loop():
        while not app.state.world_stop.is_set():
            try:
                app.state.world_cognition_last_step = await asyncio.to_thread(app.state.world_cognition.step)
            except Exception:
                log.exception("World cognition iteration failed; physics and time remain independent")
            try:
                await asyncio.wait_for(app.state.world_stop.wait(), timeout=2)
            except TimeoutError:
                pass

    async def npc_loop():
        from app.services.world import npcs
        while not app.state.world_stop.is_set():
            try:
                result = await asyncio.to_thread(npcs.step, engine.store)
                wait = 2 if "idle" not in result else 20
            except Exception:
                log.exception("World NPC step failed; physics and time remain independent")
                wait = 60
            try:
                await asyncio.wait_for(app.state.world_stop.wait(), timeout=wait)
            except TimeoutError:
                pass

    async def weather_loop():
        while not app.state.world_stop.is_set():
            wait = 1200
            try:
                changed = await asyncio.to_thread(_refresh_weather, engine, life_world)
                if changed is None:
                    wait = 300  # 没取到：过会儿再试，世界先用着旧的（超过 3 小时退回随机天气）
            except Exception:
                log.exception("World weather refresh failed; world keeps running")
                wait = 300
            try:
                await asyncio.wait_for(app.state.world_stop.wait(), timeout=wait)
            except TimeoutError:
                pass

    app.state.world_weather_task = None
    if life_world and os.getenv("WORLD_REAL_WEATHER", "true").lower() in {"true", "1", "yes", "on"}:
        app.state.world_weather_task = asyncio.create_task(weather_loop(), name="world-weather")

    async def real_loop():
        while not app.state.world_stop.is_set():
            wait = 6 * 3600
            try:
                result = await asyncio.to_thread(_refresh_real, engine, life_world)
                if result is None:
                    wait = 1800  # 一样都没取到：半小时后再试，世界先用着旧的（超过 3 天退回生成的题目）
            except Exception:
                log.exception("World real-feed refresh failed; world keeps running")
                wait = 1800
            try:
                await asyncio.wait_for(app.state.world_stop.wait(), timeout=wait)
            except TimeoutError:
                pass

    app.state.world_real_task = None
    if life_world and os.getenv("WORLD_REAL_FEED", "true").lower() in {"true", "1", "yes", "on"}:
        app.state.world_real_task = asyncio.create_task(real_loop(), name="world-real")

    if os.getenv("WORLD_NPC_ENABLED", "false").lower() in {"true", "1", "yes", "on"}:
        app.state.world_npc_task = asyncio.create_task(npc_loop(), name="world-npcs")

    async def surf_loop():
        while not app.state.world_stop.is_set():
            try:
                await asyncio.to_thread(_surf_once, engine, life_world)
            except Exception:
                log.exception("World surf session failed; world keeps running")
            try:
                await asyncio.wait_for(app.state.world_stop.wait(), timeout=45)
            except TimeoutError:
                pass

    # 上网要调模型：跟着"世界可以调便宜模型"的开关（WORLD_NPC_ENABLED）走，另可用 WORLD_SURF_ENABLED=false 单独关
    app.state.world_surf_task = None
    if (life_world and os.getenv("WORLD_NPC_ENABLED", "false").lower() in {"true", "1", "yes", "on"}
            and os.getenv("WORLD_SURF_ENABLED", "true").lower() in {"true", "1", "yes", "on"}):
        app.state.world_surf_task = asyncio.create_task(surf_loop(), name="world-surf")

    if (app.state.world_cognition and app.state.world_cognition.config.enabled
            and app.state.world_cognition_autostart):
        app.state.world_cognition_task = asyncio.create_task(cognition_loop(), name="independent-world-cognition")


async def stop_world(app) -> None:
    app.state.world_stop.set()
    if app.state.world_task is not None:
        await app.state.world_task
    if app.state.world_cognition_task is not None:
        await app.state.world_cognition_task
    if getattr(app.state, "world_npc_task", None) is not None:
        await app.state.world_npc_task
    if getattr(app.state, "world_weather_task", None) is not None:
        await app.state.world_weather_task
    if getattr(app.state, "world_real_task", None) is not None:
        await app.state.world_real_task
    if getattr(app.state, "world_surf_task", None) is not None:
        await app.state.world_surf_task


def _refresh_weather(engine, world_id: str, fetch=None) -> bool | None:
    """取她城市的真实天气落进世界。没接城市返回 False；联网失败返回 None。联网在事务外。"""
    from app.services.world import city, weather
    from app.storage.world_store import world as load_world

    with engine.store.transaction() as c:
        w = load_world(c, world_id)
    if not city.enabled(w) or "street" not in city.config(w).get("places", {}):
        return False
    lat, lon = city.point(w, "street")
    try:
        report = weather.parse((fetch or weather.fetch)(lat, lon))
    except Exception as error:
        log.warning("Real weather unavailable: %s", str(error)[:120])
        return None
    with engine.store.transaction(write=True) as c:
        w = load_world(c, world_id)
        return weather.apply(c, w, report)


def _surf_once(engine, world_id: str, ask=None, fetch=None) -> list[str] | None:
    """她正在刷手机就替她上一次网（事务外联网、调模型），看到的落进世界。没轮到返回 None。"""
    from app.services.world import npcs, surf
    from app.services.world.randomness import sample
    from app.storage.world_store import world as load_world

    with engine.store.transaction() as c:
        job = surf.due(c, load_world(c, world_id))
    if job is None:
        return None
    draws = iter(range(10 ** 6))
    result = surf.session(job["brief"], job["feeds"], job["seen"],
                          lambda n: int(sample(job["seed"], f"surf:{job['job']}", f"pick:{next(draws)}") * n),
                          steps=job["steps"], ask=ask or npcs._request, fetch=fetch, block=job["block"])
    with engine.store.transaction(write=True) as c:
        w = load_world(c, world_id)
        landed = surf.land(c, w, job, result)
        engine._save_world(c, w)
    log.info("Surf session: %s pages, %s calls, landed %s", len(result["pages"]), result["calls"], len(landed))
    return landed


def _refresh_real(engine, world_id: str, fetch=None) -> dict | bool | None:
    """现实同步：取在映的电影、城里这周的活动、热门的剧落进世界。没启用返回 False；一样都没取到返回 None。联网在事务外。"""
    from app.services.world import city, real
    from app.storage.world_store import world as load_world

    with engine.store.transaction() as c:
        w = load_world(c, world_id)
    if not real.enabled(w) or not city.enabled(w):
        return False
    feed = (fetch or real.fetch_all)(real.config(w), real.state(w).get("cache", {}))
    for error in feed.get("errors", []):
        log.warning("Real feed unavailable: %s", error)
    if not any(feed.get(part) for part in ("films", "shows", "events")):
        return None
    with engine.store.transaction(write=True) as c:
        w = load_world(c, world_id)
        result = real.apply(c, w, feed)
        engine._save_world(c, w)
    log.info("Real feed synced: %s", result)
    return result


def _ensure_life_world(engine, world_id: str) -> None:
    """她的生活世界不存在就按 life 模板建一个（时间 1:1，从现在开始跑）。"""
    from datetime import datetime, timezone
    from app.services.world.models import WorldCreate

    from app.storage.world_store import world as load_world

    with engine.store.transaction() as c:
        exists = c.execute("SELECT 1 FROM world_worlds WHERE world_id=?", (world_id,)).fetchone()
    if not exists:
        engine.create(WorldCreate(world_id=world_id, name="Neno 的生活", seed=os.getenv("MIND_WORLD_SEED", world_id),
                                  template="life", rhythms="life", running=True, start_at=datetime.now(timezone.utc)))
        log.info("Life world %s created", world_id)
    with engine.store.transaction(write=True) as c:
        w = load_world(c, world_id)
        changed = False
        if not w["settings"].get("mind_linked"):
            # 标记：这个世界接着她的大脑（挑事会读她的打算/心愿）
            w["settings"]["mind_linked"] = True
            changed = True
        if not w["settings"].get("npcs"):
            from app.services.world import npcs
            npcs.setup(c, w, "life")
            changed = True
        # 模板里新加的东西（比如新添的家电）补进已经在跑的世界；已有的不动
        from app.services.world.scene_builder import build_scene
        from app.storage.world_store import save_entity
        have = {r[0] for r in c.execute("SELECT entity_id FROM world_entities WHERE world_id=?", (world_id,))}
        values, _ = build_scene(c, engine.catalog, w, w["settings"].get("template", "life"))
        for value in values:
            if value["entity_id"] not in have and (value["parent_id"] is None or value["parent_id"] in have):
                save_entity(c, value)
                have.add(value["entity_id"])
                changed = True
                log.info("Life world %s gained %s", world_id, value["entity_id"])
        from app.services.world import city, rhythms
        fresh = rhythms.load("life")
        if w["settings"].get("rhythms") != fresh:
            # 作息/选项/参数以文件为准：改了文件重启就生效，她的状态（需求、喜好、习惯）不动
            w["settings"]["rhythms"] = fresh
            changed = True
            log.info("Life world %s rhythms refreshed (%s)", world_id, fresh.get("version"))
        from app.services.world import happenings
        if happenings.enabled(w) and not c.execute(
                "SELECT 1 FROM world_jobs WHERE world_id=? AND kind='happen_plan' AND status='pending'", (world_id,)).fetchone():
            happenings.setup(c, w)  # 第一次启用世界事件：补上正在发生的和未来几天的
            changed = True
        from app.services.world import tasks
        if tasks.enabled(w) and not w["settings"].get("tasks", {}).get("ready"):
            tasks.setup(c, w)  # 第一次启用：补上她手上已经有的事
            changed = True
        from app.services.world import money
        if money.config(w) and not w["settings"].get("money", {}).get("ready"):
            money.setup(c, w)  # 第一次启用钱：定余额、价格、囤货，排到账和账单
            changed = True
        from app.storage.world_store import entity as load_entity
        for place, name in city.anchor_labels(w).items():
            if place in have:
                value = load_entity(c, world_id, place)
                if value["label"] != name:
                    value["label"] = name  # 城里的真名
                    save_entity(c, value)
        if changed:
            engine._save_world(c, w)
