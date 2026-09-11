# SPDX-License-Identifier: Apache-2.0
"""Phase 5: Lambda entry points for the Step Functions states.

Three handlers — `prepare`, `turn`, `finalise` — each a thin adapter over
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
    return await orchestration.prepare_run(db, str(event["run_id"]))


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


prepare = _handler(_prepare)
turn = _handler(_turn)
finalise = _handler(_finalise)
