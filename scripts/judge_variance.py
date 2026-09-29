# SPDX-License-Identifier: Apache-2.0
"""Judge the same transcripts repeatedly and report each field's spread (docs/JUDGE-VARIANCE.md).

Uses `score_validation.judge_arm` unmodified, so what is measured is the shipped judge — same prompt,
schema, temperature and model as a real scoring run.

    scripts/judge_variance.py --repeats 5 --out private/docs/judge-variance.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.score_validation import judge_arm  # noqa: E402

TRANSCRIPTS = Path("docs/labels/sonnet-transcripts.json")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--model")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if not args.out.startswith("private/"):
        print("--out must be under private/: judgements quote real transcripts", file=sys.stderr)
        return 2
    model = args.model
    if not model:
        from matrix_studio.settings import get_settings

        model = get_settings().litellm_model

    corpus = json.loads(TRANSCRIPTS.read_text())
    rows, cost = {}, 0.0
    for rid, entry in corpus.items():
        conv = entry["conversation"]
        judged = await asyncio.gather(*(judge_arm("transcript_1", conv, model) for _ in range(args.repeats)))
        cost += sum(j.get("_judge_cost_usd", 0.0) for j in judged)
        failed = [j for j in judged if "distinct_positions" not in j]
        if failed:
            # A judgement that did not parse is reported, never averaged around: a spread computed over
            # four of five repeats is a different measurement from the one pre-registered.
            print(f"{rid}  {len(failed)} of {args.repeats} judgements FAILED: "
                  f"{failed[0].get('error') or str(failed[0])[:120]}")
        judged = [j for j in judged if "distinct_positions" in j]
        rows[rid] = judged
        if not judged:
            continue
        ints = {k for j in judged for k, v in j.items() if isinstance(v, int) and not k.startswith("_")}
        spreads = {k: max(j.get(k, 0) for j in judged) - min(j.get(k, 0) for j in judged) for k in sorted(ints)}
        dp = [j.get("distinct_positions") for j in judged]
        print(f"{rid}  distinct_positions {dp} spread {max(dp) - min(dp)}")
        print(f"          other spreads {spreads}")
    dps = {rid: [j["distinct_positions"] for j in judged] for rid, judged in rows.items() if judged}
    spreads = {rid: max(v) - min(v) for rid, v in dps.items()}
    worst = max(spreads.values(), default=-1)
    Path(args.out).write_text(json.dumps({"repeats": args.repeats, "model": model,
                                          "distinct_positions": dps, "spreads": spreads,
                                          "cost_usd": round(cost, 4), "judgements": rows}, indent=1))
    print(f"\nworst spread {worst} over {len(dps)} transcript(s) × {args.repeats}; ${cost:.4f}")
    print("distinct_positions is " + ("UNUSABLE at one judgement per run (spread >= 2): future comparisons "
                                      "must judge each run >= 3 times and use the median"
                                      if worst >= 2 else
                                      "stable enough at one judgement per run (every spread <= 1)"))
    median = {rid: statistics.median(v) for rid, v in dps.items()}
    print("medians", median)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
