# SPDX-License-Identifier: Apache-2.0
"""
FastAPI application — the Phase 1 control-room server.

Exposes a small REST API over the Phase 0 engine plus a WebSocket live event
stream, and serves the built React frontend as static assets from the same
process (one container, one port).

Endpoints:
    POST   /api/runs                     start a run (non-blocking)
    GET    /api/runs                     list runs (?q= filter)
    GET    /api/runs/{ref}               run metadata + final result
    GET    /api/runs/{ref}/events        historical events (?after_seq=)
    WS     /api/runs/{ref}/stream        live stream (replay then tail)
    GET    /api/name/suggest?topic=      suggested codename + description
    GET    /api/models                   selectable model string(s)
    GET    /api/health                   liveness probe

Keys never touch the browser — the model list and all provider credentials come
from server-side settings/env (Phase 0 .env). Full BYO-key browser UX is Phase 3.
"""

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from matrix_studio import service
from matrix_studio.api.manager import RunManager, TERMINAL_EVENTS, event_row_to_wire
from matrix_studio.naming import generate_run_name
from matrix_studio.settings import get_settings
from matrix_studio.storage import Database

logger = logging.getLogger(__name__)

# Location of the built frontend (populated by the Vite build / Docker stage).
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


# --------------------------------------------------------------------------- #
# Request/response models (documentation + light validation only)
# --------------------------------------------------------------------------- #
class PersonaModel(BaseModel):
    name: str
    persona: str
    goals: List[str] = Field(default_factory=list)


class RunConfigModel(BaseModel):
    max_messages: Optional[int] = None
    generate_avatars: Optional[bool] = None


class SummaryConfigModel(BaseModel):
    """Optional summary generation config (Phase 1.5). Omitted → default
    (enabled with the full field set, no focus)."""

    enabled: bool = True
    fields: Optional[List[str]] = None
    focus: Optional[str] = None


class CreateRunModel(BaseModel):
    topic: str
    cast: List[PersonaModel]
    config: RunConfigModel = Field(default_factory=RunConfigModel)
    model: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    # Phase 1.5: optional summary config; defaults applied server-side when omitted.
    summary: Optional[SummaryConfigModel] = None


class SummaryRequestModel(BaseModel):
    """Body for on-demand (re)generation of a run's structured summary."""

    fields: Optional[List[str]] = None
    focus: Optional[str] = None
    model: Optional[str] = None


class CreateThreadModel(BaseModel):
    """Open an aside thread. persona_name is required only for target='persona'."""

    target: str  # 'analyst' | 'persona' | 'room'
    persona_name: Optional[str] = None


class ThreadMessageModel(BaseModel):
    """Post a user message into an aside thread."""

    content: str


def _run_summary(run: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a runs-table row (+ derived stats) for list/detail responses."""
    return {
        "run_id": run["id"],
        "name": run.get("name"),
        "description": run.get("description"),
        "slug": run.get("slug"),
        "topic": run["topic"],
        "status": run.get("status"),
        "turn_count": run.get("turn_count", 0),
        "total_cost_usd": run.get("total_cost_usd", 0.0),
        "created_at": run.get("created_at"),
        "completed_at": run.get("completed_at"),
    }


def create_app(db_path: Optional[str] = None) -> FastAPI:
    """
    Build the FastAPI app. ``db_path`` overrides the default settings-derived
    path (used by tests).
    """
    settings = get_settings()
    resolved_db_path = db_path or str(Path(settings.data_dir) / "matrix_studio.db")

    db = Database(resolved_db_path)
    manager = RunManager(db)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await db.connect()
        logger.info("Database ready at %s", resolved_db_path)
        yield
        await manager.shutdown()
        await db.close()

    app = FastAPI(
        title="TheMatrix Simulation Studio",
        description="Control-room UI over the multi-agent conversation engine.",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.db = db
    app.state.manager = manager

    # ----------------------------- REST API ------------------------------- #
    @app.get("/api/health")
    async def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/models")
    async def models() -> Dict[str, Any]:
        """Model string(s) selectable in the new-run form. Keys stay server-side."""
        return {
            "default": settings.litellm_model,
            "models": [settings.litellm_model],
        }

    @app.get("/api/name/suggest")
    async def suggest_name(topic: str = Query(...)) -> Dict[str, str]:
        """Suggest a memorable codename + description for the new-run form."""
        result = await generate_run_name(
            topic=topic, name_exists=db.name_exists
        )
        return {
            "name": result["name"],
            "description": result["description"],
            "slug": result["slug"],
            "source": result["source"],
        }

    @app.post("/api/runs", status_code=201)
    async def create_run(body: CreateRunModel) -> Dict[str, Any]:
        request = body.model_dump(exclude_none=True)
        if not request.get("cast"):
            raise HTTPException(status_code=422, detail="At least one persona is required")
        result = await manager.create_run(request)
        return result

    @app.get("/api/runs")
    async def list_runs(q: Optional[str] = Query(default=None)) -> Dict[str, Any]:
        runs = await db.list_runs(q=q)
        return {"runs": [_run_summary(r) for r in runs]}

    @app.get("/api/runs/{ref}")
    async def get_run(ref: str) -> Dict[str, Any]:
        run = await db.get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        stats = await db.get_run_stats(run["id"])
        run.update(stats)
        summary = _run_summary(run)

        # Attach the final result (conversation + agents) from the snapshot when
        # the run is complete, so a reloaded run shows full dossier data.
        result: Optional[Dict[str, Any]] = None
        snapshot = await db.get_snapshot(run["id"])
        if snapshot is not None:
            result = {
                "conversation": snapshot.conversation,
                "agents": {
                    name: agent.model_dump() for name, agent in snapshot.agents.items()
                },
                "total_turns": snapshot.total_turns,
                "total_cost_usd": sum(
                    a.total_cost_usd for a in snapshot.agents.values()
                ),
            }

        # Always expose the requested cast so the UI can render cards even for a
        # still-running or failed run without a snapshot.
        try:
            cast = json.loads(run["cast_json"])
        except (KeyError, json.JSONDecodeError):
            cast = []
        try:
            config = json.loads(run["config_json"]) if run.get("config_json") else {}
        except json.JSONDecodeError:
            config = {}

        # Phase 1.5: attach any stored summaries (generated + imported original)
        # so a reloaded run shows its analysis panel immediately.
        summary_rows = await db.get_summaries(run["id"])
        generated = next((r for r in summary_rows if r["kind"] == "generated"), None)
        imported = next((r for r in summary_rows if r["kind"] == "imported"), None)

        return {
            **summary,
            "cast": cast,
            "config": config,
            "result": result,
            "summary": {"generated": generated, "imported": imported},
        }

    @app.get("/api/runs/{ref}/events")
    async def get_events(
        ref: str,
        after_seq: int = Query(default=-1),
        limit: Optional[int] = Query(default=None),
    ) -> Dict[str, Any]:
        run = await db.get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        rows = await db.get_events_after(run["id"], after_seq=after_seq, limit=limit)
        events = [event_row_to_wire(r) for r in rows]
        return {"run_id": run["id"], "events": events}

    # ------------------- Phase 1.5: summary + aside threads ---------------- #
    # All routes below are ADDITIVE and READ-ONLY over the canonical run: they
    # read the run's transcript/cast and write only to the new summaries /
    # threads / thread_messages tables. They never emit a canonical event,
    # mutate the snapshot, or change the run's recorded cost. Summaries and
    # aside replies are model-generated ANALYSIS, labeled as such by the UI.

    async def _require_run(ref: str) -> Dict[str, Any]:
        run = await db.get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return run

    def _shape_summaries(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Split stored summaries into generated + imported for the client."""
        generated = next((r for r in rows if r["kind"] == "generated"), None)
        imported = next((r for r in rows if r["kind"] == "imported"), None)
        return {"generated": generated, "imported": imported}

    @app.get("/api/runs/{ref}/summary")
    async def get_summary(ref: str) -> Dict[str, Any]:
        run = await _require_run(ref)
        rows = await db.get_summaries(run["id"])
        return {"run_id": run["id"], **_shape_summaries(rows)}

    @app.post("/api/runs/{ref}/summary")
    async def post_summary(
        ref: str, body: SummaryRequestModel = SummaryRequestModel()
    ) -> Dict[str, Any]:
        run = await _require_run(ref)
        if run.get("status") != "complete":
            raise HTTPException(
                status_code=409,
                detail="Summary can only be generated for a completed run.",
            )
        cfg = service.summary_config(run)
        fields = body.fields or cfg["fields"]
        focus = body.focus if body.focus is not None else cfg["focus"]
        saved = await service.generate_and_store_summary(
            db, run, fields=fields, focus=focus, model=body.model
        )
        rows = await db.get_summaries(run["id"])
        return {"run_id": run["id"], "generated": saved, **_shape_summaries(rows)}

    @app.get("/api/runs/{ref}/threads")
    async def list_threads(ref: str) -> Dict[str, Any]:
        run = await _require_run(ref)
        threads = await db.list_threads(run["id"])
        return {"run_id": run["id"], "threads": threads}

    @app.post("/api/runs/{ref}/threads", status_code=201)
    async def create_thread(ref: str, body: CreateThreadModel) -> Dict[str, Any]:
        run = await _require_run(ref)
        try:
            thread = await service.create_thread(
                db, run, target=body.target, persona_name=body.persona_name
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return thread

    @app.get("/api/threads/{thread_id}")
    async def get_thread(thread_id: str) -> Dict[str, Any]:
        thread = await db.get_thread(thread_id)
        if not thread:
            raise HTTPException(status_code=404, detail="Thread not found")
        messages = await db.get_thread_messages(thread_id)
        cost = await db.thread_cost(thread_id)
        return {**thread, "messages": messages, "total_cost_usd": cost}

    @app.post("/api/threads/{thread_id}/messages", status_code=201)
    async def post_thread_message(
        thread_id: str, body: ThreadMessageModel
    ) -> Dict[str, Any]:
        thread = await db.get_thread(thread_id)
        if not thread:
            raise HTTPException(status_code=404, detail="Thread not found")
        if not body.content.strip():
            raise HTTPException(status_code=422, detail="Message content is required")
        run = await db.get_run(thread["run_id"])
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        reply = await service.post_aside_message(
            db, run, thread, user_message=body.content.strip()
        )
        cost = await db.thread_cost(thread_id)
        return {"thread_id": thread_id, "reply": reply, "total_cost_usd": cost}

    # --------------------------- WebSocket stream -------------------------- #
    @app.websocket("/api/runs/{ref}/stream")
    async def stream(websocket: WebSocket, ref: str) -> None:
        await websocket.accept()

        run = await db.get_run_by_ref(ref)
        if not run:
            await websocket.send_json({"event_type": "error", "payload": {"detail": "Run not found"}})
            await websocket.close()
            return

        run_id = run["id"]
        broker = manager.get_broker(run_id)

        # Subscribe FIRST (start buffering live events) to avoid a gap between
        # the replay read and going live. We then replay persisted events and
        # dedupe the live queue by seq. If the run already finished, the broker's
        # end-sentinel was already dispatched to prior subscribers, so a fresh
        # subscribe would block forever — fall back to the replay-only path.
        queue = broker.subscribe() if (broker and not broker.finished) else None
        max_sent_seq = -1

        try:
            # 1. Replay everything already persisted (late-join catch-up).
            rows = await db.get_events_after(run_id, after_seq=-1)
            for row in rows:
                event = event_row_to_wire(row)
                await websocket.send_json(event)
                max_sent_seq = max(max_sent_seq, event["seq"])

            # If there is no live broker (run already finished), we are done
            # once the persisted stream contains a terminal event.
            if queue is None:
                terminal = any(
                    r["event_type"] in TERMINAL_EVENTS for r in rows
                )
                if not terminal:
                    # Run finished without a broker and no terminal persisted
                    # (edge case) — synthesize a completed marker from status.
                    await websocket.send_json({
                        "run_id": run_id,
                        "turn": run.get("branch_turn") or 0,
                        "seq": max_sent_seq + 1,
                        "event_type": "sim.completed"
                        if run.get("status") == "complete"
                        else "sim.failed",
                        "agent_name": None,
                        "payload": {"status": run.get("status")},
                    })
                await websocket.close()
                return

            # 2. Tail the live stream, skipping anything already replayed.
            while True:
                event = await queue.get()
                if event is None:  # end-of-stream sentinel
                    break
                if event["seq"] <= max_sent_seq:
                    continue
                await websocket.send_json(event)
                max_sent_seq = max(max_sent_seq, event["seq"])
                if event["event_type"] in TERMINAL_EVENTS:
                    break

            await websocket.close()

        except WebSocketDisconnect:
            logger.debug("WebSocket client disconnected from run %s", run_id)
        except Exception:  # noqa: BLE001
            logger.exception("WebSocket stream error for run %s", run_id)
        finally:
            if broker is not None and queue is not None:
                broker.unsubscribe(queue)

    # --------------------------- Static frontend --------------------------- #
    _mount_static(app)

    return app


def _mount_static(app: FastAPI) -> None:
    """
    Serve the built frontend from ``matrix_studio/static`` if present, with SPA
    fallback so client-side routes resolve to index.html. If the build is
    absent (dev backend without a frontend build), serve a small placeholder so
    the API still runs.
    """
    index_file = STATIC_DIR / "index.html"

    if index_file.exists():
        assets_dir = STATIC_DIR / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/")
        async def _index() -> FileResponse:
            return FileResponse(str(index_file))

        @app.get("/{full_path:path}")
        async def _spa(full_path: str) -> FileResponse:
            # Serve real static files when they exist, else SPA-fallback.
            candidate = STATIC_DIR / full_path
            if candidate.is_file():
                return FileResponse(str(candidate))
            return FileResponse(str(index_file))
    else:
        @app.get("/")
        async def _placeholder() -> JSONResponse:
            return JSONResponse(
                {
                    "message": "TheMatrix Simulation Studio API is running. "
                    "Frontend build not found — run the Vite build or use the "
                    "Docker image. API is under /api.",
                    "docs": "/docs",
                }
            )
