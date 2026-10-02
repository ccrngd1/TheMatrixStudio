# SPDX-License-Identifier: Apache-2.0
"""Score the moderator-assumptions comparison against the criteria in docs/studies/MODERATOR-ASSUMPTIONS.md.

Written before the on-arm runs, and not tuned afterwards. Two passes, in this order:

1. Without ``--labels``: lists every assumption the moderator made (statement, gap, asks) so they can be
   labelled FACT / PLAN / DECISION / POSITION from the statement and topic alone, and every check.
   Makes no model call.
2. With ``--labels``: scores both primaries from the labels and the log, then the guardrails and the
   reported metrics via `analyse_evidence_lean.arm` (one analyst call per run).

    AWS_REGION=us-east-1 DATA_BUCKET=... scripts/analyse_moderator_assumptions.py --owner SUB \\
        --on RUN ... --off RUN ... [--labels private/labels/moderator-assumptions.json] \\
        --out private/docs/moderator-assumptions.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix_studio import assumptions as am  # noqa: E402
from matrix_studio import export as ex  # noqa: E402
from matrix_studio.storage import Database  # noqa: E402

LABELS = ("FACT", "PLAN", "DECISION", "POSITION")


def _p(e):
    p = e["payload"]
    return json.loads(p) if isinstance(p, str) else (p or {})


async def log_of(store, rid):
    events = await store.get_events(rid)
    checks = [_p(e) for e in events if e["event_type"] == "assumption.checked"]
    made = [_p(e) for e in events if e["event_type"] == "assumption.made" and _p(e).get("source") == am.MODERATOR]
    model = await ex.run_model(store, await store.get_run(rid))
    used = am.usage([m["id"] for m in made], model["transcript"])
    return {"run": rid, "checks": checks, "made": made, "usage": used}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--on", nargs="+", required=True)
    ap.add_argument("--off", nargs="+", required=True)
    ap.add_argument("--labels")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if not args.out.startswith("private/"):
        print("--out must be under private/: the rows quote real transcripts", file=sys.stderr)
        return 2
    db = Database(table_prefix=os.environ.get("TABLE_PREFIX", "matrix-studio"),
                  bucket=os.environ["DATA_BUCKET"], region=os.environ["AWS_REGION"])
    await db.connect()
    store = db.for_owner(args.owner)

    logs = [await log_of(store, r) for r in args.on]
    for lg in logs:
        c = lg["checks"]
        print(f"\n{lg['run'][:8]}: {len(c)} checks, {sum(x.get('proposed') for x in c)} made, "
              f"{sum(1 for x in c if x.get('rejected'))} rejected")
        for x in c:
            if x.get("rejected"):
                print(f"  REJECTED after {x.get('after_turn')}: {x['rejected']} | "
                      f"{(x.get('proposal') or {}).get('statement', '')[:160]}")
        for m in lg["made"]:
            u = lg["usage"].get(m["id"], {})
            print(f"  MADE {lg['run'][:8]}:{m['id']} (turn {m['turn']}) {m['statement']}\n"
                  f"     gap: {m.get('gap')}\n     cited {u.get('cited')}, disputed {len(u.get('disputes') or [])}")

    result = {"on_logs": logs}
    if not args.labels:
        Path(args.out).write_text(json.dumps(result, indent=2))
        print("\nNo --labels: label every MADE line above, then re-run with --labels.")
        return 0

    labels = json.loads(Path(args.labels).read_text())
    made_keys = [f"{lg['run'][:8]}:{m['id']}" for lg in logs for m in lg["made"]]
    missing = [k for k in made_keys if labels.get(k) not in LABELS]
    if missing:
        print(f"Unlabelled or invalid: {missing}", file=sys.stderr)
        return 2
    counts = {lab: sum(1 for k in made_keys if labels[k] == lab) for lab in LABELS}
    n = len(made_keys)
    fact_share = counts["FACT"] / n if n else None
    firing = sum(1 for lg in logs if lg["made"])

    from scripts.analyse_evidence_lean import arm

    print("\neffects (one analyst call per run):")
    print("off:"); off = await arm(store, args.off)
    print("on:"); on = await arm(store, args.on)
    result.update({"labels": labels, "counts": counts, "off": off, "on": on})
    Path(args.out).write_text(json.dumps(result, indent=2))

    p1 = n > 0 and fact_share >= 0.80 and counts["DECISION"] == 0
    p2 = firing >= 2
    g1 = (on["mean_dissenters"] or 0) >= (off["mean_dissenters"] or 0) - 1
    g2 = bool(off["median_words"]) and abs(on["median_words"] - off["median_words"]) <= 0.25 * off["median_words"]
    g3 = on["cost_usd"] <= 1.15 * off["cost_usd"]
    checks = [x for lg in logs for x in lg["checks"]]
    print(f"\nlabels over {n} assumption(s): {counts}")
    print(f"primary 1 (>= 80% FACT and no DECISION):  {'MET' if p1 else 'MISSED'}"
          f"  ({(fact_share or 0):.2f}, {counts['DECISION']} decision)")
    print(f"primary 2 (fires in >= 2 of 3 runs):      {'MET' if p2 else 'MISSED'}  ({firing} of {len(logs)})")
    print(f"guardrail 1 (dissenters on >= off - 1):   {'MET' if g1 else 'MISSED'}"
          f"  ({on['mean_dissenters']} vs {off['mean_dissenters']})")
    print(f"guardrail 2 (median words within ±25%):   {'MET' if g2 else 'MISSED'}"
          f"  ({on['median_words']} vs {off['median_words']})")
    print(f"guardrail 3 (on cost <= 1.15x off):       {'MET' if g3 else 'MISSED'}"
          f"  ({on['cost_usd']:.2f} vs {off['cost_usd']:.2f})")
    print(f"reported: {len(checks)} checks, {sum(x.get('proposed') for x in checks)} made, "
          f"{sum(1 for x in checks if x.get('rejected'))} rejected; best guess on {on['best_guess_share']} "
          f"vs off {off['best_guess_share']}; runs with lean on {on['runs_with_lean']} vs off {off['runs_with_lean']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
