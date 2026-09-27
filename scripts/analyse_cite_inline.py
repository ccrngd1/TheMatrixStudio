#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Score the cite-inline comparison against the criteria in docs/CITE-INLINE.md.

Written before the runs finished and not tuned afterwards. Reads the deployed store; creates nothing.

    AWS_REGION=us-east-1 DATA_BUCKET=... scripts/analyse_cite_inline.py --owner SUB \\
        --off RUN_ID RUN_ID --on RUN_ID RUN_ID
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

from matrix_studio.storage import Database  # noqa: E402

CITED = {"firsthand", "secondhand"}


async def arm(store, run_ids):
    in_view = cited = attributive = unverified = 0
    words = []
    for rid in run_ids:
        run = await store.get_run(rid)
        status = (run or {}).get("status")
        parked = set()
        for row in await store.get_events(rid):
            p = row["payload"]
            p = json.loads(p) if isinstance(p, str) else p
            if row["event_type"] == "document.retrieved" and p.get("passages"):
                parked.add((row["turn"], row["agent_name"]))
            elif row["event_type"] == "agent.response":
                words.append(len((p.get("message") or "").split()))
                if (row["turn"], row["agent_name"]) not in parked:
                    continue
                in_view += 1
                marks = p.get("citation_provenance") or []
                if any(m.get("kind") in CITED for m in marks):
                    cited += 1
                for m in marks:
                    if m.get("attributive"):
                        attributive += 1
                        unverified += m.get("kind") == "unverified"
        print(f"  {rid[:8]} status={status}")
    return {
        "in_view": in_view,
        "cite_rate": cited / in_view if in_view else None,
        "attributive": attributive,
        "unverified_share": unverified / attributive if attributive else None,
        "median_words": statistics.median(words) if words else None,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--off", nargs="+", required=True)
    ap.add_argument("--on", nargs="+", required=True)
    args = ap.parse_args()
    db = Database(table_prefix=os.environ.get("TABLE_PREFIX", "matrix-studio"),
                  bucket=os.environ["DATA_BUCKET"], region=os.environ["AWS_REGION"])
    await db.connect()
    store = db.for_owner(args.owner)
    print("off:"); off = await arm(store, args.off)
    print("on:"); on = await arm(store, args.on)
    print(json.dumps({"off": off, "on": on}, indent=2))

    primary = (on["cite_rate"] or 0) >= 0.50 and (off["cite_rate"] or 0) <= 0.10
    guard1 = on["unverified_share"] is None or on["unverified_share"] <= 0.20
    guard2 = bool(off["median_words"]) and abs(on["median_words"] - off["median_words"]) <= 0.25 * off["median_words"]
    print(f"primary (on >= 0.50, off <= 0.10): {'MET' if primary else 'MISSED'}")
    print(f"guardrail 1 (unverified <= 0.20):   {'MET' if guard1 else 'MISSED'}")
    print(f"guardrail 2 (words within ±25%):    {'MET' if guard2 else 'MISSED'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
