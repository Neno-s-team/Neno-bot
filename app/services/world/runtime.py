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

    if (app.state.world_cognition and app.state.world_cognition.config.enabled
            and app.state.world_cognition_autostart):
        app.state.world_cognition_task = asyncio.create_task(cognition_loop(), name="independent-world-cognition")


async def stop_world(app) -> None:
    app.state.world_stop.set()
    if app.state.world_task is not None:
        await app.state.world_task
    if app.state.world_cognition_task is not None:
        await app.state.world_cognition_task
