# SPDX-License-Identifier: Apache-2.0
"""Score the evidence-lean comparison against the criteria in docs/studies/EVIDENCE-LEAN.md.

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


async def arm(store, run_ids, repeats: int = 1):
    """One arm's metrics. With ``repeats`` > 1 the analyst scores each run that many times: the best-guess
    share is pooled over every repeat, a run "states a lean" if most repeats say so, and each run's spread
    is kept. Added after the same three runs scored 0.36 one day and 0.50 the next
    (docs/studies/MODERATOR-ASSUMPTIONS.md) — a single analyst pass is not a stable measurement."""
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
        conversation = await service._load_conversation(store, run)
        scores, payloads = [], []
        for _ in range(max(1, repeats)):
            result = await analysis.generate_summary(
                conversation, run.get("topic", ""), fields=FIELDS, model=service.resolve_model(run, None),
            )
            scores.append(score(result["payload"]))
            payloads.append(result["payload"])
        shares = [s["stated"]["best_guess"] / s["requests"] for s in scores if s["requests"]]
        leans = sum(s["lean_stated"] for s in scores)
        print(f"  {rid[:8]} status={run.get('status')} turns={stats.get('turn_count')} "
              f"best-guess share per repeat {[round(x, 2) for x in shares]}, lean {leans}/{len(scores)}")
        rows.append({"run": rid, "scores": scores, "payloads": payloads,
                     "lean_majority": leans * 2 > len(scores),
                     "share_spread": [round(min(shares), 3), round(max(shares), 3)] if shares else None})
    requests = sum(s["requests"] for r in rows for s in r["scores"])
    stated = sum(s["stated"]["best_guess"] for r in rows for s in r["scores"])
    return {
        "runs": len(rows),
        "repeats": max(1, repeats),
        "requests": requests,
        "best_guess_share": round(stated / requests, 3) if requests else None,
        "runs_with_lean": sum(r["lean_majority"] for r in rows),
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
    ap.add_argument("--criteria", type=int, choices=(1, 2), default=1,
                    help="1: docs/studies/EVIDENCE-LEAN.md; 2: docs/studies/EVIDENCE-LEAN-2.md (lean by majority of passes)")
    ap.add_argument("--repeats", type=int, default=1,
                    help="analyst passes per run; 1 reproduces the pre-registered scoring exactly")
    args = ap.parse_args()
    if not args.out.startswith("private/"):
        print("--out must be under private/: the rows quote real transcripts", file=sys.stderr)
        return 2
    db = Database(table_prefix=os.environ.get("TABLE_PREFIX", "matrix-studio"),
                  bucket=os.environ["DATA_BUCKET"], region=os.environ["AWS_REGION"])
    await db.connect()
    store = db.for_owner(args.owner)
    print("off:"); off = await arm(store, args.off, args.repeats)
    print("on:"); on = await arm(store, args.on, args.repeats)
    Path(args.out).write_text(json.dumps({"off": off, "on": on}, indent=2))
    for name, a in (("off", off), ("on", on)):
        print(name, json.dumps({k: v for k, v in a.items() if k != "rows"}))

    if args.criteria == 2:
        # docs/studies/EVIDENCE-LEAN-2.md: lean by majority of >= 3 passes is the primary; best-guess share is
        # reported only, because a single run scored 0.00 and 1.00 on it across passes.
        if args.repeats < 3:
            print("criteria 2 requires --repeats >= 3", file=sys.stderr)
            return 2
        p = on["runs_with_lean"] >= 2 and on["runs_with_lean"] - off["runs_with_lean"] >= 1
        g1 = (on["mean_dissenters"] or 0) >= (off["mean_dissenters"] or 0) - 1
        g2 = bool(off["median_words"]) and abs(on["median_words"] - off["median_words"]) <= 0.25 * off["median_words"]
        g3 = on["cost_usd"] <= 1.15 * off["cost_usd"]
        lean_passes = lambda a: sum(s["lean_stated"] for r in a["rows"] for s in r["scores"])  # noqa: E731
        print(f"primary (lean by majority: on >= 2 of 3 and on - off >= 1): {'MET' if p else 'MISSED'}"
              f"  ({on['runs_with_lean']} vs {off['runs_with_lean']})")
        print(f"guardrail 1 (mean dissenters on >= off - 1):   {'MET' if g1 else 'MISSED'}"
              f"  ({on['mean_dissenters']} vs {off['mean_dissenters']})")
        print(f"guardrail 2 (median words within ±25%):        {'MET' if g2 else 'MISSED'}"
              f"  ({on['median_words']} vs {off['median_words']})")
        print(f"guardrail 3 (on cost <= 1.15x off):            {'MET' if g3 else 'MISSED'}"
              f"  ({on['cost_usd']:.2f} vs {off['cost_usd']:.2f})")
        print(f"reported: lean passes on {lean_passes(on)}/{on['runs'] * args.repeats} vs off "
              f"{lean_passes(off)}/{off['runs'] * args.repeats}; best-guess share (noisy) "
              f"{on['best_guess_share']} vs {off['best_guess_share']}")
        return 0

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
