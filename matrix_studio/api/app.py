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
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from matrix_studio import analysis, service
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
    # Optional custom analyst-role framing; REPLACES the default role text while
    # the non-negotiable guardrails always remain. None → default framing.
    instructions: Optional[str] = None


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
    # Optional custom analyst-role framing that REPLACES the default role text.
    # The guardrails (JSON schema, JSON-only, no-fabrication) always remain and
    # cannot be dropped by the user. None → default framing.
    instructions: Optional[str] = None


class CreateThreadModel(BaseModel):
    """Open an aside thread. persona_name is required only for target='persona'."""

    target: str  # 'analyst' | 'persona' | 'room'
    persona_name: Optional[str] = None


class ThreadMessageModel(BaseModel):
    """Post a user message into an aside thread."""

    content: str
    # Optional per-message model override for the analysis reply; None -> the
    # run's resolved analysis model (settings default for imported/branch runs).
    model: Optional[str] = None


class BranchMutationModel(BaseModel):
    """Phase 2b: a single mutation applied at the fork before the branch
    generates forward. ``kind`` selects the operation; the other fields are
    per-kind.

    Step 1: inject_message, continue
    Step 2: edit_goal, add_persona, remove_persona
    Step 3: promote_aside
    """

    kind: str
    # inject_message / promote_aside
    speaker: Optional[str] = None
    content: Optional[str] = None
    source: Optional[str] = None
    # continue
    add_budget: Optional[int] = Field(default=None, ge=1)
    # edit_goal / remove_persona — the persona's name in the cast
    persona_name: Optional[str] = None
    goals: Optional[List[str]] = None
    # add_persona — name is the cast entry name; persona is the description text
    name: Optional[str] = None
    persona: Optional[str] = None
    # promote_aside
    thread_id: Optional[str] = None
    message_id: Optional[int] = None


class BranchModel(BaseModel):
    """Body for POST /api/runs/{ref}/branch (Phase 2a fork; Phase 2b mutation)."""

    from_turn: int = Field(ge=0)
    name: Optional[str] = None
    description: Optional[str] = None
    # Optional generation-model override for the branch's forward turns; None ->
    # inherit the parent's model (or the settings default for imported parents).
    model: Optional[str] = None
    # Phase 2b: optional mutation applied at the fork. None -> a plain 2a fork.
    mutation: Optional[BranchMutationModel] = None


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
        # Wall-clock of the most recent event; lets the UI flag a run still
        # marked "running" that has gone quiet (stalled/orphaned).
        "last_event_at": run.get("last_event_at"),
        # Phase 2a lineage: set on branch runs so history/run views can show
        # "branched from <parent> @ turn N". Both null for a fresh (root) run.
        "parent_run_id": run.get("parent_run_id"),
        "branch_turn": run.get("branch_turn"),
    }


_SUPPORTED_MUTATION_KINDS = {"inject_message", "continue", "edit_goal", "add_persona", "remove_persona", "promote_aside"}


def _validate_branch_mutation(
    mutation: Optional["BranchMutationModel"],
) -> Optional[Dict[str, Any]]:
    """
    Validate a Phase 2b branch mutation and return it as a plain dict (or None).
    Raises HTTP 422 on any malformed/unsupported mutation so the caller gets a
    clean error before a branch run is created.
    """
    if mutation is None:
        return None
    kind = (mutation.kind or "").strip()
    if kind not in _SUPPORTED_MUTATION_KINDS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unsupported mutation kind {kind!r}; "
                f"supported: {sorted(_SUPPORTED_MUTATION_KINDS)}"
            ),
        )
    if kind == "continue":
        if not mutation.add_budget or mutation.add_budget < 1:
            raise HTTPException(
                status_code=422, detail="continue.add_budget must be >= 1"
            )
        return {"kind": "continue", "add_budget": int(mutation.add_budget)}
    if kind == "inject_message":
        speaker = (mutation.speaker or "").strip()
        content = (mutation.content or "").strip()
        if not speaker:
            raise HTTPException(status_code=422, detail="inject_message.speaker is required")
        if not content:
            raise HTTPException(status_code=422, detail="inject_message.content is required")
        out: Dict[str, Any] = {"kind": "inject_message", "speaker": speaker, "content": content}
        if mutation.source:
            out["source"] = str(mutation.source)
        # Optional: configurable length of the new discussion round (number of
        # generated turns after the injection). Omitted -> the original budget.
        if mutation.add_budget is not None:
            out["add_budget"] = int(mutation.add_budget)
        return out
    if kind == "edit_goal":
        persona_name = (mutation.persona_name or "").strip()
        goals = mutation.goals
        if not persona_name:
            raise HTTPException(status_code=422, detail="edit_goal.persona_name is required")
        if goals is None:
            raise HTTPException(status_code=422, detail="edit_goal.goals is required")
        return {"kind": "edit_goal", "persona_name": persona_name, "goals": [str(g) for g in goals]}
    if kind == "add_persona":
        name = (mutation.name or "").strip()
        persona_text = (mutation.persona or "").strip()
        goals = mutation.goals or []
        if not name:
            raise HTTPException(status_code=422, detail="add_persona.name is required")
        if not persona_text:
            raise HTTPException(status_code=422, detail="add_persona.persona is required")
        return {"kind": "add_persona", "name": name, "persona": persona_text,
                "goals": [str(g) for g in goals]}
    # promote_aside — resolved in execute_branch (needs DB); validate fields only
    if kind == "promote_aside":
        thread_id = (mutation.thread_id or "").strip()
        message_id = mutation.message_id
        if not thread_id:
            raise HTTPException(status_code=422, detail="promote_aside.thread_id is required")
        if message_id is None:
            raise HTTPException(status_code=422, detail="promote_aside.message_id is required")
        return {"kind": "promote_aside", "thread_id": thread_id, "message_id": int(message_id)}
    # remove_persona
    name = (mutation.persona_name or mutation.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="remove_persona.name is required")
    return {"kind": "remove_persona", "name": name}


async def sweep_stale_running_runs(db: Database) -> List[str]:
    """
    Mark orphaned "running" runs as "interrupted".

    Called once at server startup. Because a fresh process holds no live
    background tasks, any run still in ``running`` status was cut off by a
    crash/restart mid-generation (the runner never reached its terminal
    status update). Each such run is transitioned to the terminal
    ``interrupted`` status and gets an appended ``sim.interrupted`` event so
    its log records why it stopped and the UI no longer shows it as live.

    This never re-runs or mutates simulation content; it only closes out a
    dangling lifecycle status. Returns the list of affected run ids.
    """
    stale = await db.list_runs_by_status("running")
    swept: List[str] = []
    for run in stale:
        run_id = run["id"]
        last_turn = await db.last_event_turn(run_id)
        next_seq = await db.max_seq(run_id) + 1
        await db.append_event(
            run_id=run_id,
            turn=last_turn,
            seq=next_seq,
            event_type="sim.interrupted",
            payload={
                "reason": "server restarted while the run was still generating",
                "at_turn": last_turn,
            },
        )
        await db.update_run_status(
            run_id, "interrupted", completed_at=int(time.time())
        )
        swept.append(run_id)
    return swept


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
        # Startup stale-run sweep: on a fresh process, no run can have a live
        # background task, so any row still marked "running" was orphaned by a
        # crash/restart mid-generation. Mark them "interrupted" (a terminal
        # state) and record a sim.interrupted event so the UI stops showing them
        # as live forever. Read-only w.r.t. simulation content; never re-runs.
        swept = await sweep_stale_running_runs(db)
        if swept:
            logger.warning(
                "Startup sweep: marked %d orphaned running run(s) as interrupted: %s",
                len(swept),
                ", ".join(swept),
            )
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
        """Model string(s) selectable in the new-run form + in-thread pickers.
        Keys stay server-side; this only exposes the allowlist of model strings."""
        return {
            "default": settings.litellm_model,
            "models": settings.available_model_list,
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

        # Phase 2a lineage: the parent (if this is a branch) and any child
        # branches forked from this run, so the run view can thread relationships.
        parent = None
        if run.get("parent_run_id"):
            parent_row = await db.get_run(run["parent_run_id"])
            if parent_row:
                parent = {
                    "run_id": parent_row["id"],
                    "name": parent_row.get("name"),
                    "branch_turn": run.get("branch_turn"),
                }
        branches = await db.list_branches(run["id"])

        return {
            **summary,
            "cast": cast,
            "config": config,
            "result": result,
            "summary": {"generated": generated, "imported": imported},
            "lineage": {"parent": parent, "branches": branches},
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

    # ------------------- Phase 2a: checkpoints + branching ----------------- #
    # State reconstruction/replay: list the per-turn checkpoints and fetch the
    # full SimSnapshot at a given turn (read-only). The branch route forks a new
    # run that resumes forward — the parent is never modified.

    @app.get("/api/runs/{ref}/snapshots")
    async def list_snapshots(ref: str) -> Dict[str, Any]:
        run = await db.get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        snapshots = await db.list_snapshots(run["id"])
        return {"run_id": run["id"], "snapshots": snapshots}

    @app.get("/api/runs/{ref}/snapshots/{turn}")
    async def get_snapshot(ref: str, turn: int) -> Dict[str, Any]:
        run = await db.get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        snapshot = await db.get_snapshot(run["id"], turn=turn)
        if snapshot is None:
            raise HTTPException(
                status_code=404, detail=f"No checkpoint at turn {turn}"
            )
        return {
            "run_id": run["id"],
            "turn": snapshot.turn,
            "status": snapshot.status,
            "topic": snapshot.topic,
            "total_turns": snapshot.total_turns,
            "conversation": snapshot.conversation,
            "agents": {
                name: agent.model_dump()
                for name, agent in snapshot.agents.items()
            },
        }

    @app.post("/api/runs/{ref}/branch", status_code=201)
    async def branch_run(ref: str, body: BranchModel) -> Dict[str, Any]:
        parent = await db.get_run_by_ref(ref)
        if not parent:
            raise HTTPException(status_code=404, detail="Run not found")
        # The fork turn must exist in the parent's history. We validate against
        # the parent's turn count (agent.response events) so a caller can only
        # branch from a turn that actually happened.
        stats = await db.get_run_stats(parent["id"])
        max_turn = stats["turn_count"]
        if body.from_turn < 0 or body.from_turn > max_turn:
            raise HTTPException(
                status_code=422,
                detail=f"from_turn must be between 0 and {max_turn} for this run",
            )
        # Phase 2b: validate the mutation (if any) up front for a clean 422,
        # then pass it through as a plain dict.
        mutation = _validate_branch_mutation(body.mutation)
        meta = await manager.create_branch(
            parent,
            from_turn=body.from_turn,
            name=body.name,
            description=body.description,
            model=body.model,
            mutation=mutation,
        )
        return meta

    @app.get("/api/runs/{ref}/tree")
    async def run_tree(ref: str) -> Dict[str, Any]:
        """Phase 2b branch-tree. Returns the full lineage rooted at the ancestor
        of ``ref``, with each node's mutation kind (for edge labels) and status.
        Nodes are ordered oldest-first; edges are inferred from parent_run_id.
        """
        run = await db.get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        tree = await db.get_run_tree(run["id"])
        # Enrich nodes with the mutation kind from config_json for UI edge labels.
        import json as _json
        enriched: Dict[str, Any] = {}
        for nid, node in tree["nodes"].items():
            cfg = {}
            try:
                cfg = _json.loads(node["config_json"] or "{}")
            except Exception:  # noqa: BLE001
                pass
            mutation_kind = cfg.get("branch_mutation", {}).get("kind") if cfg.get("branch_mutation") else None
            enriched[nid] = {
                "id": node["id"],
                "name": node["name"],
                "slug": node["slug"],
                "status": node["status"],
                "branch_turn": node["branch_turn"],
                "parent_run_id": node["parent_run_id"],
                "created_at": node["created_at"],
                "turn_count": node["turn_count"],
                "total_cost_usd": node["total_cost_usd"],
                "mutation_kind": mutation_kind,
            }
        return {"root_id": tree["root_id"], "nodes": enriched}

    @app.post("/api/runs/{ref}/resume")
    async def resume_run(ref: str) -> Dict[str, Any]:
        """
        Error-recovery: resume an interrupted/failed run forward IN PLACE (same
        run id/codename), continuing from its last checkpoint. Non-blocking —
        returns immediately; generation runs in the background and streams over
        the existing WS. A completed run cannot be resumed (branch it instead).
        """
        run = await db.get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        try:
            return await manager.resume_run(run)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

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
        # `default_instructions` is the default analyst-role framing so the client
        # can prefill the regenerate editor / offer "reset to default" even before
        # any generation. The guardrails are enforced separately and not editable.
        return {
            "run_id": run["id"],
            "default_instructions": analysis.DEFAULT_SUMMARY_INSTRUCTIONS,
            **_shape_summaries(rows),
        }

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
        # `instructions` REPLACES the default analyst-role framing (guardrails
        # always remain). Fall back to the run's configured instructions when the
        # request omits it entirely.
        instructions = (
            body.instructions
            if body.instructions is not None
            else cfg.get("instructions")
        )
        saved = await service.generate_and_store_summary(
            db,
            run,
            fields=fields,
            focus=focus,
            model=body.model,
            instructions=instructions,
        )
        rows = await db.get_summaries(run["id"])
        return {
            "run_id": run["id"],
            "generated": saved,
            "default_instructions": analysis.DEFAULT_SUMMARY_INSTRUCTIONS,
            **_shape_summaries(rows),
        }

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
            db, run, thread, user_message=body.content.strip(), model=body.model
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
