# SPDX-License-Identifier: Apache-2.0
"""Stage 1 of "Conversations end in 'it depends'": is the information needed for a decision in the
transcript at all?

For each stored run, asks the analyst for ONLY the evidence plan and the conditional recommendation
(the same prompt and parser a real summary uses), and counts how often each column comes back
"not stated". A column that is nearly always "not stated" is information the personas never say, so
no analysis can recover it — that is the case for Stage 2 (making evidence requests specific).

Reads the deployed store and saves nothing to it. Model calls cost money (one per run, ~$0.02-0.05).
Row text is written to --out, which should be under the gitignored `private/`: it quotes real runs.

    AWS_REGION=us-east-1 DATA_BUCKET=... scripts/measure_evidence_plan.py --owner SUB \\
        --out private/docs/evidence-plan-stage1.json [--runs ID ...] [--latest 8 --min-turns 20]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix_studio import analysis, service  # noqa: E402
from matrix_studio.storage import Database  # noqa: E402

FIELDS = ["evidence_plan", "conditional_recommendation"]


def score(payload: dict) -> dict:
    plan = payload.get("evidence_plan") or []
    stated = {k: sum(1 for r in plan if r.get(k) != analysis.NOT_STATED) for k in analysis.EVIDENCE_PLAN_KEYS}
    conditional = str(payload.get("conditional_recommendation") or "").strip()
    return {
        "requests": len(plan),
        "stated": stated,
        # The row that is the whole point: data, the result that would move someone, and a guess.
        "actionable": sum(
            1 for r in plan
            if r.get("moves_them") != analysis.NOT_STATED and r.get("best_guess") != analysis.NOT_STATED
        ),
        "conditional": bool(conditional),
        "lean_stated": bool(conditional) and analysis.NOT_STATED not in conditional.lower(),
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs", nargs="*", default=[])
    ap.add_argument("--latest", type=int, default=8)
    ap.add_argument("--min-turns", type=int, default=20)
    args = ap.parse_args()
    if not args.out.startswith("private/"):
        print("--out must be under private/: the rows quote real transcripts", file=sys.stderr)
        return 2

    db = Database(table_prefix=os.environ.get("TABLE_PREFIX", "matrix-studio"),
                  bucket=os.environ["DATA_BUCKET"], region=os.environ["AWS_REGION"])
    await db.connect()
    store = db.for_owner(args.owner)

    runs = [await store.get_run(r) for r in args.runs] if args.runs else [
        r for r in await store.list_runs(limit=200) if r.get("status") == "complete"
    ]
    picked = []
    for run in runs:
        if not run:
            continue
        stats = await store.get_run_stats(run["id"])
        if int(stats.get("turn_count") or 0) >= args.min_turns:
            picked.append((run, int(stats.get("turn_count") or 0)))
        if not args.runs and len(picked) >= args.latest:
            break

    rows, cost = [], 0.0
    for run, turns in picked:
        conversation = await service._load_conversation(store, run)
        result = await analysis.generate_summary(
            conversation, run.get("topic", ""), fields=FIELDS, model=service.resolve_model(run, None),
        )
        cost += result["cost_usd"]
        s = score(result["payload"])
        print(f"{run['id'][:8]} turns={turns:>3} requests={s['requests']:>2} actionable={s['actionable']:>2} "
              f"conditional={s['conditional']} lean={s['lean_stated']} parsed={result['parsed']} "
              f"stated={s['stated']}")
        rows.append({"run": run["id"], "turns": turns, "score": s, "payload": result["payload"]})

    requests = sum(r["score"]["requests"] for r in rows)
    totals = {k: sum(r["score"]["stated"][k] for r in rows) for k in analysis.EVIDENCE_PLAN_KEYS}
    summary = {
        "runs": len(rows),
        "requests": requests,
        "stated_share": {k: round(v / requests, 2) if requests else None for k, v in totals.items()},
        "actionable_share": round(sum(r["score"]["actionable"] for r in rows) / requests, 2) if requests else None,
        "runs_with_conditional": sum(r["score"]["conditional"] for r in rows),
        "runs_with_lean": sum(r["score"]["lean_stated"] for r in rows),
        "cost_usd": round(cost, 4),
    }
    print(json.dumps(summary, indent=2))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
