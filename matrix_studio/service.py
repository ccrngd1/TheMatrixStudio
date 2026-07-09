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

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from matrix_studio import analysis
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


def resolve_model(run: Dict[str, Any], override: Optional[str] = None) -> Optional[str]:
    """
    Resolve the model for an analysis call: an explicit override wins, then the
    run's configured model, else None (analysis falls back to the current
    settings default).

    Exception for IMPORTED runs: their stored model string comes from a legacy
    system and may be end-of-life (e.g. the bridge-kibble fixture records an EOL
    Haiku). A summary/aside is NEW analysis we compute now — not a replay — so
    for imported runs we prefer the current settings default rather than
    forwarding a possibly-dead model. We never substitute a specific EOL model;
    we only decline to reuse a stale imported one.
    """
    if override:
        return override
    cfg = _run_config(run)
    if cfg.get("imported"):
        return None  # use the current settings default for fresh analysis
    model = cfg.get("model")
    return model or None


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
) -> Dict[str, Any]:
    """
    Generate a structured summary for a completed run and persist it as a
    'generated' summary (never overwriting an imported original). Returns the
    stored summary dict (with `parsed` flag). Never raises for LLM issues —
    ``analysis.generate_summary`` degrades gracefully.
    """
    conversation = await _load_conversation(db, run)
    topic = run.get("topic", "")
    result = await analysis.generate_summary(
        conversation=conversation,
        topic=topic,
        fields=fields,
        focus=focus,
        model=resolve_model(run, model),
    )
    saved = await db.save_summary(
        run_id=run["id"],
        payload=result["payload"],
        kind="generated",
        tokens_in=result["tokens_in"],
        tokens_out=result["tokens_out"],
        cost_usd=result["cost_usd"],
    )
    saved["parsed"] = result["parsed"]
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
            db, run, fields=cfg["fields"], focus=cfg["focus"]
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
    if target not in ("analyst", "persona", "room"):
        raise ValueError(f"Unknown target: {target}")
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
        persona_name=persona_name if target == "persona" else None,
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
    resolved_model = resolve_model(run, model)

    # Persist the user turn first so it is part of the history for THIS call's
    # follow-ups (but not passed as the current user_message again).
    history = await db.get_thread_messages(thread["id"])
    await db.add_thread_message(
        thread_id=thread["id"],
        role="user",
        speaker="user",
        content=user_message,
    )

    target = thread["target"]
    if target == "analyst":
        reply = await analysis.analyst_reply(
            user_message=user_message,
            conversation=conversation,
            topic=topic,
            thread_history=history,
            model=resolved_model,
        )
    elif target == "persona":
        cast = _load_cast(run)
        persona = next(
            (c for c in cast if c.get("name") == thread["persona_name"]), None
        )
        if persona is None:
            raise ValueError("Persona no longer present in run cast")
        reply = await analysis.persona_reply(
            user_message=user_message,
            persona_name=persona["name"],
            persona_text=persona.get("persona", ""),
            conversation=conversation,
            topic=topic,
            thread_history=history,
            model=resolved_model,
        )
    elif target == "room":
        cast = _load_cast(run)
        reply = await analysis.room_reply(
            user_message=user_message,
            cast=cast,
            conversation=conversation,
            topic=topic,
            thread_history=history,
            model=resolved_model,
        )
    else:  # pragma: no cover - guarded at creation
        raise ValueError(f"Unknown target: {target}")

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
