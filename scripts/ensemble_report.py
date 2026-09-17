#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Collate several runs of the same brief: what held, who dissented, what cannot coexist.

The aggregation lives in `matrix_studio/ensemble.py`, not here, because stages 2 and 3 of this
feature (a parent run row, and a comparison view in the SPA) need the same logic and a script
directory must not become an import dependency of the deployed package.

What it prints, in the order the questions get asked:

    1  the arms and their computed metrics — no model involved
    2  each persona across every run: invariant demands, situational ones, concessions
    3  standing dissents and unresolved items, ranked by how many runs left them open
    4  one synthesis: held everywhere, held sometimes, and a pairwise coexistence test

Usage:
    export AWS_REGION=us-east-1 TABLE_PREFIX=matrix-studio DATA_BUCKET=...
    scripts/ensemble_report.py --owner SUB --run ID --run ID [--no-synthesis]
    scripts/ensemble_report.py --owner SUB --match renewal
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix_studio import ensemble  # noqa: E402
from matrix_studio.storage import Database  # noqa: E402


def _rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", required=True)
    ap.add_argument("--run", action="append", default=[], help="run id (repeatable)")
    ap.add_argument("--match", help="instead of ids, every COMPLETE run whose name contains this")
    ap.add_argument("--no-synthesis", action="store_true", help="skip the one generative call")
    ap.add_argument("--no-extraction", action="store_true",
                    help="metrics only — no model calls at all")
    ap.add_argument("--cache", default="/tmp/ensemble-cache.json",
                    help="where per-run extractions are stored and reused. The extraction is "
                         "the expensive half and it does not change when the SYNTHESIS "
                         "prompt does, so paying for it twice while iterating on the report "
                         "is money for nothing")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and re-extract")
    args = ap.parse_args()

    if not os.environ.get("DATA_BUCKET"):
        raise SystemExit("DATA_BUCKET must be set (see the stack outputs).")
    db = Database(
        table_prefix=os.environ.get("TABLE_PREFIX", "matrix-studio"),
        bucket=os.environ["DATA_BUCKET"],
        region=os.environ.get("AWS_REGION", "us-east-1"),
    )
    await db.connect()
    bound = db.for_owner(args.owner)
    try:
        ids = list(args.run)
        if args.match:
            # Ordered by creation, so the report reads in the order the runs happened.
            rows = await bound.list_runs(limit=200)
            ids = [
                r["id"] for r in sorted(rows, key=lambda r: int(r.get("created_at") or 0))
                if args.match in str(r.get("name") or "") and r.get("status") == "complete"
            ]
        if not ids:
            raise SystemExit("no runs selected")

        views = await ensemble.collect(bound, ids)
        _rule(f"1. THE ARMS — {len(views)} runs, computed metrics only")
        print(f"{'run':<26} {'method':<13} {'turns':>5} {'gini':>6} {'min':>4} {'dyad':>5} "
              f"{'cover':>6} {'chars 1st→2nd':>14}")
        for v in views:
            m = v.metrics
            print(f"{v.name[:25]:<26} {v.arm.get('method','?'):<13} {m['turns']:>5} "
                  f"{m['gini']:>6.3f} {m['min_turns']:>4} {m['dyad_chain']:>5} "
                  f"{str(m['coverage_turn']):>6} "
                  f"{str(m['chars_first_half']) + '→' + str(m['chars_second_half']):>14}")

        if args.no_extraction:
            return 0

        cache_path = Path(args.cache)
        cache = {}
        if cache_path.exists() and not args.refresh:
            try:
                cache = json.loads(cache_path.read_text())
            except (OSError, json.JSONDecodeError):
                cache = {}
        spend = 0.0
        for v in views:
            hit = cache.get(v.run_id)
            if hit and not args.refresh:
                v.positions = hit
                print(f"  cached    {v.name} "
                      f"({len(hit.get('personas') or [])} personas)")
                continue
            v.positions = await ensemble.extract_positions(v)
            spend += float(v.positions.get("_cost_usd") or 0.0)
            print(f"  extracted {v.name} "
                  f"({len(v.positions.get('personas') or [])} personas)")
            if v.positions:
                cache[v.run_id] = v.positions
                cache_path.write_text(json.dumps(cache, indent=1))
        readable = [v for v in views if v.positions]
        if not readable:
            raise SystemExit("no extraction could be read; nothing to aggregate")

        _rule("2. EACH PERSONA ACROSS EVERY RUN")
        for name, d in ensemble.per_persona(readable).items():
            print(f"\n--- {name}  (in {d['appears_in_runs']} of {d['of_runs']} runs)")
            for label, key in (("held in EVERY run", "invariant_demands"),
                               ("held in some", "situational_demands"),
                               ("refused", "refusals")):
                items = d[key]
                if items:
                    print(f"  {label}:")
                    for it in items:
                        print(f"    · {it['claim'][:110]}")
                        print(f"        {len(it['runs'])}/{d['appears_in_runs']}: "
                              f"{', '.join(it['runs'])}")
            if d["concessions"]:
                print("  gave ground:")
                for c in d["concessions"]:
                    print(f"    · [{c['run']}] {c['gave_up'][:80]} — {c['because'][:80]}")

        _rule("3. DISSENT AND WHAT WAS LEFT OPEN")
        ad = ensemble.agreements_and_dissents(readable)
        print("outcomes per run:")
        for name, outcome in ad["outcomes"]:
            print(f"  [{name}] {outcome[:150]}")
        print("\nunresolved, by how many runs left it open:")
        for u in ad["unresolved_by_frequency"][:10]:
            print(f"  {u['count']}x {u['claim'][:100]}")
            print(f"      {', '.join(u['runs'])}")
        print(f"\nstanding refusals (the outcome did not adopt them): {len(ad['standing_refusals'])}")
        for s in ad["standing_refusals"][:14]:
            print(f"  [{s['run']}] {s['persona']}: {s['refusal'][:100]}")

        if not args.no_synthesis:
            _rule("4. SYNTHESIS")
            out = await ensemble.synthesise(readable)
            print(out["content"])
            spend += float(out.get("cost_usd") or 0.0)
        print(f"\n(model spend for this report: ${spend:.4f})")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
