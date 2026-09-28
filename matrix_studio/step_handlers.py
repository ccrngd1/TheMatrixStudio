# SPDX-License-Identifier: Apache-2.0
"""Phase 5: Lambda entry points for the Step Functions states.

Handlers — `research`, `prepare`, `turn`, `finalise` — each a thin adapter over
`matrix_studio.orchestration`. Thin on purpose: everything they do is testable
in-process against `moto`, and a handler that held logic would be the one part of the
turn loop only a deployment could exercise.

## The event shape is the contract

Every handler takes and returns the same small dict, so the state machine's
input/output paths need no per-state translation:

    {"run_id": ..., "owner_sub": ..., "turn": N, "max_messages": N,
     "total_cost_usd": F, "status": ..., "stop_requested": bool, "done": bool}

`orchestration.next_input` narrows it back down between states. Nothing engine-sized
travels in it — a Step Functions state's I/O is capped at 256 KB and snapshots here
reach 2.2 MB, so state is reloaded per slice rather than passed along.

## `owner_sub` arrives in the event and is not negotiable

Every storage call in this system goes through credentials scoped to one tenant
(§3), and `for_owner` is where they attach. These handlers bind from the event's
`owner_sub` and never fall back to a default: a missing owner raises rather than
quietly writing one person's conversation into the shared local partition.

That is also why the state machine does not read DynamoDB directly. A native SDK
integration would use the machine's role, which is not per-tenant, so
`dynamodb:LeadingKeys` could not constrain it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, Coroutine, Dict, Optional

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# One store per sandbox, built lazily and reused across invocations. Connecting per
# invocation would pay boto3 client construction on every turn of every run.
_store = None


async def _bound(owner_sub: str):
    """The storage layer, connected once per sandbox and bound to this run's owner."""
    global _store
    from matrix_studio.storage import Database

    if _store is None:
        store = Database()
        await store.connect()
        _store = store
    return _store.for_owner(owner_sub)


def _owner(event: Dict[str, Any]) -> str:
    owner = (event or {}).get("owner_sub")
    if not owner:
        raise ValueError(
            "the execution input has no owner_sub, so there is no tenant to bind to. "
            "It is set by POST /api/runs from the caller's verified JWT claims; a "
            "default here would attribute somebody's conversation to a shared bucket."
        )
    return str(owner)


def _run(coro: Coroutine[Any, Any, Dict[str, Any]]) -> Dict[str, Any]:
    """Drive one coroutine to completion inside a Lambda invocation.

    `asyncio.run` rather than a reused loop: the storage layer's clients are sync
    boto3 behind `asyncio.to_thread`, so nothing here is bound to a particular loop,
    and a fresh loop per invocation cannot inherit a task left pending by a previous
    one. That inheritance is what killed Phase 4 from the other direction — Lambda
    freezes the sandbox when the handler returns, so a pending task is not merely
    idle, it is abandoned mid-flight.
    """
    return asyncio.run(coro)


def _handler(
    fn: Callable[..., Coroutine[Any, Any, Dict[str, Any]]]
) -> Callable[[Dict[str, Any], Any], Dict[str, Any]]:
    def wrapper(event: Dict[str, Any], _context: Any = None) -> Dict[str, Any]:
        logger.info("%s: %s", fn.__name__, {
            k: event.get(k) for k in ("run_id", "turn", "status")
        })
        out = _run(fn(event))
        logger.info("%s -> status=%s turn=%s done=%s", fn.__name__,
                    out.get("status"), out.get("turn"), out.get("done"))
        return out
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


async def _prepare(event: Dict[str, Any]) -> Dict[str, Any]:
    from matrix_studio import orchestration

    db = await _bound(_owner(event))
    # `mode` selects fresh / branch / resume; `extra` carries the few identifiers the
    # latter two need (parent_run_id, from_turn, mutation). Absent means fresh, so an
    # execution started before this existed still runs.
    return await orchestration.prepare(
        db,
        str(event["run_id"]),
        mode=str(event.get("mode") or "fresh"),
        **(event.get("extra") or {}),
    )


async def _research(event: Dict[str, Any]) -> Dict[str, Any]:
    """The Research state: search the open web before turn 1. `PERSONA-RESEARCH.md` §5.

    A state rather than a background task, for the reason this file's header already gives about
    `prepare`: Lambda freezes the sandbox when the handler returns, so an `asyncio` task started
    from the API dies. That mistake is on the record three times in this project.

    **Returns the event it was given, with a small `research` record added.** The next state is
    `Prepare`, which needs `run_id`, `owner_sub`, `mode` and `extra` — so this passes its input
    through rather than returning a result of its own. Anything else would make the Research state
    a translation layer between two states that already agree on a shape.

    Invoked for EVERY run and returns in milliseconds when research is off. Deciding it here rather
    than in a `Choice` over the execution input keeps the run row authoritative; see
    `research_state`'s module docstring for why a second copy of that decision is the dangerous
    kind of duplication.
    """
    from matrix_studio import research_state

    owner = _owner(event)
    db = await _bound(owner)

    # Two callers, one worker. An ENSEMBLE is invoked asynchronously by `POST /api/ensembles`
    # rather than being a state in a machine, because §6 has the pass happen BEFORE any member
    # exists — so there is no member execution to host it. It researches once and then fans out.
    if event.get("ensemble_id") and not event.get("run_id"):
        return await _research_ensemble(db, str(event["ensemble_id"]), owner)

    record = await research_state.run_research(
        db, str(event["run_id"]), owner_sub=owner, mode=str(event.get("mode") or "fresh"),
    )
    return {**event, "research": {
        # Deliberately not the whole record. A state's I/O is capped at 256 KB and everything
        # downstream reads the run row anyway; what travels here is for the execution history.
        "status": record.get("status"),
        "cost_usd": record.get("cost_usd"),
    }}


async def _research_ensemble(db: Any, ensemble_id: str, owner: str) -> Dict[str, Any]:
    """Research an ensemble once, then create its members. `PERSONA-RESEARCH.md` §6.

    **The fan-out happens whatever the research did.** Research is additive (§5.2) — a search
    outage is not a reason to lose five conversations somebody asked for — and an ensemble left at
    `researching` with no members is the worst of the available failures: the parent lists run ids
    that do not exist, and nothing is working on them.

    So the record is written first and the fan-out is attempted regardless. If the fan-out itself
    raises, the parent is marked `failed` rather than left mid-flight, because a parent stuck at
    `researching` is indistinguishable from one still being worked on.
    """
    from matrix_studio import research_state
    from matrix_studio.api.manager import RunManager

    record = await research_state.research_ensemble(db, ensemble_id, owner_sub=owner)

    groups: Any = None
    try:
        # The creator's verified groups, recorded on the parent's members when they are created.
        # Read from the ensemble row rather than invented, and absent is correct for a deployment
        # with no group grants.
        parent = await db.get_ensemble(ensemble_id)
        raw = (parent or {}).get("groups_json")
        groups = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        groups = None

    try:
        # `RunManager` over the UNBOUND store: it binds per owner internally, and
        # `fan_out_ensemble` reads the parent from that binding.
        from matrix_studio.storage import Database

        assert _store is not None and isinstance(_store, Database)
        out = await RunManager(_store).fan_out_ensemble(
            ensemble_id, owner_sub=owner, groups=groups,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ensemble %s could not be fanned out after research", ensemble_id)
        await db.update_ensemble(
            ensemble_id, status="failed", owner_sub=owner,
            report_error=f"fan-out failed after research: {exc}"[:500],
        )
        return {"ensemble_id": ensemble_id, "research": record.get("status"),
                "fanned_out": 0, "error": str(exc)}

    return {
        "ensemble_id": ensemble_id,
        "research": record.get("status"),
        "cost_usd": record.get("cost_usd"),
        "fanned_out": len(out.get("members") or []),
        "failed": len(out.get("failed") or []),
    }


async def _turn(event: Dict[str, Any]) -> Dict[str, Any]:
    from matrix_studio import orchestration

    db = await _bound(_owner(event))
    budget = int(
        event.get("turn_budget")
        or os.environ.get("TURN_BUDGET")
        or orchestration.DEFAULT_TURN_BUDGET
    )
    out = await orchestration.execute_slice(
        db,
        str(event["run_id"]),
        turn=event.get("turn"),
        turn_budget=budget,
        # The run's cumulative cost as the machine last saw it. The slice charges the
        # DELTA against the owner's monthly total (§7), so passing 0 here would charge
        # the run's whole cost on every turn — a 30-turn run would bill ~465× its cost.
        spent_before=float(event.get("total_cost_usd") or 0.0),
    )
    # The machine's next input is narrowed here rather than in the state machine's
    # ResultSelector, so the one place that decides what crosses a state boundary is
    # the one place with a test for it.
    return {**orchestration.next_input(out), "status": out["status"], "done": out["done"]}


async def _finalise(event: Dict[str, Any]) -> Dict[str, Any]:
    from matrix_studio import orchestration

    db = await _bound(_owner(event))
    return await orchestration.finalise(
        db, str(event["run_id"]), status=str(event.get("status") or "complete")
    )


async def _ensemble_report(event: Dict[str, Any]) -> Dict[str, Any]:
    """Build and store one ensemble's report. Its own function, for its own clock.

    This used to run inside `finalise`, and the report outgrew it: measured end to end at 12–19
    minutes against a Lambda hard ceiling of 15. Not because the work is large — the clustering
    reply is about 1,100 tokens — but because Sonnet 5 spends roughly 29,000 output tokens
    reasoning to produce it, and that takes minutes per call.

    Moving it here buys two things. A clock of its own, so a slow report cannot time out the
    state that writes a run's terminal status. And permission to run the independent calls
    CONCURRENTLY: the extractions were sequential only because they used to share an invocation
    with a member's last turn, and that reason is gone.

    Invoked asynchronously and never awaited, so nothing about a run depends on it. Failures are
    recorded on the ensemble row as `report_error`; `generate` does not raise.
    """
    from matrix_studio import ensemble_reporting

    db = await _bound(_owner(event))
    ensemble_id = str(event["ensemble_id"])
    report = await ensemble_reporting.generate(
        db, ensemble_id, force=bool(event.get("force")),
    )
    return {
        "ensemble_id": ensemble_id,
        # `generated: false` covers both "somebody else holds the claim" and "it was refused",
        # which the row distinguishes. The async invoker reads neither — this is for a log.
        "generated": report is not None,
        "cost_usd": (report or {}).get("cost_usd"),
    }


async def _aside(event: Dict[str, Any]) -> Dict[str, Any]:
    """Generate and store one aside reply (`service.answer_aside_in_background`).

    Invoked asynchronously by `POST /api/threads/{id}/messages`, which has already stored the question
    and answered 202 — the reply used to be generated inside that request and ran into its 30 s limit.
    The browser polls the thread until the reply, or a recorded failure, appears.
    """
    from matrix_studio import service

    db = await _bound(_owner(event))
    if event.get("kind") == "summary":
        # A requested summary (`POST /api/runs/{ref}/summary`): the same worker, for the same reason.
        return await service.summarise_in_background(
            db, str(event["run_id"]), fields=event.get("fields"), focus=event.get("focus"),
            model=event.get("model"), instructions=event.get("instructions"),
        )
    return await service.answer_aside_in_background(
        db, str(event["thread_id"]), str(event.get("user_message") or ""), event.get("model"),
    )


research = _handler(_research)
prepare = _handler(_prepare)
turn = _handler(_turn)
finalise = _handler(_finalise)
ensemble_report = _handler(_ensemble_report)
aside = _handler(_aside)
