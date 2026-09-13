#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Start a conversation definition on the DEPLOYED stack, and watch it run.

## Why this exists rather than `curl POST /api/runs`

`POST /api/runs` needs a Cognito JWT, which needs somebody's password. A conversation
definition is a file an author edits, and the loop that matters — edit the JSON, run it,
read the transcript — should not go through a browser login.

So this takes the same path the API route takes, minus the HTTP and the JWT: the body is
validated by the route's own `CreateRunModel`, then handed to `RunManager.create_run`,
which resolves the name, writes the run row synchronously and starts the state machine.
Nothing here reimplements any of that, deliberately — a script with its own copy of
"create a run" is a script that drifts from the route it is standing in for, and the
divergence shows up as a run that behaves differently from one started in the UI.

The turns themselves execute in the deployed Lambdas under their own roles. This process
only writes the run row and calls `StartExecution`; it can exit at any point and the
conversation carries on.

## What it does not do

It does not check the caller may act as `--owner`. Ambient credentials for this account
already can write any partition, and the tenancy boundary this bypasses is enforced on
the *API* — see `scripts/verify_tenant_isolation.py` for the control that proves it. This
is an operator tool; the run appears in the UI as that user's own.

Usage:
    export AWS_REGION=us-east-1 TABLE_PREFIX=matrix-studio
    export DATA_BUCKET=... VECTOR_BUCKET=... VECTOR_INDEX=matrix-studio-chunks
    export TURN_LOOP_ARN=arn:aws:states:...:stateMachine:matrix-studio-turn-loop

    scripts/start_conversation.py data/sampleImport.json --owner SUB
    scripts/start_conversation.py data/sampleImport.json --owner SUB --max-messages 4
    scripts/start_conversation.py data/sampleImport.json --owner SUB --dry-run
    scripts/start_conversation.py --watch RUN_ID          # re-attach to a running one

Costs real money: every turn is a model call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix_studio.storage import Database  # noqa: E402


def _require_env() -> None:
    """Fail before doing anything if the account/table wiring is absent.

    A missing `TURN_LOOP_ARN` is the dangerous one: `start_execution` returns None for
    it, which is the legitimate *local* answer, so the run would be created, sit at
    `pending` for ever and report as started.
    """
    missing = [
        name for name in ("AWS_REGION", "DATA_BUCKET", "TURN_LOOP_ARN")
        if not os.environ.get(name)
    ]
    if missing:
        raise SystemExit(
            "Set these first (see the stack outputs): " + ", ".join(missing) + "\n"
            "TURN_LOOP_ARN especially: without it a run is created but never executes."
        )


def load_definition(path: str) -> Dict[str, Any]:
    """Read the file and validate it exactly as the API route would.

    Validating through `CreateRunModel` rather than by hand is the point: a definition
    that this accepts is one the UI would accept, and a typo'd key is refused here
    instead of becoming a field nothing reads.
    """
    from matrix_studio.api.app import CreateRunModel

    raw = json.loads(Path(path).read_text())
    if not raw.get("cast"):
        raise SystemExit(f"{path}: at least one persona is required")
    return CreateRunModel(**raw).model_dump(exclude_none=True)


def describe(request: Dict[str, Any]) -> None:
    from matrix_studio.models import ModelSet

    cfg = request.get("config") or {}
    cast = request.get("cast") or []
    cognition = (cfg.get("cognition") or {})
    print(f"  topic       {request['topic'][:100]}")
    print(f"  name        {request.get('name') or '(generated)'}")
    print(f"  cast        {len(cast)}: {', '.join(c['name'] for c in cast)}")
    print(f"  turns       {cfg.get('max_messages') or request.get('max_messages')}")
    print(f"  cognition   {'on' if cognition.get('enabled') else 'off'}"
          + (f", reflection every {cognition['reflection_every']}"
             if cognition.get("reflection_every") else ""))
    plan = ModelSet.from_config(cfg).as_dict()
    for role, model in sorted(plan.items()):
        print(f"  model:{role:<18} {model}")


async def watch(db: Database, run_id: str, *, timeout_s: int) -> int:
    """Print turns as they land, until the run reaches a terminal status.

    Polls the event log rather than the execution, because they answer different
    questions: the machine finishing says the states ran, and the log says a
    conversation happened.
    """
    deadline = time.time() + timeout_s
    seen = 0
    while time.time() < deadline:
        events = await db.get_events(run_id)
        for event in events[seen:]:
            kind = event["event_type"]
            if kind == "agent.response":
                # `payload` arrives as stored JSON, not a dict — the event log keeps the
                # raw string so a payload shape change cannot break a replay.
                payload = event.get("payload") or event.get("payload_json") or "{}"
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except ValueError:
                        payload = {"content": payload}
                text = payload.get("message") or payload.get("content") or ""
                print(f"  [{event['turn']:>3}] {event.get('agent_name')}: "
                      f"{' '.join(text.split())[:140]}")
            elif kind.startswith("sim.") and kind != "sim.started":
                print(f"  ── {kind}")
        seen = len(events)

        run = await db.get_run(run_id)
        status = (run or {}).get("status")
        if status in ("complete", "stopped", "capped", "failed", "interrupted"):
            # Cost is DERIVED from the log, not a counter on the run row — the row has
            # no such field, and reading one that isn't there reported $0.00 for a run
            # that had really spent money.
            stats = await db.get_run_stats(run_id)
            print(f"\n{status}: {stats['turn_count']} turns, {seen} events, "
                  f"${stats['total_cost_usd']:.4f}")
            return 0 if status == "complete" else 1
        await asyncio.sleep(6)
    print(f"\nStill running after {timeout_s}s — re-attach with --watch {run_id}")
    return 1


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("definition", nargs="?", help="a conversation definition JSON")
    parser.add_argument("--owner", help="the Cognito sub that will own the run")
    parser.add_argument("--groups", action="append", default=[],
                        help="Cognito groups to record on the run (KB grants use them)")
    parser.add_argument("--max-messages", type=int, default=None,
                        help="override the definition's turn budget")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and print the plan; create nothing")
    parser.add_argument("--watch", metavar="RUN_ID", default=None,
                        help="attach to a run already executing")
    parser.add_argument("--no-watch", action="store_true")
    parser.add_argument("--timeout", type=int, default=60 * 60)
    args = parser.parse_args()

    if not args.owner or not (args.definition or args.watch):
        # `--owner` is required even for `--watch`: every read below is
        # partition-scoped, and an unbound store raises rather than scanning.
        raise SystemExit("Pass --owner SUB, plus a definition or --watch RUN_ID.")
    if not args.dry_run:
        _require_env()

    if args.definition:
        request = load_definition(args.definition)
        if args.max_messages is not None:
            request.setdefault("config", {})["max_messages"] = args.max_messages
        print(f"\n{args.definition}")
        describe(request)
        if args.dry_run:
            print("\nDry run: nothing was created.")
            return 0

    db = Database(
        table_prefix=os.environ.get("TABLE_PREFIX", "matrix-studio"),
        bucket=os.environ["DATA_BUCKET"],
        region=os.environ["AWS_REGION"],
    )
    await db.connect()
    try:
        # Bound for every READ. `RunManager.create_run` binds its own store internally,
        # so it gets the unbound one — but a run row and its events live in the owner's
        # partition, and an unbound store refuses the call rather than scanning for it.
        owned = db.for_owner(args.owner)
        if args.watch:
            return await watch(owned, args.watch, timeout_s=args.timeout)

        from matrix_studio.api.manager import RunManager

        result = await RunManager(db).create_run(
            request, owner_sub=args.owner, groups=args.groups or None,
        )
        run_id = result["run_id"]
        print(f"\nstarted  {result['name']}  ({run_id})")
        run = await owned.get_run(run_id)
        if not run or run.get("status") == "pending":
            # `create_run` logs a warning and still returns when StartExecution is
            # skipped. Surfacing it here, because "pending for ever" reads as a slow
            # run rather than as one with no execution behind it.
            print("  note: status is still 'pending'; giving the machine a moment")
        if args.no_watch:
            return 0
        print()
        return await watch(owned, run_id, timeout_s=args.timeout)
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
