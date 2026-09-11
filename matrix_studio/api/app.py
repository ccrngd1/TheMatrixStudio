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
    POST   /api/runs/{ref}/stop           stop a live run after the current turn
    GET    /api/health                   liveness probe
    GET    /api/documents/formats        which file types this install can read
    POST   /api/documents/extract        extract text from an uploaded file

Keys never touch the browser — the model list and all provider credentials come
from server-side settings/env (Phase 0 .env). Full BYO-key browser UX is Phase 3.
"""

import json
import logging
import re
import tempfile
import time
from functools import partial
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from matrix_studio import analysis, blobs, orchestration, service
from matrix_studio.api.identity import current_user, current_user_ws
from matrix_studio.api.manager import RunManager, TERMINAL_EVENTS, event_row_to_wire
from matrix_studio.documents import (
    ExtractionError,
    format_support,
    ingest_file,
    ingest_text,
)
from matrix_studio.naming import generate_run_name
from matrix_studio.persona_wizard import (
    DEFAULT_PERSONAS,
    MAX_PERSONAS,
    MIN_PERSONAS,
    WizardError,
    suggest_cast,
)
from matrix_studio.personas import StructuredPersona, structured_payload
from matrix_studio.retrieval import (
    apply_budget,
    build_fts_query,
    embed_pending_chunks,
    extract_terms,
)
from matrix_studio.settings import get_settings
from matrix_studio.storage import Database

logger = logging.getLogger(__name__)

# Location of the built frontend (populated by the Vite build / Docker stage).
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


# --------------------------------------------------------------------------- #
# Request/response models (documentation + light validation only)
# --------------------------------------------------------------------------- #
class InlineDocumentModel(BaseModel):
    """A background document supplied as text in the create-run request."""

    title: Optional[str] = None
    text: str


class PersonaModel(BaseModel):
    name: str
    persona: str
    goals: List[str] = Field(default_factory=list)
    # Phase 5: server-readable paths to background documents scoped to this
    # persona, ingested at run start. Declared here because the model is the
    # request contract — an undeclared field is silently dropped, which would
    # make cast-level attachment work from the CLI but not through the API.
    documents: List[str] = Field(default_factory=list)
    # Phase 6: structured identity — background, preferences (incl. `dismisses`),
    # viewpoints with firmness. Typed as the real model rather than a loose dict so
    # a bad `firmness` is a 422 at the API boundary instead of a run-start crash.
    # Same contract lesson as `documents` above: an undeclared field is dropped.
    structured: Optional[StructuredPersona] = None
    # Inline background documents, ingested at run start exactly like `documents`
    # paths are. Declared because a BROWSER cannot supply server-readable paths, and
    # a document attached after the run exists is already too late — the engine
    # ingests cast documents before turn 1. Uploaded files land here too: they are
    # turned into text by POST /api/documents/extract first, so a knowledge-base
    # file and pasted text share one ingest path.
    document_texts: List["InlineDocumentModel"] = Field(default_factory=list)


class CognitionConfigModel(BaseModel):
    """Phase 2c per-run cognition flags. All default to pre-2c behavior; when
    ``enabled`` is False the engine path is byte-for-byte identical to Phase 2b.
    ``reflection_every`` is ON by default (4) but only takes effect when enabled."""
    enabled: bool = False
    memory: bool = True
    reflection_every: int = Field(default=4, ge=0)
    goals_dynamic: bool = False
    relationships: bool = False
    retrieval_k: int = Field(default=5, ge=0)
    # Phase 4b: pending-thread ledger (opt-in; needs enabled=True too).
    threads: bool = False
    thread_stale_after: int = Field(default=5, ge=1)


class RetrievalConfigModel(BaseModel):
    """Phase 5 per-run document retrieval. Omitted -> disabled (pre-Phase-5
    behavior). ``max_chars`` is a hard ceiling on retrieved document text per
    turn, which is what keeps a large attachment out of the per-call context."""

    enabled: bool = False
    k: int = Field(default=3, ge=0)
    max_chars: int = Field(default=1200, ge=0)
    recent_turns: int = Field(default=3, ge=1)
    # fts (in-process BM25) | vector (default) | hybrid. Kept in step with
    # `RetrievalConfig.mode`, whose docstring carries the measurement that chose
    # "vector" — three defaults that disagree is how a setting looks ignored.
    mode: str = Field(default="vector")
    embedding_model: str = ""
    rrf_k: int = Field(default=60, ge=1)
    # Phase 5h: absolute cosine floor for vector/hybrid (off-topic guard, 0 = off).
    min_similarity: float = Field(default=0.15, ge=0.0, le=1.0)
    # Phase 5g: ask the persona to flag in-voice when retrieval found nothing.
    disclose_unsupported: bool = False
    # Experimental, DEFAULT OFF: measured harmful in
    # docs/PHASE5-RETRIEVAL-MEASUREMENT.md (recall fell on all three arms).
    term_limit: int = Field(default=0, ge=0)
    max_df_ratio: float = Field(default=0.5, gt=0.0, le=1.0)
    score_ratio: float = Field(default=0.0, ge=0.0, le=1.0)


class PersonaConfigModel(BaseModel):
    """Phase 6 per-run structured personas. Omitted -> disabled, in which case a
    cast member's ``structured`` block never reaches a prompt."""

    enabled: bool = False
    withhold_concerns: bool = True
    # Named variant: mandatory | retuned | blunt | off. Booleans still accepted
    # (True -> "mandatory", False -> "off") so existing API clients keep working.
    # Typed loosely here and validated by PersonaConfig, so an unknown name is a
    # 422 rather than a silent fallback to the default wording.
    dismissal_rule: Any = "mandatory"


class RunConfigModel(BaseModel):
    max_messages: Optional[int] = None
    generate_avatars: Optional[bool] = None
    # Phase 2c: optional cognition config. Omitted -> cognition disabled
    # (engine behaves exactly as Phase 2b).
    cognition: Optional[CognitionConfigModel] = None
    # Phase 5: optional document retrieval. Independent of cognition.
    retrieval: Optional[RetrievalConfigModel] = None
    # Phase 6: optional structured personas. Omitted -> disabled, and any
    # `structured` block on a cast member is ignored (pre-Phase-6 prompts).
    personas: Optional[PersonaConfigModel] = None


class SummaryConfigModel(BaseModel):
    """Optional summary generation config (Phase 1.5). Omitted → default
    (enabled with the full field set, no focus)."""

    enabled: bool = True
    fields: Optional[List[str]] = None
    focus: Optional[str] = None
    # Optional custom analyst-role framing; REPLACES the default role text while
    # the non-negotiable guardrails always remain. None → default framing.
    instructions: Optional[str] = None


class SuggestPersonasModel(BaseModel):
    """Body for POST /api/personas/suggest — the new-run form's persona wizard."""

    brief: str
    count: int = Field(default=DEFAULT_PERSONAS, ge=MIN_PERSONAS, le=MAX_PERSONAS)
    model: Optional[str] = None


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
    Phase 4c: adaptive_pressure (EXPERIMENTAL, opt-in via
    ADAPTIVE_PRESSURE_ENABLED)
    """

    kind: str
    # inject_message / promote_aside
    speaker: Optional[str] = None
    content: Optional[str] = None
    source: Optional[str] = None
    # adaptive_pressure (Phase 4c, experimental): optional operator direction
    # for the pressure event (e.g. "escalate the audit dilemma").
    focus: Optional[str] = None
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


class AttachDocumentModel(BaseModel):
    """Body for POST /api/runs/{ref}/documents (Phase 5).

    Exactly one of ``text`` or ``path`` must be supplied. ``persona_name`` scopes
    the document to a single persona; omitted/null makes it cast-wide.
    """

    persona_name: Optional[str] = None
    title: Optional[str] = None
    # Inline content (paste / upload body).
    text: Optional[str] = None
    # Server-readable path (CLI-adjacent workflows and local files).
    path: Optional[str] = None


def _parse_cast(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Best-effort parse of a run row's cast_json (mirrors _parse_config)."""
    raw = run.get("cast_json")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _parse_config(run: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort parse of a run row's config_json.

    The counterpart _parse_cast's docstring already claimed existed. A malformed or
    absent config yields ``{}`` rather than raising: config is descriptive metadata
    about a run that has already happened, so a read of it should never be the thing
    that fails a request.
    """
    raw = run.get("config_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


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


_SUPPORTED_MUTATION_KINDS = {"inject_message", "continue", "edit_goal", "add_persona", "remove_persona", "promote_aside", "adaptive_pressure"}


# Friendly labels for the model dropdown. Exact known ids map to clean names;
# anything else falls back to a best-effort cleanup of the raw id tail.
_MODEL_LABELS = {
    "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0": "Haiku 4.5",
    "bedrock/global.anthropic.claude-sonnet-4-6": "Sonnet 4.6",
    "bedrock/global.anthropic.claude-opus-4-8": "Opus 4.8",
    "bedrock/amazon.nova-pro-v1:0": "Nova Pro",
    "bedrock/us.amazon.nova-pro-v1:0": "Nova Pro",
    "bedrock/amazon.nova-premier-v1:0": "Nova Premier",
    "bedrock/us.amazon.nova-premier-v1:0": "Nova Premier",
    "bedrock/us.meta.llama4-maverick-17b-instruct-v1:0": "Llama 4 Maverick",
    "bedrock/us.meta.llama4-scout-17b-instruct-v1:0": "Llama 4 Scout",
}


def _model_label(model_id: str) -> str:
    """Human-friendly label for a model id used in the UI dropdown."""
    if model_id in _MODEL_LABELS:
        return _MODEL_LABELS[model_id]
    # Fallback: last path segment, provider/version noise trimmed.
    tail = model_id.split("/")[-1]
    return tail


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
    # Phase 4c (EXPERIMENTAL): refuse up front while the feature is disabled so
    # no branch run row is ever created for it.
    if kind == "adaptive_pressure":
        if not get_settings().adaptive_pressure_enabled:
            raise HTTPException(
                status_code=422,
                detail="adaptive_pressure is experimental and disabled "
                "(set ADAPTIVE_PRESSURE_ENABLED=true to opt in)",
            )
        out = {"kind": "adaptive_pressure"}
        if mutation.focus:
            out["focus"] = str(mutation.focus)
        if mutation.add_budget is not None:
            out["add_budget"] = int(mutation.add_budget)
        return out
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
        # The sweep is the one cross-tenant read in the system, so it cannot be bound
        # to a caller — it binds to each ROW's owner instead, which keeps every write
        # inside the partition it belongs to. A single binding here would put one
        # user's interruption marker into another user's partition.
        owned = db.for_owner(run["owner_sub"])
        last_turn = await owned.last_event_turn(run_id)
        next_seq = await owned.max_seq(run_id) + 1
        await owned.append_event(
            run_id=run_id,
            turn=last_turn,
            seq=next_seq,
            event_type="sim.interrupted",
            payload={
                "reason": "server restarted while the run was still generating",
                "at_turn": last_turn,
            },
        )
        await owned.update_run_status(
            run_id, "interrupted", completed_at=int(time.time())
        )
        swept.append(run_id)
    return swept


def create_app(db_path: Optional[str] = None) -> FastAPI:
    """
    Build the FastAPI app.

    ``db_path`` is accepted and **ignored**, and the parameter is kept rather than
    removed on purpose: it is passed by 23 test call sites, and a keyword that raises
    would turn a storage migration into a mass test edit for no behavioural gain. What
    it used to select — a SQLite file — no longer exists. The backend is DynamoDB, S3
    and S3 Vectors, addressed by `TABLE_PREFIX`, `DATA_BUCKET`, `VECTOR_BUCKET` and
    `VECTOR_INDEX`; tests point those at a `moto` account (see `tests/conftest.py`).

    Deprecated, and logged when used, so it does not quietly become "the way tests
    configure storage" — which would be a lie with no error attached.
    """
    settings = get_settings()
    if db_path is not None:
        logger.debug(
            "create_app(db_path=%r) is ignored: storage is DynamoDB + S3 now, "
            "configured by TABLE_PREFIX/DATA_BUCKET/VECTOR_BUCKET.", db_path,
        )

    db = Database()
    manager = RunManager(db)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await db.connect()
        # Startup stale-run sweep: on a fresh process, no run can have a live
        # background task, so any row still marked "running" was orphaned by a
        # crash/restart mid-generation. Mark them "interrupted" (a terminal
        # state) and record a sim.interrupted event so the UI stops showing them
        # as live forever. Read-only w.r.t. simulation content; never re-runs.
        #
        # Gated because that premise — "this is the only process" — is false under
        # Lambda or any horizontally-scaled deployment, where two concurrent cold
        # starts would each mark the other's in-flight run as interrupted. One
        # request killing another user's live run is not a degradation, so this is
        # off there and the cleanup becomes a scheduled job (Phase 7).
        if settings.startup_sweep:
            swept = await sweep_stale_running_runs(db)
            if swept:
                logger.warning(
                    "Startup sweep: marked %d orphaned running run(s) as "
                    "interrupted: %s",
                    len(swept),
                    ", ".join(swept),
                )
        else:
            logger.info(
                "Startup sweep disabled (STARTUP_SWEEP=false). Runs orphaned by a "
                "crash will keep reporting as running until something else cleans "
                "them up."
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
    async def health() -> Dict[str, Any]:
        """
        Health + readiness check (Phase 3). Returns ``status: ok`` plus per-provider
        has-key booleans (NEVER the key values). A fresh user gets a "no model key
        detected" hint in the UI.
        """
        import os

        # Check which providers have keys configured (booleans only, never values)
        readiness = {
            "openai": bool(settings.openai_api_key),
            "anthropic": bool(settings.anthropic_api_key),
            "bedrock": bool(
                settings.aws_access_key_id
                or settings.aws_secret_access_key
                or os.getenv("AWS_ACCESS_KEY_ID")
                or os.getenv("AWS_SECRET_ACCESS_KEY")
                or os.getenv("AWS_BEARER_TOKEN_BEDROCK")
                # boto3 also checks ~/.aws/credentials, EC2 instance profile, etc.
                # We report True if env vars are set OR if the user likely has boto3
                # configured (we can't easily check that without importing boto3)
            ),
        }

        return {
            "status": "ok",
            "readiness": readiness,
        }

    @app.get("/api/models")
    async def models() -> Dict[str, Any]:
        """Models selectable in the new-run form + in-thread pickers. Returns each
        model as ``{id, label}`` (friendly label for the UI dropdown). Keys stay
        server-side; this only exposes the allowlist of model strings."""
        return {
            "default": settings.litellm_model,
            "models": [
                {"id": m, "label": _model_label(m)}
                for m in settings.available_model_list
            ],
        }

    @app.get("/api/name/suggest")
    async def suggest_name(
        topic: str = Query(...), user: str = Depends(current_user)
    ) -> Dict[str, str]:
        """Suggest a memorable codename + description for the new-run form.

        The uniqueness check is scoped to the caller, so the suggestion cannot be
        rejected by — or reveal the existence of — a run under another account.
        """
        result = await generate_run_name(
            # Bound, not `owner_sub=`: the two are not equivalent. An explicit owner
            # scopes the QUERY; binding also attaches the tenant-scoped credentials
            # (§3). Mixing the idioms is what left half the routes calling DynamoDB
            # with the Lambda's own role, which held no storage rights at all.
            topic=topic, name_exists=db.for_owner(user).name_exists
        )
        return {
            "name": result["name"],
            "description": result["description"],
            "slug": result["slug"],
            "source": result["source"],
        }

    @app.post("/api/personas/suggest")
    async def suggest_personas(body: SuggestPersonasModel) -> Dict[str, Any]:
        """Draft a cast of structured personas from a short brief.

        AUTHORING ASSISTANCE, not simulation: the result is a draft returned to the
        form for the operator to edit, and it never starts a run by itself. Nothing
        here is evidence about anything — it is a template generator, which is why it
        sits outside the engine's in-loop honesty invariants entirely.

        A failure is a 502, not a 500: the model is upstream, and the operator's
        fallback is to rephrase or write the cast by hand. They need the reason.
        """
        try:
            cast = await suggest_cast(
                brief=body.brief, count=body.count or DEFAULT_PERSONAS, model=body.model
            )
        except WizardError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"cast": cast, "count": len(cast)}

    @app.post("/api/runs", status_code=201)
    async def create_run(
        body: CreateRunModel, user: str = Depends(current_user)
    ) -> Dict[str, Any]:
        request = body.model_dump(exclude_none=True)
        if not request.get("cast"):
            raise HTTPException(status_code=422, detail="At least one persona is required")
        result = await manager.create_run(request, owner_sub=user)
        return result

    @app.get("/api/runs")
    async def list_runs(
        q: Optional[str] = Query(default=None), user: str = Depends(current_user)
    ) -> Dict[str, Any]:
        runs = await db.for_owner(user).list_runs(q=q)
        return {"runs": [_run_summary(r) for r in runs]}

    @app.get("/api/runs/{ref}")
    async def get_run(ref: str, user: str = Depends(current_user)) -> Dict[str, Any]:
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        stats = await db.for_owner(user).get_run_stats(run["id"])
        run.update(stats)
        summary = _run_summary(run)

        # Attach the final result (conversation + agents) from the snapshot when
        # the run is complete, so a reloaded run shows full dossier data.
        result: Optional[Dict[str, Any]] = None
        snapshot = await db.for_owner(user).get_snapshot(run["id"])
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
        summary_rows = await db.for_owner(user).get_summaries(run["id"])
        generated = next((r for r in summary_rows if r["kind"] == "generated"), None)
        imported = next((r for r in summary_rows if r["kind"] == "imported"), None)

        # Phase 2a lineage: the parent (if this is a branch) and any child
        # branches forked from this run, so the run view can thread relationships.
        parent = None
        if run.get("parent_run_id"):
            parent_row = await db.for_owner(user).get_run(run["parent_run_id"])
            if parent_row:
                parent = {
                    "run_id": parent_row["id"],
                    "name": parent_row.get("name"),
                    "branch_turn": run.get("branch_turn"),
                }
        branches = await db.for_owner(user).list_branches(run["id"])

        return {
            **summary,
            "cast": cast,
            "config": config,
            "result": result,
            "summary": {"generated": generated, "imported": imported},
            "lineage": {"parent": parent, "branches": branches},
        }

    # ---------------- Knowledge-base file upload (run-agnostic) ---------------- #
    # Extraction is separated from attachment on purpose. A persona's knowledge base
    # is authored BEFORE the run exists — the engine ingests cast documents ahead of
    # turn 1, so "upload once the run is created" is already too late — and the
    # create-run request is JSON with a nested cast, which a multipart body cannot
    # express. So a file is turned into text here, the form holds it as ordinary
    # `document_texts`, and it flows through run creation, retrieval and the setup
    # export with no new path. Same reason /documents/extract takes no run id: it
    # also serves adding a file to a run that already exists.

    @app.get("/api/documents/formats")
    async def document_formats() -> Dict[str, Any]:
        """Which file types can be read here, and the size limits that apply.

        Served so the picker can offer only what will work: PDF and Word extraction
        are optional extras, and an install without them should say so in the form
        rather than failing after the operator has chosen a file.
        """
        return {
            "formats": [
                {"suffix": f.suffix, "media_type": f.media_type,
                 "available": f.available, "needs": f.needs}
                for f in format_support()
            ],
            "max_upload_bytes": settings.max_upload_bytes,
            "max_document_chars": settings.max_document_chars,
        }

    @app.post("/api/documents/extract")
    async def extract_document(
        file: UploadFile = File(..., description="A .txt/.md/.pdf/.docx file"),
        title: Optional[str] = Form(default=None),
    ) -> Dict[str, Any]:
        """Extract text from an uploaded file. Stores nothing.

        The response is text the caller then submits as a persona's
        ``document_texts`` entry, which keeps this endpoint free of any run or
        persona coupling and leaves the operator able to read and edit what was
        extracted before it becomes a persona's knowledge base. That review step
        matters for PDFs, where extraction quality varies and a scanned page yields
        nothing at all.
        """
        # Only the base name: a client-supplied filename may contain path separators,
        # and this value is used to pick an extractor and as the default title.
        raw_name = Path(file.filename or "").name
        suffix = Path(raw_name).suffix.lower()
        supported = {f.suffix: f for f in format_support()}
        if suffix not in supported:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Unsupported file type {suffix or '(none)'}. "
                    f"Supported: {', '.join(sorted(supported))}"
                ),
            )
        fmt = supported[suffix]
        if not fmt.available:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Reading {suffix} files needs the '{fmt.needs}' package on the "
                    f"server. Install it with: pip install 'matrix-sim-studio[documents]'"
                ),
            )

        # Streamed with a running total rather than `await file.read()`: the point of a
        # size cap is not to buffer the oversized upload first.
        limit = settings.max_upload_bytes
        tmp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = Path(tmp.name)
                total = 0
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"{raw_name} is larger than the {limit // (1024 * 1024)} MB "
                                "limit for a single document."
                            ),
                        )
                    tmp.write(chunk)
            if total == 0:
                raise HTTPException(status_code=422, detail=f"{raw_name} is empty.")

            try:
                doc = ingest_file(
                    tmp_path,
                    title=(title or raw_name).strip() or raw_name,
                    # Errors must name the file the operator chose, not the temp file
                    # it was streamed into — otherwise a failure in a multi-file upload
                    # identifies nothing.
                    display_name=raw_name,
                )
            except ExtractionError as exc:
                # Unreadable input is the client's problem, and the message names the
                # cause (including a scanned PDF with no text layer).
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            if len(doc.text) > settings.max_document_chars:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"{raw_name} extracted to {len(doc.text):,} characters, over the "
                        f"{settings.max_document_chars:,} limit for one document. Split it "
                        "into sections and upload those separately."
                    ),
                )

            return {
                "title": doc.title,
                "media_type": doc.media_type,
                "text": doc.text,
                "char_count": doc.char_count,
                "chunk_count": len(doc.chunks),
                # The uploaded file is NOT retained; the text is the whole artefact.
                "stored": False,
            }
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    @app.get("/api/runs/{ref}/setup")
    async def get_run_setup(ref: str, user: str = Depends(current_user)) -> Dict[str, Any]:
        """This run's setup, shaped as a create-run request body.

        For "start a fresh conversation from this one": the operator gets the whole
        definition back in the new-run form, edits the topic, cast, convictions or
        documents, and runs it as a brand-new root run. That is a different
        operation from branching, which replays this run's events to a turn and
        continues them — here nothing is replayed and nothing is inherited at
        runtime.

        Returning the create-run body rather than a bespoke shape is what makes it
        editable and re-runnable with no translation layer: the same schema the
        setup importer already reads from a file, so a setup can round-trip through
        either path and the two cannot drift.

        Document text comes from the documents table, not from the cast's original
        ``document_texts``. The table is what the run actually had — it includes
        documents uploaded after the run started and text extracted from
        server-side paths, both of which the original request never contained.
        """
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        cast = _parse_cast(run)
        config = _parse_config(run)
        warnings: List[str] = []

        # Text is rebuilt per document, so a persona's background survives even
        # though the uploaded file itself was never stored.
        docs_by_persona: Dict[Optional[str], List[Dict[str, str]]] = {}
        for doc in await db.for_owner(user).list_documents(run["id"]):
            text = await db.for_owner(user).document_text(doc["id"])
            if not text.strip():
                warnings.append(
                    f'Document "{doc["title"]}" had no recoverable text and was skipped.'
                )
                continue
            docs_by_persona.setdefault(doc.get("persona_name"), []).append(
                {"title": doc["title"], "text": text}
            )

        setup_cast: List[Dict[str, Any]] = []
        for member in cast:
            if not isinstance(member, dict):
                continue
            # `documents` (server-side paths) and `document_texts` are both dropped
            # and rebuilt from the table: keeping them would double every inline
            # document, since those were ingested into the table at run start.
            trimmed = {
                k: v for k, v in member.items()
                if k not in ("documents", "document_texts")
            }
            own = docs_by_persona.get(member.get("name"))
            if own:
                trimmed["document_texts"] = own
            setup_cast.append(trimmed)

        # Cast-wide documents have no home in a create-run request — `document_texts`
        # is per-persona only. Saying so is better than silently attaching them to
        # someone, which would change who can retrieve them.
        shared = docs_by_persona.get(None) or []
        if shared:
            titles = ", ".join(d["title"] for d in shared)
            warnings.append(
                f"{len(shared)} cast-wide document(s) could not be carried over "
                f"({titles}): a new run can only attach documents to a named persona. "
                "Paste them into a persona, or upload them again once the run exists."
            )

        # Only the keys the create-run contract defines. A branch or imported run's
        # config carries extras (e.g. `imported`) that would be wrong to replay into
        # a fresh root run.
        setup_config = {
            k: config[k] for k in
            ("max_messages", "generate_avatars", "cognition", "retrieval", "personas")
            if k in config and config[k] is not None
        }

        setup: Dict[str, Any] = {
            "topic": run["topic"],
            "cast": setup_cast,
            "config": setup_config,
        }
        if config.get("model"):
            setup["model"] = config["model"]
        if run.get("name"):
            setup["name"] = run["name"]
        if run.get("description"):
            setup["description"] = run["description"]

        return {"run_id": run["id"], "setup": setup, "warnings": warnings}

    @app.get("/api/runs/{ref}/events")
    async def get_events(
        ref: str,
        after_seq: int = Query(default=-1),
        limit: Optional[int] = Query(default=None),
        user: str = Depends(current_user),
    ) -> Dict[str, Any]:
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        rows = await db.for_owner(user).get_events_after(run["id"], after_seq=after_seq, limit=limit)
        events = [event_row_to_wire(r) for r in rows]
        return {"run_id": run["id"], "events": events}

    # ------------------- Phase 2a: checkpoints + branching ----------------- #
    # State reconstruction/replay: list the per-turn checkpoints and fetch the
    # full SimSnapshot at a given turn (read-only). The branch route forks a new
    # run that resumes forward — the parent is never modified.

    @app.get("/api/runs/{ref}/snapshots")
    async def list_snapshots(ref: str, user: str = Depends(current_user)) -> Dict[str, Any]:
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        snapshots = await db.for_owner(user).list_snapshots(run["id"])
        return {"run_id": run["id"], "snapshots": snapshots}

    @app.get("/api/runs/{ref}/snapshots/{turn}")
    async def get_snapshot(
        ref: str,
        turn: int,
        user: str = Depends(current_user),
    ) -> Dict[str, Any]:
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        snapshot = await db.for_owner(user).get_snapshot(run["id"], turn=turn)
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

    # -------------------- Phase 2c: introspection (read-only) -------------- #
    # Rich per-agent dossier + the "why did it say that?" turn trace. Both are
    # assembled ONLY from genuinely-captured state (latest snapshot + the turn's
    # events). Runs that ran with cognition off have no captured cognition, so
    # the trace reports {available: false} rather than synthesizing a motive.

    @app.get("/api/runs/{ref}/agents/{name}/dossier")
    async def agent_dossier(
        ref: str,
        name: str,
        user: str = Depends(current_user),
    ) -> Dict[str, Any]:
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        snapshot = await db.for_owner(user).get_snapshot(run["id"], turn=None)  # latest
        if snapshot is None or name not in snapshot.agents:
            raise HTTPException(status_code=404, detail="Agent not found for this run")
        agent = snapshot.agents[name]
        memories = [
            {
                "id": m.id, "content": m.content, "importance": m.importance,
                "tags": m.tags, "timestamp": m.timestamp,
            }
            for m in agent.memory_stream
        ]
        beliefs = [m for m in memories if "reflection" in (m["tags"] or [])]
        # Phase 4b: this agent's pending threads (planted by them), with the
        # staleness flag so the dossier can show "dangling" setups. Sourced
        # from the same snapshot — real ledger state only.
        try:
            _cfg = json.loads(run["config_json"]) if run.get("config_json") else {}
        except json.JSONDecodeError:
            _cfg = {}
        _cog = _cfg.get("cognition") if isinstance(_cfg.get("cognition"), dict) else {}
        _stale_after = int(_cog.get("thread_stale_after", 5) or 5)
        agent_threads = []
        for t in snapshot.pending_threads:
            if t.origin_agent != name:
                continue
            entry = t.model_dump()
            entry["stale"] = (
                t.status == "open"
                and (snapshot.total_turns - t.origin_turn) >= _stale_after
            )
            agent_threads.append(entry)
        # Phase 5: what background material this persona has, and which passages
        # it actually drew on. Sourced ONLY from real document.retrieved events —
        # the audit trail, not a claim. A run without retrieval reports empty
        # lists rather than synthesizing anything.
        attached = [
            {
                "document_id": d["id"],
                "title": d["title"],
                "media_type": d["media_type"],
                "char_count": d["char_count"],
                "chunk_count": d["chunk_count"],
                "cast_wide": d["persona_name"] is None,
            }
            for d in await db.for_owner(user).list_documents(run["id"], persona_name=name)
        ]
        drew_on: List[Dict[str, Any]] = []
        for row in await db.for_owner(user).get_events(run["id"]):
            if row["event_type"] != "document.retrieved" or row["agent_name"] != name:
                continue
            payload = row["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    continue
            drew_on.append({
                "turn": row["turn"],
                "query": payload.get("query"),
                "total_chars": payload.get("total_chars"),
                "passages": payload.get("passages", []),
            })
        return {
            "run_id": run["id"],
            "agent": agent.name,
            "persona": agent.persona,
            "goals": agent.goals,
            "memory_stream": memories,
            "beliefs": beliefs,
            "pending_threads": agent_threads,
            "documents": attached,
            "document_retrievals": drew_on,
            # Phase 6: the convictions this persona was seeded with, minus the
            # operator's private fields (`validity`, `underlying_concern`). None
            # for a run that used no structured personas. Deliberately NOT the
            # withheld concern: the dossier is a UI surface, and showing the real
            # worry there would let an operator read off the answer to the thing
            # the panel is supposed to draw out in conversation.
            "structured": structured_payload(agent.structured),
            "relationships": agent.relationships,
            "tokens_in": agent.total_tokens_in,
            "tokens_out": agent.total_tokens_out,
            "cost_usd": agent.total_cost_usd,
            "portrait_key": agent.portrait_key,
            # Legacy: populated only for runs recorded before avatars moved to blobs.
            "portrait_b64": agent.portrait,
        }

    @app.post("/api/runs/{ref}/agents/{name}/regenerate-avatar")
    async def regenerate_avatar(
        ref: str,
        name: str,
        user: str = Depends(current_user),
    ) -> Dict[str, Any]:
        """Regenerate the avatar for a specific agent in a run.

        Produces a NEW portrait (random seed) for the same persona, persists it
        onto the latest snapshot, and returns it. Avatar generation is optional:
        a generation failure surfaces as 502 (upstream image model), not 500.
        """
        import random
        from matrix_studio.avatar import generate_avatar, store_avatar

        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        snapshot = await db.for_owner(user).get_snapshot(run["id"], turn=None)  # latest
        if snapshot is None or name not in snapshot.agents:
            raise HTTPException(status_code=404, detail="Agent not found for this run")

        agent = snapshot.agents[name]

        # Random seed forces a different image for the SAME persona (the point of
        # "regenerate"). The real name is kept in the prompt — do NOT bake a
        # timestamp into persona_name or it leaks into the portrait prompt.
        portrait = await generate_avatar(
            agent.name, agent.persona, seed=random.randint(0, 2**32 - 1)
        )
        if portrait is None:
            raise HTTPException(
                status_code=502,
                detail="Avatar generation is unavailable or was filtered; try again.",
            )

        # Persist the new portrait so it survives reload AND shows across the UI.
        # The cast board / live view derive avatars from the `avatar.ready`
        # EVENT (not the snapshot), so we must append a fresh one; deriveState
        # applies the last avatar.ready per agent, so the new portrait wins.
        # We also update the latest snapshot so the dossier API reflects it.
        # Store the image and keep only its key on the agent — see blobs.py. The
        # key is content-addressed, so a regenerated portrait gets a new key and any
        # URL built from it cache-busts itself.
        agent.portrait_key = store_avatar(portrait)
        agent.portrait = None
        await db.for_owner(user).save_snapshot(snapshot)

        all_events = await db.for_owner(user).get_events_after(run["id"], after_seq=-1, limit=None)
        next_seq = max((e["seq"] for e in all_events), default=-1) + 1
        await db.for_owner(user).append_event(
            run_id=run["id"],
            turn=0,
            seq=next_seq,
            event_type="avatar.ready",
            agent_name=name,
            payload={"agent_name": name, "portrait_key": agent.portrait_key},
        )

        return {
            "run_id": run["id"],
            "agent": name,
            "portrait_key": agent.portrait_key,
        }

    @app.get("/api/runs/{ref}/agents/{name}/avatar")
    async def get_avatar(
        ref: str,
        name: str,
        v: Optional[str] = Query(default=None),
        user: str = Depends(current_user),
    ):
        """Serve a persona's avatar image.

        Route-scoped to the run rather than exposing a bare blob path, which is
        what lets per-user authorisation attach here like every other run route —
        a bare `/blobs/{key}` would have had nothing to authorise against.
        `v` is the content-addressed key: it makes the URL change when the image
        does, which is what lets the response be cached immutably.

        Note the residual exposure, since it is worth being precise about: `v` is
        taken on trust, so a caller holding *any* valid blob key and *any* run of
        their own can fetch that blob. Keys are SHA-256 content hashes, so this is
        not reachable by guessing — but it is not an ownership check either, and
        Phase 2 should key avatars under the owner's S3 prefix rather than rely on
        that.
        """
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        key = v
        if not key:
            # No key supplied: fall back to the latest snapshot's record for this agent.
            snapshot = await db.for_owner(user).get_snapshot(run["id"])
            agent = (snapshot.agents.get(name) if snapshot else None)
            key = getattr(agent, "portrait_key", None) if agent else None
        if not key:
            raise HTTPException(status_code=404, detail="No avatar for this agent")

        data = blobs.get(key)
        if data is None:
            raise HTTPException(status_code=404, detail="Avatar image not found")
        # Immutable: the key IS the content hash, so this body can never change.
        return Response(
            content=data,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/api/runs/{ref}/turns/{turn}/structured")
    async def structured_turn_view(
        ref: str, turn: int, opt_in: bool = Query(default=False),
        user: str = Depends(current_user),
    ) -> Dict[str, Any]:
        """Phase 4d: the OPTIONAL structured (Narrative / Consequences /
        Updated State / Possibilities) projection of one turn. A derived
        read-only view over the turn's canonical events + snapshot — never the
        canonical record, never invented content (every consequence/state line
        carries the seq of its backing event). Default OFF: enable globally via
        settings.structured_output or per-request with ?opt_in=true."""
        if not get_settings().structured_output and not opt_in:
            raise HTTPException(
                status_code=403,
                detail="Structured output view is disabled "
                "(set STRUCTURED_OUTPUT=true or pass ?opt_in=true).",
            )
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        rows = await db.for_owner(user).get_events_after(run["id"], after_seq=-1, limit=None)
        events = [event_row_to_wire(r) for r in rows]
        turn_events = [e for e in events if e["turn"] == turn]
        if not any(e["event_type"] == "agent.response" for e in turn_events):
            raise HTTPException(status_code=404, detail=f"No turn {turn} in this run")
        snapshot = await db.for_owner(user).get_snapshot(run["id"], turn=turn)
        from matrix_studio.structured_view import build_structured_view

        view = build_structured_view(turn, turn_events, snapshot)
        view["run_id"] = run["id"]
        return view

    @app.get("/api/runs/{ref}/pending-threads")
    async def pending_threads(ref: str, user: str = Depends(current_user)) -> Dict[str, Any]:
        """Phase 4b: the run's pending-thread ledger (setups & payoffs), read
        from the latest snapshot. Open threads older than the run's configured
        staleness age (cognition.thread_stale_after, default 5 turns) are
        flagged ``stale`` ("dangling" in the dossier UI). Distinct from the
        Phase 1.5 aside ``/threads`` routes. Empty ledger -> empty list, never
        a synthesized thread."""
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        snapshot = await db.for_owner(user).get_snapshot(run["id"], turn=None)  # latest
        if snapshot is None:
            return {"run_id": run["id"], "threads": [], "stale_after": None}
        try:
            cfg = json.loads(run["config_json"]) if run.get("config_json") else {}
        except json.JSONDecodeError:
            cfg = {}
        cog = cfg.get("cognition") if isinstance(cfg.get("cognition"), dict) else {}
        stale_after = int(cog.get("thread_stale_after", 5) or 5)
        current_turn = snapshot.total_turns
        out = []
        for t in snapshot.pending_threads:
            entry = t.model_dump()
            entry["stale"] = (
                t.status == "open" and (current_turn - t.origin_turn) >= stale_after
            )
            out.append(entry)
        return {
            "run_id": run["id"],
            "threads": out,
            "stale_after": stale_after,
            "as_of_turn": current_turn,
        }

    @app.get("/api/runs/{ref}/turns/{turn}/trace")
    async def turn_trace(ref: str, turn: int, user: str = Depends(current_user)) -> Dict[str, Any]:
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        rows = await db.for_owner(user).get_events_after(run["id"], after_seq=-1, limit=None)
        events = [event_row_to_wire(r) for r in rows]
        turn_events = [e for e in events if e["turn"] == turn]
        selected = next((e for e in turn_events if e["event_type"] == "speaker.selected"), None)
        response = next((e for e in turn_events if e["event_type"] == "agent.response"), None)
        if response is None:
            raise HTTPException(status_code=404, detail=f"No turn {turn} in this run")

        payload = response["payload"]
        # A trace only exists if cognition genuinely captured a rationale for the
        # turn. No rationale -> cognition was off; we do NOT fabricate one.
        if payload.get("rationale") is None:
            return {"run_id": run["id"], "turn": turn, "available": False}

        # Resolve memory_refs (ids surfaced into the prompt for this turn) to the
        # actual memory items from the turn's snapshot.
        refs = payload.get("memory_refs") or []
        resolved: List[Dict[str, Any]] = []
        if refs:
            snap = await db.for_owner(user).get_snapshot(run["id"], turn=turn)
            if snap is not None:
                by_id = {
                    m.id: m
                    for a in snap.agents.values()
                    for m in a.memory_stream
                }
                for rid in refs:
                    m = by_id.get(rid)
                    if m is not None:
                        resolved.append({
                            "id": m.id, "content": m.content,
                            "importance": m.importance, "tags": m.tags,
                        })
        # Phase 4a: surface the turn's validation trail (checked attempts +
        # flag, if any) so the why-trace can show "validated / regenerated once
        # for a causality violation". Empty when validation was off — we never
        # synthesize a validation record.
        validation_checks = [
            e["payload"] for e in turn_events
            if e["event_type"] == "validation.checked"
        ]
        validation_flag = next(
            (e["payload"] for e in turn_events
             if e["event_type"] == "validation.flagged"),
            None,
        )

        return {
            "run_id": run["id"],
            "turn": turn,
            "available": True,
            "speaker": payload.get("speaker"),
            "selection_reason": (selected or {}).get("payload", {}).get("reason"),
            "utterance": payload.get("message"),
            "rationale": payload.get("rationale"),
            "goal_served": payload.get("goal_served"),
            "memory_refs": refs,
            "memories": resolved,
            "validation": {"checks": validation_checks, "flagged": validation_flag},
        }

    @app.post("/api/runs/{ref}/branch", status_code=201)
    async def branch_run(
        ref: str,
        body: BranchModel,
        user: str = Depends(current_user),
    ) -> Dict[str, Any]:
        parent = await db.for_owner(user).get_run_by_ref(ref)
        if not parent:
            raise HTTPException(status_code=404, detail="Run not found")
        # The fork turn must exist in the parent's history. We validate against
        # the parent's turn count (agent.response events) so a caller can only
        # branch from a turn that actually happened.
        stats = await db.for_owner(user).get_run_stats(parent["id"])
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
    async def run_tree(ref: str, user: str = Depends(current_user)) -> Dict[str, Any]:
        """Phase 2b branch-tree. Returns the full lineage rooted at the ancestor
        of ``ref``, with each node's mutation kind (for edge labels) and status.
        Nodes are ordered oldest-first; edges are inferred from parent_run_id.
        """
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        tree = await db.for_owner(user).get_run_tree(run["id"])
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
    async def resume_run(ref: str, user: str = Depends(current_user)) -> Dict[str, Any]:
        """
        Error-recovery: resume an interrupted/failed run forward IN PLACE (same
        run id/codename), continuing from its last checkpoint. Non-blocking —
        returns immediately; generation runs in the background and streams over
        the existing WS. A completed run cannot be resumed (branch it instead).
        """
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        try:
            return await manager.resume_run(run)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/runs/{ref}/stop", status_code=202)
    async def stop_run(ref: str, user: str = Depends(current_user)) -> Dict[str, Any]:
        """Ask a live run to stop after the turn it is currently generating.

        A request, not a kill: the in-flight turn finishes and is persisted, then
        the run ends in the terminal ``stopped`` status. Cancelling mid-call would
        discard tokens already paid for and leave a partial turn for a later resume
        to trim, so waiting one turn is the cheaper trade.

        ``stopped`` is deliberately its own status rather than reusing
        ``interrupted``: it is resumable exactly the same way, but it records that
        someone chose to end the run rather than that the process died.

        Returns 202 while the request is registered, since the effect lands a turn
        later. Idempotent — a second request on the same live run is not an error.
        """
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        if run.get("status") in orchestration.TERMINAL_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"Run is '{run.get('status')}'; there is nothing to stop.",
            )
        try:
            # Phase 5: durable, because under Step Functions the turn is generated by
            # a worker Lambda and this process has no task to find. The in-memory
            # check `request_stop` performs would refuse every stop on the deployed
            # system while working perfectly on a laptop.
            return await manager.request_stop_durable(run)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    # ------------------- Phase 1.5: summary + aside threads ---------------- #
    # All routes below are ADDITIVE and READ-ONLY over the canonical run: they
    # read the run's transcript/cast and write only to the new summaries /
    # threads / thread_messages tables. They never emit a canonical event,
    # mutate the snapshot, or change the run's recorded cost. Summaries and
    # aside replies are model-generated ANALYSIS, labeled as such by the UI.

    async def _require_run(ref: str, owner_sub: str) -> Dict[str, Any]:
        """Resolve one of ``owner_sub``'s runs, or 404.

        ``owner_sub`` is a parameter rather than something read from an enclosing
        scope because this helper is shared by the summary and thread routes: if it
        closed over an identity it would be the same one for every caller, which is
        the exact bug it exists to prevent.
        """
        # Bound, not `owner_sub=`: binding is what attaches the tenant-scoped
        # credentials as well as scoping the query. See `for_owner`.
        run = await db.for_owner(owner_sub).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return run

    async def _require_thread_run(
        thread: Dict[str, Any], owner_sub: str
    ) -> Dict[str, Any]:
        """The run behind an aside thread, if it belongs to ``owner_sub``.

        Raises 404 "Thread not found" — not "Run not found" — because the caller
        addressed a thread, and the run's existence is not theirs to learn about.
        """
        run = await db.for_owner(owner_sub).get_run_by_ref(thread["run_id"])
        if not run:
            raise HTTPException(status_code=404, detail="Thread not found")
        return run

    def _public_documents(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Strip the storage layer's own keys before a document reaches a client.

        `pk`, `sk` and `s3_key` are how the backend addresses an item; a client that
        started reading them would be coupled to the key design, and the key design is
        the thing most likely to change (Phase 6 re-partitions documents by knowledge
        base). `owner_sub` goes too — the caller already knows who they are, and echoing
        another tenant's sub back would be a disclosure if it were ever wrong.
        """
        internal = {"pk", "sk", "s3_key", "owner_sub", "document_id"}
        return [
            {k: v for k, v in row.items() if k not in internal} for row in rows
        ]

    def _shape_summaries(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Split stored summaries into generated + imported for the client."""
        generated = next((r for r in rows if r["kind"] == "generated"), None)
        imported = next((r for r in rows if r["kind"] == "imported"), None)
        return {"generated": generated, "imported": imported}

    @app.get("/api/runs/{ref}/summary")
    async def get_summary(ref: str, user: str = Depends(current_user)) -> Dict[str, Any]:
        run = await _require_run(ref, user)
        rows = await db.for_owner(user).get_summaries(run["id"])
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
        ref: str, body: SummaryRequestModel = SummaryRequestModel(),
        user: str = Depends(current_user),
    ) -> Dict[str, Any]:
        run = await _require_run(ref, user)
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
            db.for_owner(user),
            run,
            fields=fields,
            focus=focus,
            model=body.model,
            instructions=instructions,
        )
        rows = await db.for_owner(user).get_summaries(run["id"])
        return {
            "run_id": run["id"],
            "generated": saved,
            "default_instructions": analysis.DEFAULT_SUMMARY_INSTRUCTIONS,
            **_shape_summaries(rows),
        }

    @app.get("/api/runs/{ref}/threads")
    async def list_threads(ref: str, user: str = Depends(current_user)) -> Dict[str, Any]:
        run = await _require_run(ref, user)
        threads = await db.for_owner(user).list_threads(run["id"])
        return {"run_id": run["id"], "threads": threads}

    @app.post("/api/runs/{ref}/threads", status_code=201)
    async def create_thread(
        ref: str,
        body: CreateThreadModel,
        user: str = Depends(current_user),
    ) -> Dict[str, Any]:
        run = await _require_run(ref, user)
        try:
            thread = await service.create_thread(
                db.for_owner(user), run, target=body.target, persona_name=body.persona_name
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return thread

    @app.get("/api/threads/{thread_id}")
    async def get_thread(
        thread_id: str, user: str = Depends(current_user)
    ) -> Dict[str, Any]:
        """Read an aside thread.

        The only two routes addressed by a thread id rather than a run ref, so the
        run behind the thread has NOT been authorised by the path. Resolving it
        through the scoped lookup is what closes that: a thread id belonging to
        another tenant's run reads as "thread not found", identical to one that
        never existed.
        """
        thread = await db.for_owner(user).get_thread(thread_id)
        if not thread:
            raise HTTPException(status_code=404, detail="Thread not found")
        await _require_thread_run(thread, user)
        messages = await db.for_owner(user).get_thread_messages(thread_id)
        cost = await db.for_owner(user).thread_cost(thread_id)
        return {**thread, "messages": messages, "total_cost_usd": cost}

    @app.post("/api/threads/{thread_id}/messages", status_code=201)
    async def post_thread_message(
        thread_id: str, body: ThreadMessageModel,
        user: str = Depends(current_user),
    ) -> Dict[str, Any]:
        thread = await db.for_owner(user).get_thread(thread_id)
        if not thread:
            raise HTTPException(status_code=404, detail="Thread not found")
        if not body.content.strip():
            raise HTTPException(status_code=422, detail="Message content is required")
        run = await _require_thread_run(thread, user)
        reply = await service.post_aside_message(
            db.for_owner(user), run, thread, user_message=body.content.strip(), model=body.model
        )
        cost = await db.for_owner(user).thread_cost(thread_id)
        return {"thread_id": thread_id, "reply": reply, "total_cost_usd": cost}

    # ------------------- Phase 5: per-persona documents -------------------- #
    # Attach background material to a SPECIFIC persona, list/delete it, rebuild
    # the search index, and — most importantly — INSPECT what a query actually
    # retrieves. BM25 is lexical, so retrieval quality has to be measurable
    # rather than trusted; /documents/search exists to produce that evidence.

    @app.get("/api/runs/{ref}/documents")
    async def list_documents(
        ref: str,
        persona: Optional[str] = Query(
            default=None,
            description="Restrict to what this persona can see (its own + cast-wide)",
        ),
        user: str = Depends(current_user),
    ) -> Dict[str, Any]:
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        docs = _public_documents(
            await db.for_owner(user).list_documents(run["id"], persona_name=persona)
        )
        return {
            "run_id": run["id"],
            "persona": persona,
            "documents": docs,
            "total_chars": sum(int(d["char_count"] or 0) for d in docs),
            "total_chunks": sum(int(d["chunk_count"] or 0) for d in docs),
        }

    @app.post("/api/runs/{ref}/documents", status_code=201)
    async def attach_document(
        ref: str,
        body: AttachDocumentModel,
        user: str = Depends(current_user),
    ) -> Dict[str, Any]:
        """Attach a document by inline text or by a server-readable path.

        ``persona_name`` omitted/null makes the document cast-wide. A persona
        name that is not in the run's cast is rejected rather than silently
        creating material no one can ever retrieve.
        """
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        if bool(body.text) == bool(body.path):
            raise HTTPException(
                status_code=422,
                detail="Provide exactly one of 'text' or 'path'",
            )

        if body.persona_name is not None:
            cast_names = {
                p.get("name") for p in (_parse_cast(run) or []) if isinstance(p, dict)
            }
            if cast_names and body.persona_name not in cast_names:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"persona_name {body.persona_name!r} is not in this run's cast "
                        f"({', '.join(sorted(n for n in cast_names if n))})"
                    ),
                )

        try:
            if body.text:
                if not body.title:
                    raise HTTPException(
                        status_code=422, detail="title is required when supplying text"
                    )
                doc = ingest_text(body.text, title=body.title)
            else:
                doc = ingest_file(body.path, title=body.title)
        except ExtractionError as exc:
            # A document we cannot read is a client-supplied problem, not a
            # server fault; the message names the cause (and any missing package).
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        document_id = await db.for_owner(user).add_document(
            run_id=run["id"],
            title=doc.title,
            chunks=[c.content for c in doc.chunks],
            persona_name=body.persona_name,
            source_path=doc.source_path,
            media_type=doc.media_type,
            char_count=doc.char_count,
            # The ORIGINAL extracted text, not just the chunks. Without it the stored
            # body is `join_chunks(chunks)`, which is not an exact inverse — measured
            # on two real documents it shifts 2–7% of chunk boundaries, so an
            # `ordinal` can point at different text from the vector built at that
            # ordinal, and hybrid retrieval fuses the two arms by chunk id. See the
            # §4a correction in AWS-SERVERLESS-ARCHITECTURE.md.
            #
            # This route was writing `text_is_original: false` on every upload, which
            # is exactly the condition that correction exists to avoid, on the ONE
            # path a user actually uses.
            text=doc.text,
        )
        return {
            "run_id": run["id"],
            "document_id": document_id,
            "title": doc.title,
            "persona_name": body.persona_name,
            "media_type": doc.media_type,
            "char_count": doc.char_count,
            "chunk_count": len(doc.chunks),
        }

    @app.delete("/api/runs/{ref}/documents/{document_id}")
    async def delete_document(
        ref: str,
        document_id: str,
        user: str = Depends(current_user),
    ) -> Dict[str, Any]:
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        # Confirm the document belongs to THIS run before deleting it, so a
        # document id from another run cannot be removed through this route.
        owned = {d["id"] for d in await db.for_owner(user).list_documents(run["id"])}
        if document_id not in owned:
            raise HTTPException(status_code=404, detail="Document not found for this run")
        await db.for_owner(user).delete_document(document_id)
        return {"run_id": run["id"], "document_id": document_id, "deleted": True}

    @app.post("/api/runs/{ref}/documents/reindex")
    async def reindex_documents(ref: str, user: str = Depends(current_user)) -> Dict[str, Any]:
        """Rebuild the search index from ``doc_chunks``, the source of truth.

        This is the documented recovery path for a stale or corrupt index. It is
        safe to run at any time and is idempotent: the FTS5 table is
        external-content, so it holds no text of its own and is always a pure
        derivative of the chunks table.
        """
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        indexed = await db.for_owner(user).reindex_documents()
        return {"run_id": run["id"], "reindexed_chunks": indexed}

    @app.post("/api/runs/{ref}/documents/embed")
    async def embed_documents(
        ref: str,
        model: Optional[str] = Query(default=None, description="LiteLLM embedding model"),
        user: str = Depends(current_user),
    ) -> Dict[str, Any]:
        """Embed a run's chunks for vector/hybrid retrieval.

        Idempotent and resumable — only chunks without a vector are embedded, so
        re-running never pays to redo work. Reports real tokens and cost, because
        embeddings are a per-chunk spend and the cost gate applies to them too.

        Returns 422 (not 500) when the embedding provider is unavailable: that is a
        deployment condition the caller can act on rather than a server fault.
        """
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        # A BOUND store: `embed_pending_chunks` reaches `chunks_missing_vectors` and
        # `store_chunk_vectors`, both of which scope by owner — vectors carry
        # `owner_sub` metadata and a filtered query cannot exclude what it cannot see.
        stats = await embed_pending_chunks(
            db.for_owner(user), run["id"], embedding_model=model or ""
        )
        if stats.get("error"):
            raise HTTPException(status_code=422, detail=stats["error"])
        return {
            "run_id": run["id"],
            **stats,
            "chunks_with_vectors": await db.for_owner(user).count_chunk_vectors(run["id"]),
        }

    @app.get("/api/runs/{ref}/documents/search")
    async def search_documents(
        ref: str,
        q: str = Query(..., min_length=1, description="Free text; sanitised server-side"),
        persona: Optional[str] = Query(default=None),
        k: int = Query(default=5, ge=1, le=50),
        max_chars: int = Query(default=2000, ge=1),
        corpus: str = Query(
            default="run",
            pattern="^(run|database)$",
            description=(
                "BM25 statistics source. 'run' (default) scores against this run's "
                "slice only, so a score is reproducible. 'database' is the old "
                "whole-index behaviour, kept so the difference can be seen."
            ),
        ),
        user: str = Depends(current_user),
    ) -> Dict[str, Any]:
        """Inspect what a query retrieves, without running a simulation.

        This is the measurement tool the design depends on: it returns the
        sanitised FTS5 query alongside the passages and their BM25 scores, so the
        lexical-vs-semantic gap can be quantified on a real corpus instead of
        argued about. Read-only — it performs no writes.
        """
        run = await db.for_owner(user).get_run_by_ref(ref)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        fts_query = build_fts_query(q)
        if not fts_query:
            return {
                "run_id": run["id"],
                "query": q,
                "fts_query": "",
                "terms": [],
                "passages": [],
                "note": "No searchable terms in the query after removing stopwords.",
            }
        rows = await db.for_owner(user).search_documents(
            run_id=run["id"], query=fts_query, persona_name=persona, k=k, corpus=corpus
        )
        passages = apply_budget(rows, max_chars=max_chars)
        return {
            "run_id": run["id"],
            "query": q,
            "fts_query": fts_query,
            "terms": extract_terms(q),
            "persona": persona,
            # Reported because a score is only comparable to another score from the
            # same corpus, and this is the one knob that changes what it means.
            "corpus": corpus,
            "passages": [
                {
                    "chunk_id": p.chunk_id,
                    "document_id": p.document_id,
                    "title": p.title,
                    "ordinal": p.ordinal,
                    "score": round(p.score, 4),
                    "chars": len(p.content),
                    "content": p.content,
                    "citation": p.citation,
                }
                for p in passages
            ],
            "total_chars": sum(len(p.content) for p in passages),
            "matched_before_budget": len(rows),
        }

    # --------------------------- WebSocket stream -------------------------- #
    @app.websocket("/api/runs/{ref}/stream")
    async def stream(
        websocket: WebSocket,
        ref: str,
        user: str = Depends(current_user_ws),
    ) -> None:
        await websocket.accept()

        run = await db.for_owner(user).get_run_by_ref(ref)
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
            rows = await db.for_owner(user).get_events_after(run_id, after_seq=-1)
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

    # Cache policy for a hashed-asset build. Getting this wrong is not a
    # performance nit, it is a hard failure, and it was observed:
    #
    #   Vite emits content-hashed bundles (index-CyGdWPwT.js) and rewrites
    #   index.html to point at the current one. Old bundles are deleted on rebuild.
    #   FileResponse sent `etag` and `last-modified` but NO `Cache-Control`, so a
    #   browser was free to reuse a cached index.html — which referenced a bundle
    #   that no longer existed. The shell loaded, the module 404'd, and the page
    #   hung blank with nothing in the network log to explain it.
    #
    # So: the HTML shell must always be revalidated, and the hashed assets can be
    # cached forever precisely BECAUSE their names change when their content does.
    # This matters most on upgrade — a new image with the same URL is exactly the
    # situation that serves a stale shell.
    INDEX_CACHE = {"Cache-Control": "no-cache, must-revalidate"}
    ASSET_CACHE = {"Cache-Control": "public, max-age=31536000, immutable"}

    if index_file.exists():  # noqa: C901
        assets_dir = STATIC_DIR / "assets"
        if assets_dir.exists():
            # A StaticFiles mount takes precedence over the catch-all below, so the
            # immutable header has to be set on the mount itself — setting it in the
            # fallback route silently did nothing, which a test caught.
            class _ImmutableStatic(StaticFiles):
                def file_response(self, *args: Any, **kwargs: Any):  # type: ignore[override]
                    response = super().file_response(*args, **kwargs)
                    response.headers.update(ASSET_CACHE)
                    return response

            app.mount("/assets", _ImmutableStatic(directory=str(assets_dir)), name="assets")

        @app.get("/")
        async def _index() -> FileResponse:
            return FileResponse(str(index_file), headers=INDEX_CACHE)

        @app.get("/{full_path:path}")
        async def _spa(full_path: str) -> FileResponse:
            # Serve real static files when they exist, else SPA-fallback.
            candidate = STATIC_DIR / full_path
            if candidate.is_file():
                # Content-hashed filenames are safe to cache immutably; anything
                # else (favicon, manifest) gets the conservative shell policy.
                immutable = "/assets/" in f"/{full_path}" or bool(
                    re.search(r"-[A-Za-z0-9_-]{8,}\.(js|css|woff2?)$", full_path)
                )
                return FileResponse(
                    str(candidate), headers=ASSET_CACHE if immutable else INDEX_CACHE
                )
            # SPA fallback serves the shell, so it takes the shell's policy — a
            # cached deep link pointing at a dead bundle is the same failure.
            return FileResponse(str(index_file), headers=INDEX_CACHE)
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
