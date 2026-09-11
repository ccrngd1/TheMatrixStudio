# SPDX-License-Identifier: Apache-2.0
"""Phase 5: one slice of a run, so Step Functions can own the turn loop.

A run cannot execute on AWS today. `POST /api/runs` returns 201, logs `Starting
simulation`, and the sandbox freezes when the handler returns — Lambda stops
scheduling the event loop the moment the response goes out, so an
`asyncio.create_task` background run dies in milliseconds. `docs/AWS-IMPLEMENTATION-PLAN.md`
records the measurement and cancels Phase 4 on the strength of it. This module is
what replaces it.

The unit is a **slice**: load the run's state as of turn N, generate up to
`turn_budget` turns, persist as the engine already does, and return a small dict the
state machine can branch on. `turn_budget=1` is what ships, so a slice is a turn.

## Why the state is reloaded rather than passed along

A Step Functions state's input/output is capped at **256 KB**. Snapshot bodies here
are mean 45 KB and max 2.2 MB, measured over 619 real snapshots
(`docs/PHASE2-STORAGE-KEY-DESIGN.md` §3 — one already exceeds DynamoDB's 400 KB item
limit). Passing engine state through the execution payload therefore works for short
conversations and fails for exactly the long ones this phase exists to enable.

So the payload carries identifiers and counters only, and every slice loads its own
state. `next_input()` returns that payload and nothing larger can leak into it,
because it is built field by field rather than by copying the slice's result.

## Why the snapshot is the fast path and replay is the fallback

Phase 2a chose a full snapshot per turn over deltas precisely so that
"reconstruction is O(1) (load one row)". A slice uses `get_snapshot(turn=N)`. Replay
via `reconstruct_at_turn` is the fallback for a run with no snapshot at N — an
imported run, or one interrupted before its first checkpoint — and it is O(N), which
is why it is not the default.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from matrix_studio.state import (
    AgentState,
    CognitionConfig,
    PersonaConfig,
    RetrievalConfig,
    SimSnapshot,
)
from matrix_studio.storage import Database

logger = logging.getLogger(__name__)

# Statuses from which no further slice is run. `running` and `pending` are the only
# non-terminal ones, so this is defined as the complement rather than listed twice.
TERMINAL_STATUSES = frozenset(
    {"complete", "failed", "stopped", "capped", "interrupted"}
)

# Default turns per slice. One, because per-turn granularity is what buys the things
# §6 wants from an orchestrator: `Retry` around a single Bedrock call rather than a
# batch, a uniform ~10 s invocation with no risk of a 15-minute timeout mid-run, and
# a stop that lands one turn after it is asked for rather than one batch.
DEFAULT_TURN_BUDGET = 1


def _config(run: Dict[str, Any]) -> Dict[str, Any]:
    raw = run.get("config_json")
    if not raw:
        return {}
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _cast(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        cast = json.loads(run["cast_json"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return []
    return cast if isinstance(cast, list) else []


async def load_state(db: Database, run: Dict[str, Any], turn: int):
    """Engine state as of ``turn``: ``(topic, agents, conversation, threads, ledger)``.

    Snapshot first (O(1)); replay second (O(turn)). The two are not equivalent and
    the order matters: the snapshot is what the previous slice wrote, so using it
    keeps a slice's cost independent of how long the conversation already is. Replay
    exists for the cases that have no snapshot to load, and reading the log is the
    only thing that always works.
    """
    from matrix_studio.branching import reconstruct_at_turn
    from matrix_studio.personas import parse_structured

    snap = await db.get_snapshot(run["id"], turn=turn)
    if snap is not None:
        # A snapshot's agents carry accumulated cost and memory, but NOT the persona
        # text and goals in every historical case, so the cast is re-applied over the
        # top. Losing a persona would not raise — it would quietly generate turns for
        # a blank character.
        agents: Dict[str, AgentState] = dict(snap.agents)
        for persona in _cast(run):
            existing = agents.get(persona["name"])
            if existing is None:
                agents[persona["name"]] = AgentState(
                    name=persona["name"],
                    persona=persona.get("persona", ""),
                    goals=persona.get("goals", []),
                    structured=parse_structured(persona.get("structured")),
                )
                continue
            if not existing.persona:
                existing.persona = persona.get("persona", "")
            if not existing.goals:
                existing.goals = persona.get("goals", [])
            if existing.structured is None:
                existing.structured = parse_structured(persona.get("structured"))
        return (
            snap.topic or run.get("topic", ""),
            agents,
            list(snap.conversation),
            list(snap.pending_threads),
            [list(p) for p in snap.firsthand_citations],
        )

    logger.info(
        "No snapshot for run %s at turn %d; replaying the event log instead",
        run["id"], turn,
    )
    return await reconstruct_at_turn(db, run, turn)


async def execute_slice(
    db: Database,
    run_id: str,
    *,
    turn: Optional[int] = None,
    turn_budget: int = DEFAULT_TURN_BUDGET,
    on_event: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Generate up to ``turn_budget`` turns of ``run_id``, starting after ``turn``.

    ``db`` must already be bound to the run's owner (`for_owner`). This function does
    not bind, on purpose: binding here would mean deciding a tenant deep inside the
    engine, and the whole point of §3 is that the tenant is named once, where the
    identity arrives.

    Returns the state machine's next payload — see `next_input`. Never raises for a
    run-level failure: the engine records `sim.failed` and a `failed` status, and this
    returns that status, because a state machine needs a verdict rather than an
    exception to route on.
    """
    from matrix_studio.engine.simulator import resume_simulation

    run = await db.get_run(run_id)
    if run is None:
        raise ValueError(f"run {run_id!r} does not exist")

    status = run.get("status")
    if status in TERMINAL_STATUSES:
        # Not an error. A retry after a slice that already finished the run, or a stop
        # that landed between slices, both arrive here; saying so lets the machine end
        # cleanly instead of generating past a terminal event.
        logger.info("Run %s is already %r; no slice to run", run_id, status)
        return _payload(run_id, status, run, turn or 0)

    cfg = _config(run)
    max_messages = int(cfg.get("max_messages") or 0)
    if max_messages <= 0:
        from matrix_studio.settings import get_settings
        max_messages = get_settings().max_messages

    # Where to resume from. The caller's `turn` is a hint the machine carries forward;
    # the checkpoint is the truth, because it is what was actually persisted. Trusting
    # the hint would let a retry after a partially-written turn resume from a turn that
    # does not exist.
    checkpoint = await db.last_checkpoint_turn(run_id)
    start_turn = checkpoint if checkpoint is not None else 0
    if turn is not None and turn != start_turn:
        logger.info(
            "Run %s: slice input said turn %d, last checkpoint is %d; using the "
            "checkpoint", run_id, turn, start_turn,
        )

    # Trim anything past the checkpoint before generating. This is what makes a slice
    # idempotent, and therefore what makes `Retry` safe: a slice that died after
    # appending events but before checkpointing leaves a partial turn behind, and a
    # retry without this would append a SECOND copy of that turn under fresh seqs.
    # The event log is the source of truth `reconstruct_at_turn` replays, so two
    # copies of one turn is corruption rather than clutter.
    removed = await db.truncate_after_turn(run_id, start_turn)
    if removed:
        logger.info(
            "Run %s: trimmed %d event(s) past checkpoint turn %d before generating",
            run_id, removed, start_turn,
        )

    if start_turn >= max_messages:
        # Budget already met — the previous slice was the last one. Finalising here
        # rather than generating a turn nobody asked for.
        return await finalise(db, run_id, status="complete")

    topic, agents, conversation, threads, ledger = await load_state(
        db, run, start_turn
    )
    if not agents:
        raise ValueError(
            f"run {run_id!r} has no agents at turn {start_turn} — its cast is empty "
            "and its event log has no responses, so there is nobody to generate"
        )

    if status == "pending":
        await db.update_run_status(run_id, "running")

    start_seq = await db.max_seq(run_id) + 1
    result = await resume_simulation(
        run_id=run_id,
        topic=topic or run.get("topic", ""),
        agents=agents,
        conversation=conversation,
        from_turn=start_turn,
        start_seq=start_seq,
        max_messages=max_messages,
        db=db,
        on_event=on_event,
        model=cfg.get("model") or None,
        cognition=CognitionConfig.from_config(cfg),
        pending_threads=threads,
        retrieval=RetrievalConfig.from_config(cfg),
        personas=PersonaConfig.from_config(cfg),
        firsthand_citations=ledger,
        # A stop is a DynamoDB flag read once per slice, not an in-memory set. The
        # engine still polls it AFTER each turn is persisted, so the in-flight turn
        # always finishes — the contract does not move, only where the flag lives.
        should_stop=_stop_predicate(run),
        turn_budget=turn_budget,
    )

    fresh = await db.get_run(run_id) or run
    return _payload(
        run_id, result.get("status", "running"), fresh,
        int(result.get("total_turns") or start_turn),
        total_cost_usd=float(result.get("total_cost_usd") or 0.0),
        max_messages=max_messages,
    )


def _stop_predicate(run: Dict[str, Any]) -> Callable[[], bool]:
    """Whether a stop was requested, as of the run row this slice already read.

    Read once per slice rather than per turn, which is exact at `turn_budget=1` and
    at most one turn stale above it. Re-reading per turn would add a DynamoDB call to
    the hot path to shorten a window that the default budget closes anyway.
    """
    requested = bool(run.get("stop_requested"))
    return lambda: requested


def _payload(
    run_id: str,
    status: str,
    run: Dict[str, Any],
    turn: int,
    *,
    total_cost_usd: float = 0.0,
    max_messages: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "owner_sub": run.get("owner_sub"),
        "status": status,
        "turn": turn,
        "max_messages": max_messages if max_messages is not None else int(
            _config(run).get("max_messages") or 0
        ),
        "total_cost_usd": total_cost_usd,
        "stop_requested": bool(run.get("stop_requested")),
        "done": status in TERMINAL_STATUSES,
    }


def next_input(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The execution payload for the next slice — small, and provably so.

    Built field by field rather than by copying and deleting, because the failure this
    guards against is a large value *arriving* rather than a known one staying: a
    `conversation` or `agents` key added to a slice's result later would ride along
    unnoticed under a copy-and-strip, and the 256 KB ceiling is only reached by the
    long conversations that are hardest to test.
    """
    return {
        "run_id": payload["run_id"],
        "owner_sub": payload.get("owner_sub"),
        "turn": int(payload.get("turn") or 0),
        "max_messages": int(payload.get("max_messages") or 0),
        "total_cost_usd": float(payload.get("total_cost_usd") or 0.0),
    }


async def finalise(
    db: Database, run_id: str, *, status: str = "complete"
) -> Dict[str, Any]:
    """Write the terminal status and snapshot for a run whose loop has ended.

    Separate from the slice because a run can reach its budget without a slice being
    the thing that noticed — a retry, a resumed execution, or a budget already met.
    Idempotent: a run already in a terminal status is left alone rather than being
    re-completed, so a second call cannot move `completed_at`.
    """
    run = await db.get_run(run_id)
    if run is None:
        raise ValueError(f"run {run_id!r} does not exist")
    if run.get("status") in TERMINAL_STATUSES:
        return _payload(run_id, str(run.get("status")), run, 0)

    if run.get("stop_requested") and status == "complete":
        # A stop that arrived on the last turn should still read as a stop: the run
        # did end because someone asked, and "complete" would erase that.
        status = "stopped"

    completion_time = int(time.time())
    turn = await db.last_checkpoint_turn(run_id) or 0
    snap = await db.get_snapshot(run_id, turn=turn)
    if snap is not None:
        await db.save_snapshot(
            SimSnapshot(
                run_id=run_id,
                turn=turn,
                topic=snap.topic,
                agents=snap.agents,
                conversation=snap.conversation,
                pending_threads=snap.pending_threads,
                firsthand_citations=snap.firsthand_citations,
                status=status,
                created_at=completion_time,
                completed_at=completion_time,
                total_turns=turn,
            )
        )
    await db.update_run_status(run_id, status, completion_time)
    fresh = await db.get_run(run_id) or run
    return _payload(run_id, status, fresh, turn)
