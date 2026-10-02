# SPDX-License-Identifier: Apache-2.0
"""Run the moderator's assumption check on stored transcripts, recording nothing to the runs.

For docs/studies/MODERATOR-ASSUMPTIONS.md, addendum 2: fresh proposals to label, so a classifier can be tried
again on data it was not fitted to. At every point the check would have fired live (after every
``--every``-th completed turn) it is given the conversation up to that point and the shadow ledger, and
its answer is put through the same verbatim-asks and repeat checks the engine uses. No cap.

    AWS_REGION=us-east-1 DATA_BUCKET=... scripts/shadow_assumption_checks.py --owner SUB \\
        --runs RUN ... --out private/labels/shadow-proposals.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix_studio import assumptions as am  # noqa: E402
from matrix_studio import export as ex  # noqa: E402
from matrix_studio.jsonio import extract_json_object  # noqa: E402
from matrix_studio.lazy_litellm import litellm  # noqa: E402
from matrix_studio.models import ModelSet, model_for  # noqa: E402
from matrix_studio.settings import get_settings  # noqa: E402
from matrix_studio.storage import Database  # noqa: E402


def half(key: str) -> str:
    """The pre-registered split: working or held-out, by the key's hash."""
    return "working" if int(hashlib.sha1(key.encode()).hexdigest(), 16) % 2 == 0 else "held-out"


async def shadow(store, rid: str, every: int):
    run = await store.get_run(rid)
    config = json.loads(run.get("config_json") or "{}")
    model = model_for(ModelSet.from_config(config), "speaker_selection") or get_settings().litellm_model
    transcript = (await ex.run_model(store, run))["transcript"]
    last = max((m["turn"] or 0 for m in transcript), default=0)
    ledger, out, cost = [], [], 0.0
    for after in range(every, last, every):
        conversation = [{"speaker": m["speaker"], "content": m["message"]} for m in transcript if (m["turn"] or 0) <= after]
        response = await litellm.acompletion(
            model=model, messages=am.propose_messages(run.get("topic", ""), conversation, ledger),
            temperature=0.2, response_format={"type": "json_object"}, drop_params=True,
        )
        cost += float((getattr(response, "_hidden_params", None) or {}).get("response_cost") or 0.0)
        parsed = extract_json_object((response.choices[0].message.content or "").strip())
        proposal, why_not = am.parse_proposal(parsed, conversation[-am.RECENT_MESSAGES:], ledger)
        key = f"{rid[:8]}:{after}"
        row = {"key": key, "run": rid, "after_turn": after, "half": half(key), "passed": proposal is not None,
               "rejected": why_not, "raw": (parsed or {}).get("assumption") if isinstance(parsed, dict) else None}
        if proposal is not None:
            row.update(statement=proposal["statement"], gap=proposal["gap"])
            ledger.append(am.Assumption(am.next_id(ledger), proposal["statement"], proposal["basis"], am.MODERATOR, after))
        out.append(row)
    return run.get("topic", ""), out, cost


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--runs", nargs="+", required=True)
    ap.add_argument("--every", type=int, default=am.DEFAULT_EVERY)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if not args.out.startswith("private/"):
        print("--out must be under private/: proposals quote real briefs", file=sys.stderr)
        return 2
    db = Database(table_prefix=os.environ.get("TABLE_PREFIX", "matrix-studio"),
                  bucket=os.environ["DATA_BUCKET"], region=os.environ["AWS_REGION"])
    await db.connect()
    store = db.for_owner(args.owner)
    rows, total, topics = [], 0.0, {}
    for rid in args.runs:
        topic, got, cost = await shadow(store, rid, args.every)
        topics[rid] = topic
        rows += got
        total += cost
        print(f"{rid[:8]}: {len(got)} checks, {sum(r['passed'] for r in got)} passed, ${cost:.4f}")
    Path(args.out).write_text(json.dumps({"topics": topics, "rows": rows, "cost_usd": total}, indent=1))
    passed = [r for r in rows if r["passed"]]
    print(f"total: {len(rows)} checks, {len(passed)} passed "
          f"({sum(r['half'] == 'working' for r in passed)} working / {sum(r['half'] == 'held-out' for r in passed)} held-out), ${total:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
