# SPDX-License-Identifier: Apache-2.0
"""
Analysis service — orchestrates the Phase 1.5 read-only analysis layer over a
completed run: loads a run's transcript/cast, calls the (mockable) ``analysis``
module, and persists results into the ADDITIVE tables only (summaries /
threads / thread_messages).

Shared by the RunManager (auto-summary at completion) and the API routes
(on-demand summary + aside threads) so both paths behave identically.

READ-ONLY INVARIANT: no function here writes to ``events`` or ``snapshots`` or
mutates a run's recorded cost. Analysis token/cost is persisted to the new
tables and reported separately from the canonical run cost.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from matrix_studio import analysis
from matrix_studio import experts as experts_mod
from matrix_studio.settings import get_settings
from matrix_studio.storage import Database

logger = logging.getLogger(__name__)


def _run_config(run: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a run row's config_json (best-effort)."""
    raw = run.get("config_json")
    if not raw:
        return {}
    try:
        cfg = json.loads(raw)
        return cfg if isinstance(cfg, dict) else {}
    except json.JSONDecodeError:
        return {}


def resolve_model(
    run: Dict[str, Any], override: Optional[str] = None, role: str = "summary"
) -> Optional[str]:
    """
    Resolve the model for an analysis call: an explicit override wins, then the
    run's configured model, else None (analysis falls back to the current
    settings default).

    Exception for IMPORTED and BRANCH runs: their stored model string is not a
    model this run actually generated fresh with here —
      * imported runs carry a legacy model from another system (e.g. the
        bridge-supply fixture records an EOL Haiku);
      * branch/resumed runs inherit their parent's config model, which may be
        stale/EOL, even though the engine always generates with the current
        settings default (never the config model).
    A summary/aside is NEW analysis we compute now — not a replay — so for these
    runs we prefer the current settings default rather than forwarding a
    possibly-dead inherited model. We never substitute a specific EOL model; we
    only decline to reuse a stale one.
    """
    if override:
        return override
    # A branch (parent_run_id set) inherited its config model from the parent;
    # treat it like an imported run and use the current default for fresh
    # analysis rather than the possibly-stale inherited model.
    if run.get("parent_run_id"):
        return None
    cfg = _run_config(run)
    if cfg.get("imported"):
        return None  # use the current settings default for fresh analysis
    # Through `ModelSet`, the resolver the ENGINE uses, so a per-role `models.summary` (or
    # `models.aside`) is honoured. This read only `config.model` before, which made the per-role
    # choice inert for analysis: measured on brainstorm-opus, whose definition pinned the
    # summary to Sonnet 5 and whose summary ran on Opus 5 — found in Bedrock's invocation log,
    # not from anything the run reported. `config.model` still applies when no role is named.
    from matrix_studio.models import ModelSet

    return ModelSet.from_config(cfg).resolve(role) or None


def summary_config(run: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return the effective summary config for a run, applying the Phase 1.5
    default (enabled + full field set) when the run specified nothing.
    """
    cfg = _run_config(run)
    sc = cfg.get("summary")
    if not isinstance(sc, dict):
        sc = {}
    enabled = sc.get("enabled", True)
    fields = sc.get("fields") or list(analysis.DEFAULT_SUMMARY_FIELDS)
    # Keep only recognized fields, preserving the canonical order.
    fields = [f for f in analysis.DEFAULT_SUMMARY_FIELDS if f in fields] or list(
        analysis.DEFAULT_SUMMARY_FIELDS
    )
    return {
        "enabled": bool(enabled),
        "fields": fields,
        "focus": sc.get("focus"),
        "instructions": sc.get("instructions"),
    }


async def _load_conversation(
    db: Database, run: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Load a completed run's transcript from its snapshot (read-only)."""
    snapshot = await db.get_snapshot(run["id"])
    if snapshot is not None:
        return list(snapshot.conversation)
    return []


def _load_cast(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Load a run's cast (persona name + real stored persona text)."""
    try:
        cast = json.loads(run["cast_json"])
        return cast if isinstance(cast, list) else []
    except (KeyError, json.JSONDecodeError):
        return []


async def generate_and_store_summary(
    db: Database,
    run: Dict[str, Any],
    fields: Optional[List[str]] = None,
    focus: Optional[str] = None,
    model: Optional[str] = None,
    instructions: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate a structured summary for a completed run and persist it as a
    'generated' summary (never overwriting an imported original). Returns the
    stored summary dict (with `parsed` flag). Never raises for LLM issues —
    ``analysis.generate_summary`` degrades gracefully.

    ``instructions`` (optional) REPLACES the default analyst-role framing; the
    non-negotiable guardrails always remain. The effective instructions (NULL
    when the default was used) are persisted so the regenerate UI can prefill
    the prompt that created the summary. Backward-compatible: omit → default.
    """
    conversation = await _load_conversation(db, run)
    topic = run.get("topic", "")
    from matrix_studio import assumptions as assumptions_mod

    result = await analysis.generate_summary(
        conversation=conversation,
        topic=topic,
        fields=fields,
        focus=focus,
        model=resolve_model(run, model),
        instructions=instructions,
        context=assumptions_mod.summary_note(assumptions_mod.from_events(await db.get_events(run["id"]))),
    )
    saved = await db.save_summary(
        run_id=run["id"],
        payload=result["payload"],
        kind="generated",
        tokens_in=result["tokens_in"],
        tokens_out=result["tokens_out"],
        cost_usd=result["cost_usd"],
        instructions=result["instructions"],
    )
    saved["parsed"] = result["parsed"]
    # Charged to the owner's month, here rather than in the auto-summary path, because every
    # summary is generated through this function — including a regenerate from the UI, which is
    # a real model call over the whole transcript. It was charged to nobody: measured on
    # brainstorm-opus as ~$0.21, the largest single uncounted call in the run. Best-effort, for
    # the reason `record_spend` gives: a missed increment delays the cap rather than losing a
    # summary that has already been paid for and stored.
    cost = float(result.get("cost_usd") or 0.0)
    owner = run.get("owner_sub")
    if cost > 0 and owner:
        try:
            await db.add_user_spend(cost, owner_sub=str(owner))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not record $%.4f of summary spend for run %s: %s",
                           cost, run.get("id"), exc)
    return saved


async def maybe_autogenerate_summary(db: Database, run_id: str) -> None:
    """
    Called after a run completes. Generates the default summary unless the run's
    summary config disabled it. Best-effort: any failure is logged and swallowed
    so it can never break run completion (the run is already recorded).

    This runs entirely on the additive tables — it does NOT emit a canonical
    event, touch the snapshot, or change the run's recorded cost.
    """
    try:
        run = await db.get_run(run_id)
        if not run:
            return
        cfg = summary_config(run)
        if not cfg["enabled"]:
            logger.info("Auto-summary disabled for run %s", run_id)
            return
        await generate_and_store_summary(
            db,
            run,
            fields=cfg["fields"],
            focus=cfg["focus"],
            instructions=cfg.get("instructions"),
        )
        logger.info("Auto-generated summary for run %s", run_id)
    except Exception:  # noqa: BLE001 - analysis must never break completion
        logger.exception("Auto-summary generation failed for run %s", run_id)


# --------------------------------------------------------------------------- #
# Aside threads.
# --------------------------------------------------------------------------- #
async def create_thread(
    db: Database,
    run: Dict[str, Any],
    target: str,
    persona_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create an aside thread on a run after validating the target. For a persona
    target, the persona name must exist in the run's cast (we reuse the REAL
    stored persona text, never inventing one).
    """
    if target not in ("analyst", "persona", "room", "consultant"):
        raise ValueError(f"Unknown target: {target}")
    if target == "consultant":
        # A consultant defined on THIS run (matrix_studio/experts.py): the reviewer asks it directly,
        # and it answers from its own sources exactly as it would a persona.
        names = {e.name for e in experts_mod.from_config(_run_config(run))}
        if not persona_name or persona_name not in names:
            raise ValueError(f"persona_name must be one of the run's consultants: {sorted(names)}")
    if target == "persona":
        cast = _load_cast(run)
        names = {c.get("name") for c in cast}
        if not persona_name or persona_name not in names:
            raise ValueError(
                f"persona_name must be one of the run's cast: {sorted(n for n in names if n)}"
            )
    thread_id = str(uuid.uuid4())
    return await db.create_thread(
        thread_id=thread_id,
        run_id=run["id"],
        target=target,
        persona_name=persona_name if target in ("persona", "consultant") else None,
        mode="aside",
    )


async def post_aside_message(
    db: Database,
    run: Dict[str, Any],
    thread: Dict[str, Any],
    user_message: str,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Post a user message to an aside thread, run the appropriate read-only LLM
    call(s), persist both the user turn and the target reply, and return the
    target reply dict. Multi-turn: prior thread messages are passed as context.

    Read-only: writes ONLY to thread_messages; the canonical run is untouched.
    """
    conversation = await _load_conversation(db, run)
    topic = run.get("topic", "")
    resolved_model = resolve_model(run, model, role="aside")

    # Persist the user turn first so it is part of the history for THIS call's
    # follow-ups (but not passed as the current user_message again).
    history = await db.get_thread_messages(thread["id"])
    # Generate FIRST, store after. The user's message used to be stored before the reply was generated,
    # so a reply that timed out left the question unanswered in the thread for good (seen 2026-09-28).
    reply = await asyncio.wait_for(
        _aside_reply(run, thread, user_message, conversation, topic, history, resolved_model, db=db),
        timeout=ASIDE_DEADLINE_SECONDS,
    )
    await db.add_thread_message(
        thread_id=thread["id"],
        role="user",
        speaker="user",
        content=user_message,
    )
    stored = await db.add_thread_message(
        thread_id=thread["id"],
        role="target",
        speaker=reply["speaker"],
        content=reply["content"],
        tokens_in=reply["tokens_in"],
        tokens_out=reply["tokens_out"],
        cost_usd=reply["cost_usd"],
    )
    # Surface per-persona breakdown for a room reply (not persisted separately;
    # the combined content is the canonical stored form).
    if "replies" in reply:
        stored["replies"] = reply["replies"]
    return stored


#: The server's own deadline for generating an aside reply. The deployed API gives a request 30 s in
#: total, including any cold start, so the reply must finish well inside that — or fail with a clear
#: message instead of the gateway's bare 504.
ASIDE_DEADLINE_SECONDS = 26


async def _aside_reply(
    run: Dict[str, Any], thread: Dict[str, Any], user_message: str,
    conversation: List[Dict[str, Any]], topic: str, history: List[Dict[str, Any]],
    resolved_model: Optional[str], max_tokens: int = 0, db: Optional[Database] = None,
) -> Dict[str, Any]:
    """The target's reply to one aside message. Pure generation: stores nothing."""
    target = thread["target"]
    if target == "consultant":
        return await _consultant_reply(db, run, thread["persona_name"], user_message, resolved_model)
    if target == "analyst":
        return await analysis.analyst_reply(
            user_message=user_message,
            conversation=conversation,
            topic=topic,
            thread_history=history,
            model=resolved_model,
            max_tokens=max_tokens,
        )
    if target == "persona":
        cast = _load_cast(run)
        persona = next(
            (c for c in cast if c.get("name") == thread["persona_name"]), None
        )
        if persona is None:
            raise ValueError("Persona no longer present in run cast")
        return await analysis.persona_reply(
            user_message=user_message,
            persona_name=persona["name"],
            persona_text=persona.get("persona", ""),
            conversation=conversation,
            topic=topic,
            thread_history=history,
            model=resolved_model,
            max_tokens=max_tokens,
        )
    if target == "room":
        return await analysis.room_reply(
            user_message=user_message,
            cast=_load_cast(run),
            conversation=conversation,
            topic=topic,
            thread_history=history,
            model=resolved_model,
            max_tokens=max_tokens,
        )
    raise ValueError(f"Unknown target: {target}")  # pragma: no cover - guarded at creation


async def _consultant_reply(
    db: Optional[Database], run: Dict[str, Any], name: str, question: str, model: Optional[str],
) -> Dict[str, Any]:
    """A consultant answering the reviewer directly: retrieval over ITS sources, then the same
    `experts.answer` the engine uses mid-conversation: cited, or "That isn't in my sources."
    """
    from matrix_studio.models import model_for
    from matrix_studio.retrieval import retrieve_for_turn
    from matrix_studio.state import RetrievalConfig

    config = _run_config(run)
    expert = next((e for e in experts_mod.from_config(config) if e.name == name), None)
    if expert is None:
        raise ValueError("Consultant no longer present in the run's config")
    retrieval = RetrievalConfig.from_config(config)
    passages: List[Any] = []
    if db is not None and retrieval.enabled:
        passages, _q, _floor, _fail = await retrieve_for_turn(
            db, run["id"], expert.name, "", [{"speaker": "reviewer", "content": question}],
            k=retrieval.k, max_chars=retrieval.max_chars, recent_turns=1, mode=retrieval.mode,
            embedding_model=retrieval.embedding_model, rrf_k=retrieval.rrf_k,
            min_similarity=retrieval.min_similarity,
        )
    settings = get_settings()
    result = await experts_mod.answer(
        expert, question, "The reviewer", run.get("topic", ""), passages,
        model=model_for(model, "voice") or settings.litellm_model, settings=settings,
    )
    return {"speaker": expert.speaker, "content": result["answer"], "tokens_in": result["tokens_in"],
            "tokens_out": result["tokens_out"], "cost_usd": result["cost_usd"]}


# --------------------------------------------------------------------------- #
# Background replies — the aside WORKER (infra: AsideFunction)
# --------------------------------------------------------------------------- #

#: A question older than this with no reply is treated as abandoned: the worker's own timeout is 3 min,
#: so a reply cannot still be coming. Without it, a worker that died silently would lock the thread.
ASIDE_PENDING_SECONDS = 240


def aside_function_name() -> str:
    """The aside worker's name, set by the stack on the API. Empty locally and in tests, where the
    reply is generated in the request as before."""
    return os.environ.get("ASIDE_FUNCTION", "")


def pending_question(messages: List[Dict[str, Any]], now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The thread's unanswered question, if one is still within its reply window."""
    if not messages or messages[-1].get("role") != "user":
        return None
    last = messages[-1]
    age = (now or time.time()) - float(last.get("created_at") or 0)
    return last if age < ASIDE_PENDING_SECONDS else None


async def dispatch_aside(thread_id: str, owner_sub: str, user_message: str, model: Optional[str]) -> None:
    """Hand one reply to the aside worker, asynchronously, and return at once."""
    def _invoke() -> None:
        import boto3

        boto3.client("lambda", region_name=os.environ.get("AWS_REGION")).invoke(
            FunctionName=aside_function_name(),
            # Event: the whole point is that nothing waits on the reply inside an HTTP request.
            InvocationType="Event",
            Payload=json.dumps({"thread_id": thread_id, "owner_sub": owner_sub,
                                "user_message": user_message, "model": model}).encode(),
        )
    await asyncio.to_thread(_invoke)


async def dispatch_summary(run_id: str, owner_sub: str, *, fields: Optional[List[str]], focus: Optional[str],
                           model: Optional[str], instructions: Optional[str]) -> None:
    """Hand a requested summary to the aside worker, asynchronously, and return at once.

    The same worker as asides because it is the same shape of job — one long model call over a finished
    transcript — and the same reason: measured 2026-09-28, a 40-turn run's summary took 30.4 s with the
    old fields and 56.9 s with the evidence plan, against the request's 30 s limit. The run's AUTOMATIC
    summary is unaffected; it is generated in the finalise worker, which has five minutes.
    """
    def _invoke() -> None:
        import boto3

        boto3.client("lambda", region_name=os.environ.get("AWS_REGION")).invoke(
            FunctionName=aside_function_name(),
            InvocationType="Event",
            Payload=json.dumps({"kind": "summary", "run_id": run_id, "owner_sub": owner_sub,
                                "fields": fields, "focus": focus, "model": model,
                                "instructions": instructions}).encode(),
        )
    await asyncio.to_thread(_invoke)


async def summarise_in_background(
    db: Database, run_id: str, *, fields: Optional[List[str]] = None, focus: Optional[str] = None,
    model: Optional[str] = None, instructions: Optional[str] = None,
) -> Dict[str, Any]:
    """The worker's job for a requested summary. Never raises.

    A failed model call is already stored as a summary that says so (`analysis.generate_summary`
    degrades rather than raising), so the browser polling for a new summary always gets one.
    """
    run = await db.get_run(run_id)
    if not run:
        return {"run_id": run_id, "stored": False, "error": "run not found"}
    try:
        saved = await generate_and_store_summary(
            db, run, fields=fields, focus=focus, model=model, instructions=instructions,
        )
        return {"run_id": run_id, "stored": True, "summary_id": saved.get("id")}
    except Exception as exc:  # noqa: BLE001 — a worker has nobody to raise to
        logger.exception("Requested summary for %s failed", run_id)
        return {"run_id": run_id, "stored": False, "error": str(exc)[:300]}


async def answer_aside_in_background(
    db: Database, thread_id: str, user_message: str, model: Optional[str] = None,
) -> Dict[str, Any]:
    """The worker's job: generate the reply to the thread's pending question and store it.

    The question is already stored (by the API, before dispatch). The reply is stored as the target's
    message; a failure is stored as an `error` message, so the browser polling the thread stops
    waiting and says what happened. Never raises.
    """
    thread = await db.get_thread(thread_id)
    if not thread:
        return {"thread_id": thread_id, "stored": False, "error": "thread not found"}
    try:
        run = await db.get_run(thread["run_id"])
        if not run:
            raise ValueError("the conversation this aside belongs to no longer exists")
        conversation = await _load_conversation(db, run)
        history = await db.get_thread_messages(thread_id)
        # The question being answered is the last message; it is the user turn, not history.
        if history and history[-1].get("role") == "user":
            history = history[:-1]
        reply = await _aside_reply(
            run, thread, user_message, conversation, run.get("topic", ""), history,
            resolve_model(run, model, role="aside"), max_tokens=analysis.ASIDE_BACKGROUND_MAX_TOKENS, db=db,
        )
        await db.add_thread_message(
            thread_id=thread_id, role="target", speaker=reply["speaker"], content=reply["content"],
            tokens_in=reply["tokens_in"], tokens_out=reply["tokens_out"], cost_usd=reply["cost_usd"],
        )
        return {"thread_id": thread_id, "stored": True, "cost_usd": reply["cost_usd"]}
    except Exception as exc:  # noqa: BLE001 — recorded in the thread, where the user is looking
        logger.warning("Aside reply for thread %s failed: %s", thread_id, exc)
        await db.add_thread_message(
            thread_id=thread_id, role="error", speaker="system",
            content=f"The reply could not be generated ({type(exc).__name__}). Your question is kept above; ask again to retry.",
        )
        return {"thread_id": thread_id, "stored": False, "error": str(exc)[:300]}
