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

import asyncio
import json
import logging
import os
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


def turn_loop_arn() -> str:
    """The state machine's ARN, or "" when there is none.

    Empty is not a misconfiguration — it is the local case. `uvicorn` is one process
    that stays alive, so a background asyncio task there genuinely runs to completion
    and needs no orchestrator. The presence of this variable is what selects between
    the two, so a laptop keeps the fast path and Lambda gets the only one that works.
    """
    return os.environ.get("TURN_LOOP_ARN", "")


async def start_execution(
    run_id: str, owner_sub: str, *, max_messages: int
) -> Optional[str]:
    """Start the turn loop for a run. Returns the execution ARN, or None if disabled.

    The name is derived from the run id so a second `StartExecution` for the same run
    is rejected by Step Functions rather than starting a rival execution. That matters
    more than it sounds: two executions on one run id would both append to the same
    event log and both write snapshots at the same turns, which is corruption rather
    than duplication — and it is exactly what a retried API call would cause.

    ``ExecutionAlreadyExists`` is therefore a success, not an error: it means the run
    is already executing.
    """
    arn = turn_loop_arn()
    if not arn:
        return None

    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("stepfunctions", region_name=os.environ.get("AWS_REGION"))
    payload = json.dumps({
        "run_id": run_id,
        "owner_sub": owner_sub,
        "turn": 0,
        "max_messages": int(max_messages),
        "total_cost_usd": 0.0,
    })
    # Step Functions allows [0-9A-Za-z-_] and 80 characters. A run id is a uuid4 hex
    # with hyphens (36), so `run-` plus it fits with room to spare.
    name = f"run-{run_id}"[:80]

    def _start() -> str:
        return client.start_execution(
            stateMachineArn=arn, name=name, input=payload
        )["executionArn"]

    try:
        return await asyncio.to_thread(_start)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ExecutionAlreadyExists":
            logger.info("Run %s already has an execution; not starting another", run_id)
            return None
        raise


def _emitter(db: Database, run_id: str, start_seq: int = 0):
    """An ``(emit, next_seq)`` pair that persists events, with no live callback.

    The engine's own `_emit` closures also push to an `on_event` subscriber. There is
    nobody to push to here: the WebSocket broker lives in the API process and a slice
    runs in a different one. The UI polls `events?after_seq=` instead, which is what
    `docs/PHASE5-ORCHESTRATION-DESIGN.md` §8 settles.
    """
    counter = {"seq": start_seq}

    def next_seq() -> int:
        s = counter["seq"]
        counter["seq"] += 1
        return s

    async def emit(turn, seq, event_type, payload, agent_name=None) -> None:
        await db.append_event(
            run_id=run_id, turn=turn, seq=seq, event_type=event_type,
            agent_name=agent_name, payload=payload,
        )

    return emit, next_seq


async def prepare_run(db: Database, run_id: str) -> Dict[str, Any]:
    """The turn-0 work: `sim.started`, avatars, document ingest, embeddings.

    Idempotent by inspection rather than by hope: if the run already has a
    `sim.started` event this returns without doing anything. A `Retry` on the state
    that calls it would otherwise emit a second `sim.started`, re-ingest every
    document and pay to re-embed the whole corpus — the last of which is the only
    genuinely expensive mistake available in this phase.
    """
    from matrix_studio.engine.simulator import begin_run

    run = await db.get_run(run_id)
    if run is None:
        raise ValueError(f"run {run_id!r} does not exist")

    existing = await db.get_events(run_id, from_turn=0, to_turn=0)
    if any(e["event_type"] == "sim.started" for e in existing):
        logger.info("Run %s is already started; prepare is a no-op", run_id)
        return _payload(run_id, str(run.get("status") or "running"), run, 0)

    cfg = _config(run)
    from matrix_studio.settings import get_settings
    settings = get_settings()

    await db.update_run_status(run_id, "running")
    emit, next_seq = _emitter(db, run_id, start_seq=await db.max_seq(run_id) + 1)
    await begin_run(
        run_id=run_id,
        topic=run.get("topic", ""),
        cast=_cast(run),
        db=db,
        emit=emit,
        next_seq=next_seq,
        generate_avatars_flag=bool(
            cfg.get("generate_avatars", settings.enable_avatars)
        ),
        personas_cfg=PersonaConfig.from_config(cfg),
        retrieval=RetrievalConfig.from_config(cfg),
    )
    fresh = await db.get_run(run_id) or run
    return _payload(run_id, "running", fresh, 0)


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
    status = str(result.get("status", "running"))
    turn_now = int(result.get("total_turns") or start_turn)

    # Re-read the stop flag before letting the machine loop.
    #
    # The engine's `should_stop` closes over the run row this slice read at its START,
    # so a stop requested WHILE the turn was generating is invisible to it — the slice
    # returns `running`, the machine loops, and one more turn is generated. Measured
    # on the deployed stack: asking after turn 2 produced a log ending at turn 4, when
    # the documented contract is that the turn in flight finishes and "only the NEXT
    # one is prevented".
    #
    # One `GetItem` per slice closes that window exactly. It is the cheapest thing in
    # a turn by orders of magnitude — a turn is a Bedrock call — and it buys back the
    # semantics the local path has always had, where the predicate is a live closure
    # over an in-memory set.
    if status == "running" and fresh.get("stop_requested"):
        logger.info(
            "Run %s: stop requested during turn %d; ending here rather than "
            "generating another", run_id, turn_now,
        )
        return await stop_now(db, run_id, turn=turn_now)

    return _payload(
        run_id, status, fresh, turn_now,
        total_cost_usd=float(result.get("total_cost_usd") or 0.0),
        max_messages=max_messages,
    )


async def stop_now(db: Database, run_id: str, *, turn: int) -> Dict[str, Any]:
    """End a run as `stopped` at ``turn``, with the event the log needs.

    `finalise` writes the status and a snapshot but no event, and a run whose log has
    no terminal marker replays as one that is still going — `reconstruct_at_turn` and
    every export read the log, not the row. So the event is emitted here, matching
    what `_run_turns` writes when it notices the stop itself.
    """
    snap = await db.get_snapshot(run_id, turn=turn)
    total_cost = (
        sum(a.total_cost_usd for a in snap.agents.values()) if snap else 0.0
    )
    emit, next_seq = _emitter(db, run_id, start_seq=await db.max_seq(run_id) + 1)
    await emit(
        turn=turn,
        seq=next_seq(),
        event_type="sim.stopped",
        payload={
            "total_turns": turn,
            "message_count": len(snap.conversation) if snap else 0,
            "total_cost_usd": total_cost,
        },
    )
    completion_time = int(time.time())
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
                status="stopped",
                created_at=completion_time,
                completed_at=completion_time,
                total_turns=turn,
            )
        )
    await db.update_run_status(run_id, "stopped", completion_time)
    fresh = await db.get_run(run_id) or {}
    return _payload(run_id, "stopped", fresh, turn, total_cost_usd=total_cost)


def _stop_predicate(run: Dict[str, Any]) -> Callable[[], bool]:
    """Whether a stop was requested, as of the run row this slice already read.

    Deliberately stale, and the staleness is handled elsewhere. The engine's
    `should_stop` is a synchronous callable — it cannot await a DynamoDB read — so this
    can only report what was known when the slice started. A stop requested *during*
    the turn is caught by the re-read at the end of `execute_slice`, which is where an
    `await` is available.

    An earlier version of this docstring claimed the arrangement was "exact at
    `turn_budget=1`". It was not, and the deployment said so: a stop asked for during
    turn 3 produced a log ending at turn 4.
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
        # The common case, and not a no-op: `_run_turns` writes the terminal status
        # itself on the last slice, so by the time the machine's Finalise state runs
        # the run is already `complete`. The summary still has to happen, and this is
        # the only place left to do it — `RunManager._runner` used to, in a background
        # task that Lambda no longer runs.
        await _summarise_once(db, run_id, str(run.get("status")))
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
    await _summarise_once(db, run_id, status)
    fresh = await db.get_run(run_id) or run
    return _payload(run_id, status, fresh, turn)


async def _summarise_once(db: Database, run_id: str, status: str) -> None:
    """Auto-generate the run's summary, at most once, and never fatally.

    Guarded on a summary already existing because `maybe_autogenerate_summary` is not
    idempotent — it stores a new row every call — and a `Retry` on the Finalise state
    would otherwise pay for a second LLM summary of the same conversation and leave
    two, with the reader given no way to tell which is current.

    Only for a `complete` run. Summarising a stopped or capped one would describe a
    conversation that was cut off as though it had finished.
    """
    if status != "complete":
        return
    try:
        if await db.get_summaries(run_id):
            logger.info("Run %s already has a summary; not generating another", run_id)
            return
    except Exception:  # noqa: BLE001
        # If the check itself fails, skip rather than risk the duplicate.
        logger.exception("Could not check for an existing summary on run %s", run_id)
        return
    from matrix_studio.service import maybe_autogenerate_summary

    await maybe_autogenerate_summary(db, run_id)
