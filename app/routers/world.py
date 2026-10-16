from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from app.services.world.engine import WorldEngine
from app.services.world.cognition_models import CognitionUpdate
from app.services.world.models import ActionRequest, ActorControl, Advance, Control, EntityCreate, Inject, RandomUpdate, ThreadControl, ThreadRequest, WorldCreate
from app.storage.world_store import WorldError


def authorize(request: Request, x_admin_token: str | None = Header(default=None)) -> None:
    expected = getattr(request.app.state, "world_admin_token", "")
    if not expected or not x_admin_token or not hmac.compare_digest(expected.encode(), x_admin_token.encode()):
        raise HTTPException(403, "Invalid or unconfigured ADMIN_TOKEN")


router = APIRouter(prefix="/world", tags=["world"], dependencies=[Depends(authorize)])


def backend(request: Request) -> WorldEngine:
    engine = getattr(request.app.state, "world_engine", None)
    if engine is None:
        raise HTTPException(503, "World backend is disabled or unavailable")
    return engine


def call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except WorldError as error:
        raise HTTPException(error.status, str(error)) from error


def cognition(request: Request):
    service = getattr(request.app.state, "world_cognition", None)
    if service is None:
        raise HTTPException(503, getattr(request.app.state, "world_cognition_error", None) or "World cognition is unavailable")
    return service


@router.get("/status")
def status(request: Request):
    engine = getattr(request.app.state, "world_engine", None)
    return {"enabled": engine is not None, "chat_connected": False,
            "catalog": engine.catalog.report if engine else None,
            "initialization_error": getattr(request.app.state, "world_error", None),
            "last_tick": getattr(request.app.state, "world_tick", None),
            "cognition": request.app.state.world_cognition.status() if getattr(request.app.state, "world_cognition", None) else None,
            "cognition_error": getattr(request.app.state, "world_cognition_error", None),
            "last_decision": getattr(request.app.state, "world_cognition_last_step", None)}


@router.get("/catalog")
def catalog(q: str = Query("", max_length=200), limit: int = Query(20, ge=1, le=100),
            offset: int = Query(0, ge=0), version: str | None = None, engine=Depends(backend)):
    return call(engine.catalog.search, q, limit, offset, version)


@router.post("/worlds", status_code=201)
def create(body: WorldCreate, engine=Depends(backend)):
    return call(engine.create, body)


@router.get("/worlds")
def worlds(engine=Depends(backend)):
    return {"items": call(engine.list_worlds)}


@router.get("/worlds/{wid}/snapshot")
def snapshot(wid: str, parent_id: str | None = None, limit: int = Query(200, ge=1, le=1000),
             offset: int = Query(0, ge=0), engine=Depends(backend)):
    return call(engine.snapshot, wid, parent_id, limit, offset)


@router.get("/worlds/{wid}/entities/{entity_id}")
def get_entity(wid: str, entity_id: str, engine=Depends(backend)):
    return call(engine.get_entity, wid, entity_id)


@router.post("/worlds/{wid}/entities", status_code=201)
def create_entity(wid: str, body: EntityCreate, engine=Depends(backend)):
    return call(engine.create_entity, wid, body)


@router.get("/worlds/{wid}/events")
def events(wid: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), engine=Depends(backend)):
    return call(engine.events, wid, after, limit)


@router.get("/worlds/{wid}/replay")
def replay(wid: str, event_id: int = Query(ge=1), engine=Depends(backend)):
    return call(engine.history_view, wid, event_id)


@router.get("/worlds/{wid}/records/{collection}")
def records(wid: str, collection: str, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), engine=Depends(backend)):
    return call(engine.records, wid, collection, limit, offset)


@router.get("/worlds/{wid}/actors/{actor_id}/knowledge")
def knowledge(wid: str, actor_id: str, q: str = Query("", max_length=200), limit: int = Query(100, ge=1, le=500),
              offset: int = Query(0, ge=0), engine=Depends(backend)):
    return call(engine.knowledge, wid, actor_id, q, limit, offset)


@router.get("/worlds/{wid}/actors/{actor_id}/context-preview")
def preview(wid: str, actor_id: str, q: str = Query("", max_length=200), max_chars: int = Query(4000, ge=256, le=16000), engine=Depends(backend)):
    return call(engine.context_preview, wid, actor_id, q, max_chars)


@router.post("/worlds/{wid}/control")
def control(wid: str, body: Control, engine=Depends(backend)):
    return call(engine.control, wid, body)


@router.post("/worlds/{wid}/advance")
def advance(wid: str, body: Advance, engine=Depends(backend)):
    return call(engine.advance, wid, body)


@router.post("/worlds/{wid}/actions", status_code=202)
def action(wid: str, body: ActionRequest, engine=Depends(backend)):
    return call(engine.submit_action, wid, body)


@router.post("/worlds/{wid}/threads", status_code=201)
def thread(wid: str, body: ThreadRequest, engine=Depends(backend)):
    return call(engine.submit_thread, wid, body)


@router.post("/worlds/{wid}/actors/{actor_id}/control")
def actor_control(wid: str, actor_id: str, body: ActorControl, engine=Depends(backend)):
    return call(engine.actor_control, wid, actor_id, body)


@router.post("/worlds/{wid}/threads/{thread_id}/control")
def thread_control(wid: str, thread_id: str, body: ThreadControl, engine=Depends(backend)):
    return call(engine.thread_control, wid, thread_id, body)


@router.post("/worlds/{wid}/random")
def random(wid: str, body: RandomUpdate, engine=Depends(backend)):
    return call(engine.set_random, wid, body)


@router.post("/worlds/{wid}/injections", status_code=202)
def inject(wid: str, body: Inject, engine=Depends(backend)):
    return call(engine.inject, wid, body)


@router.get("/worlds/{wid}/cognition")
def cognition_status(wid: str, service=Depends(cognition)):
    return call(service.status, wid)


@router.post("/worlds/{wid}/cognition")
def cognition_configure(wid: str, body: CognitionUpdate, service=Depends(cognition)):
    return call(service.configure, wid, body)


@router.get("/worlds/{wid}/decisions")
def decisions(wid: str, limit: int = Query(20, ge=1, le=100), before: int | None = Query(None, ge=1),
              include_context: bool = False, service=Depends(cognition)):
    return call(service.decisions, wid, limit, before, include_context)
