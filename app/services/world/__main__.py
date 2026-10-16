from __future__ import annotations

import argparse
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI

from app.routers.world import router
from app.services.world.runtime import start_world, stop_world


def create_app(database: Path, admin_token: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        await start_world(app, database, enabled=True, admin_token=admin_token)
        if app.state.world_engine is None:
            raise RuntimeError(app.state.world_error or "World startup failed")
        try:
            yield
        finally:
            await stop_world(app)

    app = FastAPI(title="Neno Independent World", lifespan=lifespan)
    app.include_router(router)
    return app


if __name__ == "__main__":
    import uvicorn

    load_dotenv()
    parser = argparse.ArgumentParser(description="Run only the world backend; no chat or Telegram imports.")
    parser.add_argument("--db", type=Path, default=Path("data/world-dev.db"))
    parser.add_argument("--port", type=int, default=8011)
    args = parser.parse_args()
    if not os.getenv("ADMIN_TOKEN", "").strip():
        parser.error("Configure ADMIN_TOKEN in .env before starting the management API")
    uvicorn.run(create_app(args.db), host="127.0.0.1", port=args.port)
