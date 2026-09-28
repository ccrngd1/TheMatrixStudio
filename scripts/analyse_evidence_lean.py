# SPDX-License-Identifier: Apache-2.0
"""Score the evidence-lean comparison against the criteria in docs/EVIDENCE-LEAN.md.

Written before the runs, and not tuned afterwards. The primary metrics are `measure_evidence_plan.score`
applied to each run exactly as Stage 1 measured the baseline; the guardrails come from the stored runs.
Reads the deployed store and saves nothing to it. One analyst call per run.

    AWS_REGION=us-east-1 DATA_BUCKET=... scripts/analyse_evidence_lean.py --owner SUB \\
        --off RUN_ID ... --on RUN_ID ... --out private/docs/evidence-lean.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix_studio import analysis, service  # noqa: E402
from matrix_studio.storage import Database  # noqa: E402
from scripts.measure_evidence_plan import FIELDS, score  # noqa: E402


async def arm(store, run_ids):
    rows, words, cost, dissenters = [], [], 0.0, []
    for rid in run_ids:
        run = await store.get_run(rid)
        stats = await store.get_run_stats(rid)
        cost += float(stats.get("total_cost_usd") or 0.0)
        for ev in await store.get_events(rid):
            if ev["event_type"] == "agent.response":
                p = ev["payload"]
                p = json.loads(p) if isinstance(p, str) else p
                words.append(len((p.get("message") or "").split()))
        auto = next((s for s in await store.get_summaries(rid) if s["kind"] == "generated"), None)
        dissenters.append(len(((auto or {}).get("payload") or {}).get("dissenters") or []))
        result = await analysis.generate_summary(
            await service._load_conversation(store, run), run.get("topic", ""), fields=FIELDS,
            model=service.resolve_model(run, None),
        )
        s = score(result["payload"])
        print(f"  {rid[:8]} status={run.get('status')} turns={stats.get('turn_count')} {s}")
        rows.append({"run": rid, "score": s, "payload": result["payload"]})
    requests = sum(r["score"]["requests"] for r in rows)
    return {
        "runs": len(rows),
        "requests": requests,
        "best_guess_share": (round(sum(r["score"]["stated"]["best_guess"] for r in rows) / requests, 3)
                             if requests else None),
        "runs_with_lean": sum(r["score"]["lean_stated"] for r in rows),
        "mean_dissenters": statistics.mean(dissenters) if dissenters else None,
        "median_words": statistics.median(words) if words else None,
        "cost_usd": round(cost, 4),
        "rows": rows,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--off", nargs="+", required=True)
    ap.add_argument("--on", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if not args.out.startswith("private/"):
        print("--out must be under private/: the rows quote real transcripts", file=sys.stderr)
        return 2
    db = Database(table_prefix=os.environ.get("TABLE_PREFIX", "matrix-studio"),
                  bucket=os.environ["DATA_BUCKET"], region=os.environ["AWS_REGION"])
    await db.connect()
    store = db.for_owner(args.owner)
    print("off:"); off = await arm(store, args.off)
    print("on:"); on = await arm(store, args.on)
    Path(args.out).write_text(json.dumps({"off": off, "on": on}, indent=2))
    for name, a in (("off", off), ("on", on)):
        print(name, json.dumps({k: v for k, v in a.items() if k != "rows"}))

    bg_on, bg_off = on["best_guess_share"] or 0, off["best_guess_share"] or 0
    p1 = bg_on >= 0.80 and bg_on - bg_off >= 0.20
    p2 = on["runs_with_lean"] >= 2
    g1 = (on["mean_dissenters"] or 0) >= (off["mean_dissenters"] or 0) - 1
    g2 = bool(off["median_words"]) and abs(on["median_words"] - off["median_words"]) <= 0.25 * off["median_words"]
    g3 = on["cost_usd"] <= 1.15 * off["cost_usd"]
    print(f"primary 1 (best guess on >= 0.80 and on - off >= 0.20): {'MET' if p1 else 'MISSED'}"
          f"  ({bg_on:.2f} vs {bg_off:.2f})")
    print(f"primary 2 (lean stated in >= 2 of 3 on runs):           {'MET' if p2 else 'MISSED'}"
          f"  ({on['runs_with_lean']} vs {off['runs_with_lean']})")
    print(f"guardrail 1 (mean dissenters on >= off - 1):            {'MET' if g1 else 'MISSED'}"
          f"  ({on['mean_dissenters']} vs {off['mean_dissenters']})")
    print(f"guardrail 2 (median words within ±25%):                 {'MET' if g2 else 'MISSED'}"
          f"  ({on['median_words']} vs {off['median_words']})")
    print(f"guardrail 3 (on cost <= 1.15x off):                     {'MET' if g3 else 'MISSED'}"
          f"  ({on['cost_usd']:.2f} vs {off['cost_usd']:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
