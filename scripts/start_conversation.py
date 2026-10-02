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


async def check_owner(db: Database, owner: str, *, allow_unknown: bool) -> None:
    """Refuse an `--owner` that is nobody, before a single turn is paid for.

    **This exists because of a real, expensive mistake.** A sub was read from a scan whose output
    had been truncated to 14 characters and the remaining 22 were invented. The result was a
    valid-looking partition that no user owns: five conversations, a report and several
    regenerations — about $8 — went to a tenant the operator could never see, because the SPA
    resolves the owner from a verified token and correctly shows only that partition. Recovering
    it meant rewriting the partition key on 1,821 items.

    Nothing caught it because this script bypasses the API's tenancy boundary ON PURPOSE (see the
    module docstring), so `--owner` was taken at face value all the way through.

    **The user pool is authoritative when there is one.** The first version of this guard treated
    "this owner already has runs" as an equally good signal, and it let the same bad sub straight
    through on the very next attempt — because the sub owned five runs *created by the original
    mistake*. A guard whose evidence is produced by the fault it guards against is not a guard. It
    cost another five turns before being stopped.

    So existing runs are a FALLBACK, used only when no pool is configured, for a local or imported
    tenant that legitimately has no Cognito user. When a pool is reachable, its answer decides.

    Failing CLOSED on neither signal. A typo here is invisible afterwards, so "I could not verify"
    must not read as "fine". `--allow-unknown-owner` is the deliberate override.
    """
    existing = 0
    try:
        existing = len(await db.for_owner(owner).list_runs(limit=1))
    except Exception as exc:  # noqa: BLE001
        print(f"  note: could not list runs for this owner ({exc})")

    pool = os.environ.get("USER_POOL_ID")
    in_pool: Optional[bool] = None
    if pool:
        try:
            import boto3
            from botocore.exceptions import ClientError

            client = boto3.client("cognito-idp", region_name=os.environ["AWS_REGION"])
            try:
                client.admin_get_user(UserPoolId=pool, Username=owner)
                in_pool = True
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "UserNotFoundException":
                    # `admin_get_user` takes a username, which is the sub only when the pool was
                    # built that way. Fall back to a filtered list, which queries the attribute.
                    found = client.list_users(
                        UserPoolId=pool, Filter=f'sub = "{owner}"', Limit=1,
                    )
                    in_pool = bool(found.get("Users"))
                else:
                    raise
        except Exception as exc:  # noqa: BLE001
            print(f"  note: could not check the user pool ({exc})")

    if in_pool:
        print(f"  owner      {owner}  (a user with this sub exists)")
        return

    if in_pool is None and existing:
        # No pool to ask, so the weaker signal has to do — and says so, because "this partition
        # already has data" is exactly what a previous mistake leaves behind.
        print(
            f"  owner      {owner}  (owns runs; NO user pool checked — set USER_POOL_ID to "
            "verify this is a real user)"
        )
        return

    if in_pool is False and existing:
        raise SystemExit(
            f"\nRefusing to start: --owner {owner} owns {existing} run(s) but matches NO user in "
            "the pool.\n"
            "\n"
            "That combination is the signature of an earlier mistake rather than a reason to\n"
            "proceed: a mistyped sub creates runs, and those runs then make the sub look\n"
            "established. The pool is authoritative — a partition with data in it is not a user.\n"
            "\n"
            "If this tenant is genuinely poolless, pass --allow-unknown-owner."
        )

    if allow_unknown:
        print(
            f"  owner      {owner}  *** UNVERIFIED, proceeding because "
            "--allow-unknown-owner was passed ***"
        )
        return

    raise SystemExit(
        f"\nRefusing to start: --owner {owner} matches no Cognito user and owns no runs.\n"
        "\n"
        "A wrong sub is not visible afterwards — the conversations run, cost real money, and\n"
        "never appear in the UI, because the SPA only ever shows the partition belonging to the\n"
        "signed-in token. That has happened: ~$8 of conversations under a sub whose last 22\n"
        "characters had been invented from a truncated log line.\n"
        "\n"
        "Check the sub with:\n"
        "  aws cognito-idp list-users --user-pool-id \"$USER_POOL_ID\" \\\n"
        "    --query \"Users[].Attributes[?Name=='sub'].Value\" --output text\n"
        "\n"
        "Set USER_POOL_ID so this check can be conclusive, or pass --allow-unknown-owner if the\n"
        "tenant really is new."
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


def _research_on(request: Dict[str, Any]) -> bool:
    from matrix_studio import research_state as rs

    return rs.settings_from(request.get("config")).enabled


async def describe_research(db: Database, request: Dict[str, Any], owner: str) -> None:
    """Where a research pass would WRITE, before a penny is spent. Reads nothing into it.

    This exists because of a specific mistake, on 2026-09-24. A verify definition was prepared with
    the run-level `knowledge_bases` removed, and reported as "research will create its collections,
    so nothing curated is touched". **The personas had their own bindings and nobody looked.** Six
    hand-curated collections each gained a dozen searched documents — which is §5.1's designed
    reuse working correctly, and was not what the operator was told would happen.

    An ingest target is the one thing about this feature that cannot be undone by reading a log
    afterwards: by then the documents are in somebody's collection. So it is printed as a plan per
    scope, and `--dry-run` shows it without writing anything.

    Since 2026-10-01 every scope is CREATE — research writes only into collections made for the
    run (`research_state.allocate_targets`), never into one that is bound — so what this prints
    that an operator still needs is the READ side: which bound collections the run will search
    alongside its research, how many curated documents each holds, and whether one of them is an
    EARLIER pass's research. That last case is legal (it is read-only to this run) and is how a
    hand-copied definition ends up reading two passes at once, so it is said out loud.
    """
    from matrix_studio import research_state as rs
    from matrix_studio.bindings import _clean

    settings = rs.settings_from(request.get("config"))
    if not settings.enabled:
        return

    config = request.get("config") or {}
    print(f"  research    ON — shared={settings.shared} personas={settings.personas} "
          f"consultants={settings.consultants}")
    scopes: list = []
    if settings.shared:
        scopes.append(("shared (whole cast)", _clean(config.get("knowledge_bases"))))
    if settings.personas:
        for member in request.get("cast") or []:
            viewpoints = ((member.get("structured") or {}).get("viewpoints")) or []
            if str(member.get("name") or "").strip() and viewpoints:
                scopes.append((str(member["name"]), rs._persona_kbs(member)))
    if settings.consultants:
        for expert in config.get("experts") or []:
            if isinstance(expert, dict) and str(expert.get("name") or "").strip():
                scopes.append((f"consultant {expert['name']}", rs._persona_kbs(expert)))

    for who, bound in scopes:
        print(f"    CREATE  {who:<22} (a new collection for this run's research)")
        for kb_id in bound:
            kb = await db.get_knowledge_base(kb_id)
            if kb is None:
                print(f"      read  {kb_id}  (does not exist — the create will refuse it)")
                continue
            docs = await db.list_kb_documents(kb_id)
            curated = [d for d in docs if str(d.get("origin") or "") != "researched"]
            note = ""
            if kb.get("research_for") or (docs and not curated):
                note = "  << an EARLIER research pass — read alongside this one"
            elif len(curated) < len(docs):
                note = f"  ({len(docs) - len(curated)} researched doc(s) from before 2026-10-01)"
            print(f"      read  {kb_id}  {str(kb.get('name'))[:28]:<30}"
                  f"  {len(curated)} curated doc(s){note}")


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


def _print_renamed(result: dict) -> None:
    """Say which real public figures' names the definition carried (matrix_studio/real_names.py): the run
    was created with the parodies, and a definition that will be reused should be changed to match."""
    for r in result.get("renamed") or []:
        print(f"  renamed  '{r['from']}' is a real public figure, so this {r['role']} is '{r['to']}'")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("definition", nargs="?", help="a conversation definition JSON")
    parser.add_argument("--owner", help="the Cognito sub that will own the run")
    parser.add_argument("--groups", action="append", default=[],
                        help="Cognito groups to record on the run (KB grants use them)")
    parser.add_argument("--max-messages", type=int, default=None,
                        help="override the definition's turn budget")
    parser.add_argument("--name", default=None,
                        help="override the definition's name. Worth using for an ensemble: "
                             "the name is the stem of every member's name, so a stale or "
                             "misleading one is stamped onto N runs and a report.")
    parser.add_argument("--ensemble", type=int, metavar="N", default=None,
                        help="run the definition N times as an ensemble and report over "
                             "them, instead of once. N>=2; see "
                             "docs/ENSEMBLE-CONVERSATIONS.md")
    parser.add_argument("--hybrid", type=int, metavar="N", default=None,
                        help="with --ensemble, add a second group of N runs differing ONLY "
                             "in speaker method (hybrid). Anything else would confound the "
                             "comparison and the planner refuses it.")
    parser.add_argument("--allow-unknown-owner", action="store_true",
                        help="start even when --owner matches no Cognito user and owns no runs. "
                             "For a genuinely new tenant; see `check_owner` for the mistake this "
                             "guard exists to prevent.")
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
    # A research dry run reads the account, so it needs the environment a real run needs.
    if not args.dry_run or (args.definition and _research_on(load_definition(args.definition))):
        _require_env()

    if args.definition:
        request = load_definition(args.definition)
        if args.max_messages is not None:
            request.setdefault("config", {})["max_messages"] = args.max_messages
        if args.name:
            request["name"] = args.name
        print(f"\n{args.definition}")
        describe(request)
        if args.ensemble is not None:
            # Built here so a bad spec is refused BEFORE anything is created, which is the
            # planner's own contract — and so `--dry-run` prints the real plan rather than an
            # optimistic one.
            from matrix_studio import ensemble_spec

            cells = [ensemble_spec.Cell(label="base", n=args.ensemble)]
            if args.hybrid:
                cells.append(ensemble_spec.Cell(
                    label="hybrid", n=args.hybrid,
                    overrides={"selection.method": "hybrid",
                               "selection.hybrid_opening_rounds": 2},
                ))
            try:
                members = ensemble_spec.plan(request.get("config") or {}, cells)
            except ensemble_spec.SpecError as exc:
                raise SystemExit(f"\nRefused: {exc}")
            print(f"\nensemble  {len(members)} conversations")
            for cell in cells:
                varied = ", ".join(f"{k}={v}" for k, v in cell.overrides.items())
                print(f"  {cell.label:<8} {cell.n} run(s)  {varied or 'nothing varied'}")
        if args.dry_run and not _research_on(request):
            print("\nDry run: nothing was created.")
            return 0
        # A dry run WITH research falls through to connect, because the ingest-target plan can
        # only be produced by reading the account: which collections are bound, and who owns
        # them. That is the one thing about this feature worth seeing before spending — an
        # ingested document cannot be un-ingested by reading a log afterwards.

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
        # BEFORE anything is created or any turn is paid for. Not for `--watch`, which only
        # reads, and which is the one case where an unknown owner is self-evident: there is
        # nothing there.
        if not args.watch:
            await check_owner(db, args.owner, allow_unknown=args.allow_unknown_owner)

        owned = db.for_owner(args.owner)
        if args.watch:
            return await watch(owned, args.watch, timeout_s=args.timeout)

        # The ingest-target plan, printed BEFORE anything is created. See
        # `describe_research` for the mistake that put it here.
        await describe_research(owned, request, args.owner)
        if args.dry_run:
            print("\nDry run: nothing was created.")
            return 0

        from matrix_studio.api.manager import RunManager

        if args.ensemble is not None:
            # Same reasoning as the single-run path: go through `RunManager`, not a private
            # copy of "create an ensemble". The report is generated by the LAST member's
            # `finalise` in the deployed Lambda, so this process can exit immediately and
            # the report still appears.
            result = await RunManager(db).create_ensemble(
                request, cells, owner_sub=args.owner, groups=args.groups or None,
            )
            print(f"\nstarted  {result['name']}  ensemble {result['ensemble_id']}")
            _print_renamed(result)
            for m in result["members"]:
                print(f"  {m['cell']}{m['index']}  {m.get('name')}  {m['run_id']}")
            for f in result.get("failed") or []:
                print(f"  FAILED {f['cell']}{f['index']}: {f['error']}")
            print(
                "\nThe report is built by whichever member finishes last, so nothing here "
                "needs to stay running.\nWatch it at "
                f"/api/ensembles/{result['ensemble_id']} or in the SPA."
            )
            return 0

        result = await RunManager(db).create_run(
            request, owner_sub=args.owner, groups=args.groups or None,
        )
        run_id = result["run_id"]
        print(f"\nstarted  {result['name']}  ({run_id})")
        _print_renamed(result)
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
