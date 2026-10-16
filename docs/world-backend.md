# Independent world backend

Implemented 2026-09-25. This backend does not call the chat service, send Telegram messages, or load the chat persona. The rule engine needs no model calls. Optional, separately configured world cognition can spend model API credits only for explicitly enabled, running worlds; see world-cognition.md.

## What runs

- Versioned BDDL classification metadata plus Neno-authored Chinese everyday types. The import report distinguishes taxonomic nodes, executable capabilities and metadata-only entries; category count is not a count of fully simulated 3D objects.
- A seeded neighborhood template with ten places, three actors, containers, finite shop stock, and individual objects. This is an explicitly authored fictional development world, not Neno's established biography. Existing instances never regenerate on reads or restarts.
- An independent wall-clock scheduler and persistent due-job queue. Paused worlds can advance by a bounded interval or to the next event. Closing the browser or pausing Neno does not pause NPCs or the environment.
- Transactional actions: move, take, put, open, close, turn_on, turn_off, clean, consume, pour, repair, read, inspect, cook, split and buy. Shop prices use fictional credits, not real-world prices.
- Persistent objectives with explicit completion conditions and attempted-action results. Describing a completed action does not complete it. Failed effects roll back; the failure is recorded. Pausing an actor preserves the remaining duration of their in-progress action.
- Deterministic environmental opportunities with probability, category weights, same-kind cooldown, global and per-place daily budgets, and rare-event gating. Draws, candidate selection and outcomes are stored. No random sampling occurs during GET requests.
- Observations recorded separately from truth. Closed-container contents and unseen rooms stay out of an actor's knowledge. Old observations can be stale. Context preview is a bounded development query, not a chat integration.
- Administrative snapshot, entity, incremental-event, action, objective, clock, injection, knowledge and read-only replay APIs. The replay reconstructs entity state at an event; it is not a scheduler rollback or database restore.
- Optional actor decisions and bounded world proposals, separate from the rule scheduler. Accepted short action plans persist and execute without a new model call for every step. No world is opted into model decisions automatically.

## Start without touching chat

From D:\Neno, using the existing ADMIN_TOKEN in .env:

    .\venv\Scripts\python.exe -m app.services.world --db data/world-dev.db --port 8011

This explicit standalone command serves only the world routes on 127.0.0.1:8011 and uses its own development SQLite file. It does not start the main chat application or Telegram gateway. It creates no world until POST /world/worlds.

The existing application also registers /world routes. In that process, WORLD_ENABLED defaults to false, so no world schema or scheduler is started unless explicitly enabled. If later enabled there, the world tables use the existing SQLite file and world_ names. Do not point two independently configured development environments at the chat database for experimentation.

No new credentials or dependencies are required. All /world routes require the x-admin-token header, including read routes. The standalone OpenAPI documentation is at /docs; it describes the schemas but is not the planned visual world console. Actor knowledge routes currently remain admin-only development APIs, not public actor authentication.

## Create a development world

POST /world/worlds with:

    {"world_id":"dev","name":"Independent neighborhood","seed":"dev-1","start_at":"2026-09-25T09:00:00+08:00","running":false,"rate":1}

World time uses the explicitly supplied timestamp and a configured fixed UTC offset (default +480 minutes); this version does not implement daylight-saving transitions. The development scene includes a Neno-labelled participant but does not use the chat account, user history or private profile.

All subsequent mutations require a caller-generated request_id. Retrying an identical request returns the original result; reusing the same ID with different content returns 409. World creation uses the unique world_id and returns 409 if it already exists.

## API map

All paths below begin with /world.

| Method | Path | Result |
| --- | --- | --- |
| GET | /status | Initialization, catalog and scheduler status; chat_connected is false |
| GET | /catalog?q=杯子&limit=20&offset=0 | Paged, versioned metadata search |
| GET / POST | /worlds | List / create worlds |
| GET | /worlds/{world_id}/snapshot | World state, paged entities, event cursor and next due time |
| GET / POST | /worlds/{world_id}/entities/{entity_id} / /worlds/{world_id}/entities | Inspect / explicitly instantiate a catalog item |
| GET | /worlds/{world_id}/events?after=0&limit=100 | Incremental event timeline |
| GET | /worlds/{world_id}/records/{actions,threads,jobs} | Paged execution and objective records |
| GET | /worlds/{world_id}/replay?event_id=ID | Read-only entity-state history, without reexecuting model calls or actions |
| POST | /worlds/{world_id}/control | operation pause / resume / rate |
| POST | /worlds/{world_id}/advance | seconds or next_event; requires global pause |
| POST | /worlds/{world_id}/actions | Submit an action and its explicit parameters |
| POST | /worlds/{world_id}/threads | Create an objective with typed field-equality conditions |
| POST | /worlds/{world_id}/threads/{thread_id}/control | pause / resume / abandon, with a reason |
| POST | /worlds/{world_id}/actors/{actor_id}/control | Suspend / resume that actor and unfinished action |
| POST | /worlds/{world_id}/random | Replace validated random settings; future opportunities only |
| POST | /worlds/{world_id}/injections | Schedule a labelled administrator delivery or weather event |
| GET | /worlds/{world_id}/actors/{actor_id}/knowledge | Stored observations only, without refreshing hidden facts |
| GET | /worlds/{world_id}/actors/{actor_id}/context-preview | Character-limited preview, not sent to any model |

An action returns 202 with action_id and due_at. Effects occur only when world time reaches the due job. Preconditions are checked on completion; a request may be accepted and later fail because a container is closed, inventory is insufficient, or the target changed. Inspect the action result rather than treating 202 as success.

Examples, after creating dev:

    POST /world/worlds/dev/actions
    {"request_id":"open-tools-1","actor_id":"neno","operation":"open","target_id":"toolbox"}

    POST /world/worlds/dev/advance
    {"request_id":"advance-open-1","seconds":3}

    POST /world/worlds/dev/threads
    {"request_id":"lamp-goal-1","actor_id":"neno","title":"修好台灯","conditions":[{"entity_id":"lamp_1","field":"state.broken","equals":false}]}

Use the returned thread_id for the repair request:

    POST /world/worlds/dev/actions
    {"request_id":"repair-1","actor_id":"neno","operation":"repair","target_id":"lamp_1","params":{"tool_id":"screwdriver_1","part_id":"switch_1"}}

    POST /world/worlds/dev/advance
    {"request_id":"advance-repair-1","seconds":600}

The examples omit the optional thread_id because it is generated from the first command; include the actual returned ID to attach the repair to that objective. The repair consumes the compatible switch only when successfully completed.

## Clock and replay boundaries

Manual advancement reports target_time, sim_time and backlog. If the event budget is reached, no events are skipped; advance the remaining target_time - sim_time using a new request_id. Wall-clock catch-up processes at most one simulated day and 500 due jobs per scheduler pass, carrying the remaining elapsed time forward. Errors put only the affected world into error status and preserve the error message. Review it before resuming.

The same seed does not guarantee identical future LLM output. This version makes no LLM calls: NPCs follow authored schedules and explicit objectives/actions are submitted through the API. Open-ended autonomous model planning is not connected. Recorded historical entity replay does not rerun random choices.

## Reproduce complete scenarios

    .\venv\Scripts\python.exe scripts/verify_world_backend.py

The script uses temporary databases only. It exercises failed/successful repairs, resource consumption, objectives, cooking and cleanup, shopping, observer isolation, idempotency under concurrent requests, restart persistence, independent NPC schedules, deterministic sampling, actor pause/resume, and authenticated HTTP routes. It sends no Telegram messages and makes no model calls.

## Source import and 3D cache

    .\venv\Scripts\python.exe scripts/import_world_catalog.py --offline

The local normalized catalog in content/world is sufficient at runtime. The importer uses a pinned BDDL commit; --offline needs the cached source metadata under data/world-source. Omitting --offline retrieves missing metadata only. BDDL's MIT license is retained in content/world/BDDL-LICENSE.txt.

The separate official BEHAVIOR 3.9.0 3D archive is being cached at data/world-assets, not loaded by this backend. Its compressed size is 31,457,673,073 bytes. The ZIP central directory contains 141,485 entries with 34,742,171,984 total uncompressed bytes (about 34.74 GB); keeping both forms would take about 66.20 GB before filesystem overhead. Only the archive is currently being cached. The downloader supports partial-file resume and verifies the official SHA256 before renaming the .part file to .zip. Read download-status.json for the actual state; a .part file is never evidence of a completed download.

Asset licensing is separate from the MIT code/metadata license. This work does not decrypt, extract or accept the asset EULA, and does not integrate those assets as general-purpose Neno visuals. There is no frontend implementation in this delivery; the snapshot/event/knowledge APIs are ready for the later 2D console.
