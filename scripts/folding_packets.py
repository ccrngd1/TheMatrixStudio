# SPDX-License-Identifier: Apache-2.0
"""Anonymous judging packets for docs/studies/EVIDENCE-LEAN-FOLDING.md: defended positions and final turns, no arm.

    AWS_REGION=us-east-1 DATA_BUCKET=... scripts/folding_packets.py --owner SUB --runs RUN ... \\
        --packets private/labels/folding-packets.md --key private/labels/folding-key.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix_studio import export as ex  # noqa: E402
from matrix_studio.storage import Database  # noqa: E402

DEFENDED = {"firm", "non-negotiable", "requires-escalation"}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--runs", nargs="+", required=True)
    ap.add_argument("--packets", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--seed", type=int, default=20260929)
    args = ap.parse_args()
    if not (args.packets.startswith("private/") and args.key.startswith("private/")):
        print("packets and key must be under private/", file=sys.stderr)
        return 2
    db = Database(table_prefix=os.environ.get("TABLE_PREFIX", "matrix-studio"),
                  bucket=os.environ["DATA_BUCKET"], region=os.environ["AWS_REGION"])
    await db.connect()
    store = db.for_owner(args.owner)
    runs = list(args.runs)
    random.Random(args.seed).shuffle(runs)
    key, out = {}, []
    for i, rid in enumerate(runs, start=1):
        label = f"P{i:02d}"
        key[label] = rid
        run = await store.get_run(rid)
        cast = json.loads(run.get("cast_json") or "[]")
        transcript = (await ex.run_model(store, run))["transcript"]
        out.append(f"# {label}\n\n## Defended positions\n")
        for member in cast:
            for vp in ((member.get("structured") or {}).get("viewpoints") or []):
                if str(vp.get("firmness") or "") in DEFENDED:
                    shifts = "; ".join(vp.get("evidence_that_shifts") or []) or "(none stated)"
                    out.append(f"- **{member.get('name')}** [{vp.get('firmness')}] {vp.get('position')}\n"
                               f"  - moves only if: {shifts}\n")
        out.append("\n## Final 10 turns\n")
        last = max((t["turn"] or 0) for t in transcript)
        for t in transcript:
            if (t["turn"] or 0) > last - 10:
                out.append(f"**{t['turn']} {t['speaker']}:** {t['message']}\n")
        out.append("\n")
    Path(args.packets).write_text("\n".join(out))
    Path(args.key).write_text(json.dumps(key, indent=1))
    print(f"{len(runs)} packets written; key kept separately")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
