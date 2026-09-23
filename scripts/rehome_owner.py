#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Move every item belonging to one owner into another owner's partition.

## Why this exists

A mistyped `--owner` put five conversations, an ensemble and its report into a partition no user
owns: about $8 of work the operator could never see, because the SPA resolves the owner from a
verified token and correctly shows only that partition. `scripts/start_conversation.py` now
refuses an owner that matches no Cognito user, so this should be needed once.

## What it does, and the order it does it in

DynamoDB cannot rename a partition key. Every item is therefore **written to the new key and only
then deleted from the old one**, per item, so an interruption leaves duplicates rather than a hole.
Duplicates are recoverable by re-running; a hole is not.

`--apply` is required. Without it this reports exactly what it would move and changes nothing.

## What it deliberately does NOT move

`SPEND#` items. The monthly total is an accounting fact about a tenant, and moving spend from a
partition nobody owns into a real user's cap would silently charge them for work they did not
start. It is reported so the number is not lost.

`NAME#` markers are rewritten, and a collision is FATAL rather than skipped: the marker enforces
per-owner name uniqueness, and a run whose name is already taken in the destination would end up
with a marker pointing at the other run.

Items keyed by run rather than by user — summaries, threads, documents — are untouched by design:
their key does not contain the owner, so they follow their run automatically.

Usage:
    export AWS_REGION=us-east-1 TABLE_PREFIX=matrix-studio
    scripts/rehome_owner.py --from SUB_A --to SUB_B            # report only
    scripts/rehome_owner.py --from SUB_A --to SUB_B --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: Tables whose partition key is `USER#{sub}`. Everything else keys by run or by collection and
#: needs no rewrite — see the module docstring.
USER_PARTITIONED = ("runs", "events", "snapshots")


def _pk(sub: str) -> str:
    return f"USER#{sub}"


def _scan_partition(table, pk: str) -> List[Dict[str, Any]]:
    from boto3.dynamodb.conditions import Key

    out: List[Dict[str, Any]] = []
    kw: Dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(pk)}
    while True:
        res = table.query(**kw)
        out += res.get("Items", [])
        if not res.get("LastEvaluatedKey"):
            break
        kw["ExclusiveStartKey"] = res["LastEvaluatedKey"]
    return out


def _classify(items: List[Dict[str, Any]]) -> Counter:
    kinds: Counter = Counter()
    for item in items:
        kinds[str(item.get("sk", "")).split("#")[0] or "?"] += 1
    return kinds


def main() -> int:
    import boto3

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="src", required=True, help="the sub to move FROM")
    ap.add_argument("--to", dest="dst", required=True, help="the sub to move TO")
    ap.add_argument("--ensemble", action="append", default=[],
                    help="move only this ensemble and its member runs (repeatable). Without it, "
                         "the WHOLE partition moves — rarely what you want, since a partition "
                         "created by mistake usually holds other mistakes too.")
    ap.add_argument("--apply", action="store_true",
                    help="actually write and delete. Without it, nothing changes.")
    args = ap.parse_args()

    if args.src == args.dst:
        raise SystemExit("--from and --to are the same sub; nothing to do.")
    region = os.environ.get("AWS_REGION") or "us-east-1"
    prefix = os.environ.get("TABLE_PREFIX", "matrix-studio")
    ddb = boto3.resource("dynamodb", region_name=region)

    src_pk, dst_pk = _pk(args.src), _pk(args.dst)
    print(f"{src_pk}\n  -> {dst_pk}\n  region={region} prefix={prefix}\n")

    # Scope, if asked for: the ensemble rows, their member runs, and everything keyed by those
    # runs. Anything else in the partition stays put. A partition that exists because of a
    # mistake tends to contain later mistakes as well, and dragging those into a real user's
    # account is worse than leaving them somewhere nobody looks.
    keep_runs: Optional[set] = None
    keep_ensembles: Optional[set] = None
    if args.ensemble:
        import json as _json

        runs_t = ddb.Table(f"{prefix}-runs")
        keep_ensembles = set(args.ensemble)
        keep_runs = set()
        for ens_id in args.ensemble:
            row = runs_t.get_item(
                Key={"pk": src_pk, "sk": f"ENSEMBLE#{ens_id}"}
            ).get("Item")
            if not row:
                raise SystemExit(f"ensemble {ens_id} is not in {src_pk}")
            for m in _json.loads(row.get("members_json") or "[]"):
                if m.get("run_id"):
                    keep_runs.add(str(m["run_id"]))
        print(f"  scoped to {len(keep_ensembles)} ensemble(s) and {len(keep_runs)} member run(s)\n")

    def wanted(item: Dict[str, Any]) -> bool:
        if keep_runs is None:
            return True
        sk = str(item.get("sk", ""))
        if sk.startswith("ENSEMBLE#"):
            return sk.split("#", 1)[1] in (keep_ensembles or set())
        if sk.startswith("NAME#"):
            # A marker names a run rather than containing its id in the key.
            return str(item.get("run_id") or "") in keep_runs
        if "#" in sk:
            # `RUN#{id}`, `RUN#{id}#{seq}`, `RUN#{id}#{turn}` — the id is the second segment.
            parts = sk.split("#")
            return len(parts) > 1 and parts[1] in keep_runs
        return False

    plan: List[Tuple[str, List[Dict[str, Any]]]] = []
    spend_items: List[Dict[str, Any]] = []
    total = 0

    for name in USER_PARTITIONED:
        table = ddb.Table(f"{prefix}-{name}")
        items = _scan_partition(table, src_pk)
        # Spend is an accounting fact about a tenant, not part of the work. Reported, never moved.
        moving = [
            i for i in items
            if not str(i.get("sk", "")).startswith("SPEND#") and wanted(i)
        ]
        spend_items += [i for i in items if str(i.get("sk", "")).startswith("SPEND#")]
        plan.append((name, moving))
        total += len(moving)
        kinds = ", ".join(f"{k}#{v}" for k, v in sorted(_classify(moving).items()))
        print(f"  {name:<10} {len(moving):>5} items   {kinds or '(none)'}")

    if spend_items:
        print(f"\n  NOT moving {len(spend_items)} SPEND# item(s):")
        for item in spend_items:
            print(f"    {item.get('sk')}  cost_usd={item.get('cost_usd')}")
        print("  Moving spend would charge the destination's cap for work it did not start.")

    # Name markers must not collide: the marker is what enforces per-owner name uniqueness, and
    # overwriting one in the destination would point it at a different run.
    runs = ddb.Table(f"{prefix}-runs")
    markers = [i for i in plan[0][1] if str(i.get("sk", "")).startswith("NAME#")]
    clashes = []
    for marker in markers:
        got = runs.get_item(Key={"pk": dst_pk, "sk": marker["sk"]}).get("Item")
        if got and got.get("run_id") != marker.get("run_id"):
            clashes.append((marker["sk"], marker.get("run_id"), got.get("run_id")))
    if clashes:
        print("\nREFUSING: these names already exist in the destination, pointing elsewhere:")
        for sk, mine, theirs in clashes:
            print(f"  {sk}: would move run {mine}, destination already has {theirs}")
        print("Rename the source runs first; a clobbered marker silently mis-resolves a name.")
        return 1
    print(f"\n  {len(markers)} name marker(s), no collisions in the destination")

    if not args.apply:
        print(f"\nDRY RUN: {total} item(s) would move. Re-run with --apply.")
        return 0

    print(f"\napplying to {total} item(s) — write to the new key, then delete the old\n")
    moved = 0
    for name, items in plan:
        table = ddb.Table(f"{prefix}-{name}")
        for item in items:
            fresh = dict(item)
            fresh["pk"] = dst_pk
            if "owner_sub" in fresh:
                # Carried on run rows and read by the turn loop, which has no token to derive it
                # from. A row moved without this would be served to the new owner while every
                # background path still attributed it to the old one.
                fresh["owner_sub"] = args.dst
            table.put_item(Item=fresh)
            table.delete_item(Key={"pk": src_pk, "sk": item["sk"]})
            moved += 1
            if moved % 200 == 0:
                print(f"  {moved}/{total}")
    print(f"  {moved}/{total}\n")

    left = sum(len(_scan_partition(ddb.Table(f"{prefix}-{n}"), src_pk)) for n in USER_PARTITIONED)
    print(f"verify: {left} item(s) remain under {src_pk} "
          f"({len(spend_items)} expected — the spend records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
