from contextlib import asynccontextmanager
import asyncio

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from app.routers import chat, session, system
from app.routers import operations as operations_router
from app.storage.db import init_db, DB_PATH
from app.routers import world as world_router
from app.services.world.runtime import start_world, stop_world
from app.utils.logging_utils import configure_safe_logging
from app import config

configure_safe_logging()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    from app.services.chat.maintenance import worker
    maintenance_stop = asyncio.Event()
    maintenance_task = asyncio.create_task(worker(maintenance_stop), name='chat-maintenance')
    await start_world(_, DB_PATH)
    from app.services.mind import scheduler as mind_scheduler

    if config.MIND_ENABLED and getattr(config, "MIND_ASYNC_DELIVERY", False):
        mind_scheduler.start()
    try:
        yield
    finally:
        mind_scheduler.stop()
        maintenance_stop.set()
        await maintenance_task
        await stop_world(_)


app = FastAPI(lifespan=lifespan)
if config.CORS_ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.CORS_ALLOWED_ORIGINS),
        allow_credentials=False,
        allow_methods=["POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Internal-Token"],
    )

app.include_router(system.router)
app.include_router(operations_router.router)
app.include_router(session.router)
app.include_router(chat.router)
from app.routers import mind as mind_router

app.include_router(mind_router.router)
app.include_router(world_router.router)
