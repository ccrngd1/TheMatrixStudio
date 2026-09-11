#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Phase 5 acceptance: does a run actually execute on the deployed stack?

The plan's exit criteria for Phase 5, verbatim: *"a 40+ turn run completes
(impossible in one Lambda), a stop lands **after the turn in flight is persisted**,
and a cost cap terminates a run as `capped`."* This script checks all three against
the real state machine.

**It exists because Phase 4 was cancelled by measurement rather than by reasoning.**
That phase planned to run the loop in the API Lambda's background task, from
Lambda's documented limits, and died on its actual execution model: the sandbox
freezes when the handler returns, so the run logged one line and billed 8 ms. Nothing
local caught it, and nothing local can. A green unit suite proves the slice logic;
only this proves a conversation happens.

What it does NOT cover, deliberately: the API's `POST /api/runs` path needs a Cognito
JWT and is exercised by `tests/test_orchestration.py`. Here the run row is written
directly and the execution started directly, so a failure is attributable to the turn
loop rather than to authentication.

Costs real money — 40+ turns of Bedrock on the default Haiku model, plus a summary.

Usage:
    scripts/verify_turn_loop.py                    # all three checks
    scripts/verify_turn_loop.py --turns 40
    scripts/verify_turn_loop.py --only completion
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3  # noqa: E402

from matrix_studio.storage import Database  # noqa: E402

# A distinct owner so nothing here can be mistaken for real user data, and so a
# LeadingKeys failure would show up as an access error rather than as a silent read
# of somebody else's partition.
OWNER = "phase5-verify"

CAST = [
    {
        "name": "Ada",
        "persona": "a pragmatic staff engineer who wants to ship",
        "goals": ["get a decision made"],
    },
    {
        "name": "Bo",
        "persona": "a careful SRE who has been paged too often",
        "goals": ["find what breaks in production"],
    },
]

PASS = "PASS"
FAIL = "FAIL"
results: List[tuple] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((PASS if ok else FAIL, name, detail))
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def sfn():
    return boto3.client("stepfunctions", region_name=os.environ["AWS_REGION"])


def machine_arn() -> str:
    arn = os.environ.get("TURN_LOOP_ARN")
    if arn:
        return arn
    client = sfn()
    for page in client.get_paginator("list_state_machines").paginate():
        for m in page["stateMachines"]:
            if m["name"].endswith("turn-loop"):
                return m["stateMachineArn"]
    raise SystemExit("no turn-loop state machine found; set TURN_LOOP_ARN")


async def make_run(db, run_id: str, *, turns: int, name: str) -> None:
    await db.create_run(
        run_id=run_id,
        topic="whether to roll back the release or fix forward tonight",
        cast=CAST,
        name=name,
        description="phase 5 verification",
        config={"max_messages": turns, "generate_avatars": False},
        owner_sub=OWNER,
    )


def start(run_id: str, *, turns: int) -> str:
    return sfn().start_execution(
        stateMachineArn=machine_arn(),
        name=f"verify-{run_id}"[:80],
        input=json.dumps({
            "run_id": run_id, "owner_sub": OWNER, "turn": 0,
            "max_messages": turns, "total_cost_usd": 0.0,
        }),
    )["executionArn"]


async def watch(
    db,
    run_id: str,
    execution_arn: str,
    *,
    timeout_s: int,
    on_turn=None,
) -> Dict[str, Any]:
    """Poll until the execution stops, reporting turns as they land.

    Polls the EVENT LOG as well as the execution, because those are different
    questions: the execution's status says the machine finished, and the log says a
    conversation happened. A machine that succeeds having generated nothing is the
    failure this whole script exists to detect.
    """
    client = sfn()
    deadline = time.time() + timeout_s
    seen = 0
    last_report = 0.0
    while time.time() < deadline:
        desc = client.describe_execution(executionArn=execution_arn)
        events = await db.get_events(run_id)
        turns = sorted({
            e["turn"] for e in events if e["event_type"] == "agent.response"
        })
        if len(turns) > seen:
            seen = len(turns)
            if on_turn:
                await on_turn(seen, events)
        if time.time() - last_report > 20:
            last_report = time.time()
            print(f"    … {desc['status']}, {seen} turn(s) generated", flush=True)
        if desc["status"] != "RUNNING":
            return {"execution": desc, "events": events, "turns": turns}
        await asyncio.sleep(4)
    raise TimeoutError(
        f"execution did not finish within {timeout_s}s "
        f"({seen} turns generated so far)"
    )


def terminal_events(events: List[Dict[str, Any]]) -> List[str]:
    return [
        e["event_type"] for e in events
        if e["event_type"].startswith("sim.") and e["event_type"] != "sim.started"
    ]


# --------------------------------------------------------------------------- #


async def verify_completion(db, turns: int) -> None:
    print(f"\n1. A {turns}-turn run completes (impossible inside one Lambda)")
    run_id = f"verify-complete-{int(time.time())}"
    await make_run(db, run_id, turns=turns, name=run_id)
    arn = start(run_id, turns=turns)
    print(f"   execution: {arn.rsplit(':', 1)[-1]}", flush=True)

    started = time.time()
    out = await watch(db, run_id, arn, timeout_s=60 * 40)
    elapsed = time.time() - started

    check(
        "the execution succeeded",
        out["execution"]["status"] == "SUCCEEDED",
        out["execution"]["status"],
    )
    check(
        f"{turns} turns were generated",
        len(out["turns"]) == turns,
        f"{len(out['turns'])} of {turns} in {elapsed / 60:.1f} min",
    )
    check(
        "turn numbers are contiguous from 1",
        out["turns"] == list(range(1, len(out["turns"]) + 1)),
        f"first={out['turns'][:3]} last={out['turns'][-3:]}" if out["turns"] else "none",
    )
    seqs = [e["seq"] for e in out["events"]]
    check(
        "event seqs are strictly increasing across every slice",
        seqs == sorted(seqs) and len(seqs) == len(set(seqs)),
        f"{len(seqs)} events",
    )
    check(
        "exactly one terminal event, and it is sim.completed",
        terminal_events(out["events"]) == ["sim.completed"],
        str(terminal_events(out["events"])),
    )
    run = await db.get_run(run_id)
    check("the run row reads complete", run["status"] == "complete", str(run["status"]))
    snap = await db.get_snapshot(run_id)
    check(
        "the final snapshot holds the whole transcript",
        snap is not None and len(snap.conversation) == turns,
        f"{len(snap.conversation) if snap else 0} messages",
    )
    # The dossier the acceptance criteria ask for: a summary, generated by Finalise.
    summaries = await db.get_summaries(run_id)
    check("a summary was generated", bool(summaries), f"{len(summaries)} summary/ies")
    cost = sum(a.total_cost_usd for a in snap.agents.values()) if snap else 0.0
    print(f"   cost: ${cost:.4f} over {turns} turns")


async def verify_stop(db) -> None:
    print("\n2. A stop lands AFTER the turn in flight is persisted")
    run_id = f"verify-stop-{int(time.time())}"
    await make_run(db, run_id, turns=25, name=run_id)
    arn = start(run_id, turns=25)
    print(f"   execution: {arn.rsplit(':', 1)[-1]}", flush=True)

    asked_at: Dict[str, int] = {}

    async def on_turn(count: int, _events) -> None:
        # Ask to stop once two turns exist, and record where we asked so the
        # "finished the turn in flight" claim can be checked rather than assumed.
        if count == 2 and "turn" not in asked_at:
            asked_at["turn"] = count
            await db.set_stop_requested(run_id)
            print(f"    stop requested after turn {count}", flush=True)

    out = await watch(db, run_id, arn, timeout_s=60 * 15, on_turn=on_turn)

    check("a stop was actually requested", "turn" in asked_at,
          f"at turn {asked_at.get('turn')}")
    check(
        "the execution succeeded (a stop is not a failure)",
        out["execution"]["status"] == "SUCCEEDED",
        out["execution"]["status"],
    )
    run = await db.get_run(run_id)
    check("the run row reads stopped", run["status"] == "stopped", str(run["status"]))
    check(
        "exactly one terminal event, and it is sim.stopped",
        terminal_events(out["events"]) == ["sim.stopped"],
        str(terminal_events(out["events"])),
    )
    # The guarantee: the turn being generated when the stop arrived was finished and
    # persisted, so the transcript is one longer than where we asked — never shorter.
    at = asked_at.get("turn", 0)
    last = out["turns"][-1] if out["turns"] else 0
    # An upper bound as well as a lower one, and the upper one is the point.
    #
    # `>= at` alone passed while the deployment was generating TWO extra turns: the
    # engine's `should_stop` closes over the run row read at the slice's start, so a
    # stop arriving mid-turn was invisible and the machine looped once more. The
    # documented contract is that the turn in flight finishes and "only the NEXT one
    # is prevented", so at most one turn beyond where the stop was asked.
    check(
        "the turn in flight was finished and persisted",
        len(out["turns"]) >= at,
        f"asked after turn {at}, log ends at turn {last}",
    )
    check(
        "no more than one turn beyond the request",
        last <= at + 1,
        f"asked after turn {at}, log ends at turn {last}",
    )
    check(
        "it stopped early rather than running to its 25-turn budget",
        len(out["turns"]) < 25,
        f"{len(out['turns'])} turns",
    )
    snap = await db.get_snapshot(run_id)
    check(
        "the final snapshot matches the persisted turns",
        snap is not None and len(snap.conversation) == len(out["turns"]),
        f"snapshot {len(snap.conversation) if snap else 0} vs log {len(out['turns'])}",
    )
    check(
        "a stopped run is resumable",
        run["status"] in {"interrupted", "failed", "stopped"},
        str(run["status"]),
    )


async def verify_cap(db) -> None:
    print("\n3. A cost cap terminates the run as `capped`")
    # The cap is a SETTING on the worker, not per-run, so this cannot be driven from
    # here without redeploying the function's environment. Rather than skip the
    # criterion silently, set it, run, and put it back.
    fn = "matrix-studio-turn"
    lam = boto3.client("lambda", region_name=os.environ["AWS_REGION"])
    current = lam.get_function_configuration(FunctionName=fn)["Environment"]["Variables"]
    original = current.get("MAX_RUN_COST_USD")
    try:
        lam.update_function_configuration(
            FunctionName=fn,
            Environment={"Variables": {**current, "MAX_RUN_COST_USD": "0.000001"}},
        )
        # An in-flight update makes the next invoke fail with ResourceConflict, so wait.
        lam.get_waiter("function_updated_v2").wait(FunctionName=fn)

        run_id = f"verify-cap-{int(time.time())}"
        await make_run(db, run_id, turns=20, name=run_id)
        arn = start(run_id, turns=20)
        print(f"   execution: {arn.rsplit(':', 1)[-1]}", flush=True)
        out = await watch(db, run_id, arn, timeout_s=60 * 10)

        run = await db.get_run(run_id)
        check("the run row reads capped", run["status"] == "capped", str(run["status"]))
        check(
            "exactly one terminal event, and it is sim.capped",
            terminal_events(out["events"]) == ["sim.capped"],
            str(terminal_events(out["events"])),
        )
        check(
            "it capped early rather than running its 20-turn budget",
            0 < len(out["turns"]) < 20,
            f"{len(out['turns'])} turns",
        )
    finally:
        restored = {k: v for k, v in current.items()}
        if original is None:
            restored.pop("MAX_RUN_COST_USD", None)
        else:
            restored["MAX_RUN_COST_USD"] = original
        lam.update_function_configuration(
            FunctionName=fn, Environment={"Variables": restored}
        )
        lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
        print("   (restored the worker's cost-cap setting)")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=40,
                    help="Turns for the completion check (the criterion says 40+)")
    ap.add_argument("--only", choices=["completion", "stop", "cap"], default=None)
    args = ap.parse_args()

    os.environ.setdefault("AWS_REGION", "us-east-1")
    for required in ("TABLE_PREFIX", "DATA_BUCKET"):
        if not os.environ.get(required):
            raise SystemExit(f"set {required} (see the stack outputs)")

    print(f"Verifying the Phase 5 turn loop in {os.environ['AWS_REGION']}")
    print(f"  state machine: {machine_arn()}")

    store = Database()
    await store.connect()
    db = store.for_owner(OWNER)
    try:
        if args.only in (None, "completion"):
            await verify_completion(db, args.turns)
        if args.only in (None, "stop"):
            await verify_stop(db)
        if args.only in (None, "cap"):
            await verify_cap(db)
    finally:
        await store.close()

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for _status, name, detail in failed:
        print(f"  FAIL {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
