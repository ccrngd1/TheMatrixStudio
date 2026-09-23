#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Measure claim-clustering recall against a real ensemble, without paying for a whole report.

## Why this exists

Iterating on the clustering by regenerating a live report costs **$1.03 and nine minutes** per
attempt, and eight of those nine minutes are re-extracting transcripts that have not changed.
So the extractions are cached to a file once, and each clustering experiment afterwards is one
model call per kind.

## What it reports, and what it cannot

**Recall, measured.** How many clusters, how wide the widest one is, how many span every run,
and how many invariant demands each persona ends up with. `invariant_demands` is the number
that matters most: it requires a demand in ALL runs a persona appeared in, and on the first
live ensemble it was zero for all six personas across five runs of an identical brief.

**Precision, NOT measured.** There is no ground truth here, and the dangerous failure —
over-merging, which deletes a dissent invisibly — cannot be detected by counting. So this
prints the widest merges and the ones whose member phrasings share the fewest words, for a
human to read. A clustering that scores well on recall and looks wrong in that list is worse
than the thing it replaced.

Usage:
    export AWS_REGION=us-east-1 DATA_BUCKET=... TABLE_PREFIX=matrix-studio
    scripts/tune_clustering.py --owner SUB --ensemble ENS_ID            # extract + cache + run
    scripts/tune_clustering.py --owner SUB --ensemble ENS_ID --variant two-pass
    scripts/tune_clustering.py --owner SUB --ensemble ENS_ID --review 12

Costs real money: one call per kind per run, plus the extractions the first time only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix_studio import ensemble, ensemble_reporting  # noqa: E402
from matrix_studio.storage import Database  # noqa: E402


async def _extractions(
    owner: str, ensemble_id: str, cache: Path, refresh: bool
) -> Dict[str, Any]:
    """`{run name: extraction}` for every usable member, from the cache or from the model.

    Cached as plain JSON rather than pickled views: the cache outlives code changes to
    `RunView`, and a stale cache that silently fails to load would send someone re-paying for
    nine extractions while believing they were free.
    """
    if cache.exists() and not refresh:
        loaded = json.loads(cache.read_text())
        if loaded.get("ensemble_id") == ensemble_id:
            print(f"cache  {cache} ({len(loaded['runs'])} runs)")
            return loaded
        print(f"cache  {cache} is for a different ensemble; re-extracting")

    db = Database(
        table_prefix=os.environ.get("TABLE_PREFIX", "matrix-studio"),
        bucket=os.environ["DATA_BUCKET"],
        region=os.environ["AWS_REGION"],
    )
    await db.connect()
    try:
        owned = db.for_owner(owner)
        members = await owned.list_ensemble_members(ensemble_id)
        present = [m["run_id"] for m in members if m["run"]]
        views = await ensemble.collect(owned, present)
        print(f"extracting {len(views)} transcript(s) — this is the expensive part, once")
        results = await asyncio.gather(*(
            ensemble.extract_positions(v) for v in views
        ))
        runs = {}
        cost = 0.0
        for view, positions in zip(views, results):
            cost += float(positions.get("_cost_usd") or 0.0)
            runs[view.name] = positions
        body = {"ensemble_id": ensemble_id, "runs": runs, "extraction_cost_usd": cost}
        cache.write_text(json.dumps(body, indent=1))
        print(f"cached {cache}  (${cost:.4f})")
        return body
    finally:
        await db.close()


def _harvest(runs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The same flat, ordered harvest the report builds, from cached extractions.

    Mirrors `ensemble_reporting._harvest_claims` in ORDER, because the clustering is indexed by
    position and a different order here would measure something the report does not do.
    """
    out: List[Dict[str, Any]] = []
    for name in sorted(runs):
        positions = runs[name]
        for persona in (positions.get("personas") or []):
            for kind, field in (("demand", "demands"), ("refusal", "refusals")):
                for raw in persona.get(field) or []:
                    text = str(raw).strip()
                    if text:
                        out.append({"kind": kind, "text": text, "run": name,
                                    "persona": persona.get("name")})
        for raw in positions.get("unresolved") or []:
            text = str(raw).strip()
            if text:
                out.append({"kind": "unresolved", "text": text, "run": name, "persona": None})
    return out


def _words(text: str) -> set:
    return set(ensemble._normalise(text).split())


def _overlap(texts: List[str]) -> float:
    """Lowest pairwise word overlap in a cluster. A crude "is this merge suspicious" signal.

    Low overlap does not prove a bad merge — "a live exam" and "synchronous licensed
    examination" share almost nothing and are the same requirement — which is exactly why this
    ranks a review list rather than deciding anything.
    """
    worst = 1.0
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            a, b = _words(texts[i]), _words(texts[j])
            if a or b:
                worst = min(worst, len(a & b) / max(len(a | b), 1))
    return worst


def measure(
    claims: List[Dict[str, Any]],
    keys: Optional[Dict[int, Tuple[str, str]]],
    total_runs: int,
) -> Dict[str, Any]:
    """Recall numbers for one clustering, plus the material for a precision review."""
    groups: Dict[Any, List[int]] = defaultdict(list)
    label_of: Dict[Any, str] = {}
    for i, claim in enumerate(claims):
        if keys is not None:
            key, label = keys[i]
        else:
            key, label = ensemble._normalise(claim["text"]), claim["text"]
        groups[key].append(i)
        label_of.setdefault(key, label)

    spans = {k: len({claims[i]["run"] for i in v}) for k, v in groups.items()}
    demand_groups = {
        k: v for k, v in groups.items() if claims[v[0]]["kind"] == "demand"
    }

    # `invariant_demands`, computed exactly as `per_persona` does: a demand is invariant for a
    # persona when it appears in every run that persona was extracted in.
    appeared = Counter()
    for claim in claims:
        if claim["persona"]:
            appeared[claim["persona"]] = appeared[claim["persona"]]
    persona_runs: Dict[str, set] = defaultdict(set)
    for claim in claims:
        if claim["persona"]:
            persona_runs[claim["persona"]].add(claim["run"])
    per_persona_key_runs: Dict[Tuple[str, Any], set] = defaultdict(set)
    for i, claim in enumerate(claims):
        if claim["kind"] != "demand" or not claim["persona"]:
            continue
        key = keys[i][0] if keys is not None else ensemble._normalise(claim["text"])
        per_persona_key_runs[(claim["persona"], key)].add(claim["run"])
    invariant = Counter()
    for (persona, _key), runs in per_persona_key_runs.items():
        if len(runs) == len(persona_runs[persona]):
            invariant[persona] += 1

    review = sorted(
        (
            {
                "label": label_of[k],
                "span": spans[k],
                "overlap": round(_overlap([claims[i]["text"] for i in v]), 3),
                "texts": [f"{claims[i]['run']}: {claims[i]['text']}" for i in v],
            }
            for k, v in groups.items() if len(v) > 1
        ),
        key=lambda r: (r["overlap"], -r["span"]),
    )

    return {
        "claims": len(claims),
        "clusters": len(groups),
        "merged": sum(1 for v in groups.values() if len(v) > 1),
        "widest": max(spans.values()) if spans else 0,
        "spanning_all": sum(1 for s in spans.values() if s == total_runs),
        "demand_clusters_spanning_all": sum(
            1 for k in demand_groups if spans[k] == total_runs
        ),
        "invariant_demands": dict(invariant),
        "unresolved_top": max(
            (spans[k] for k, v in groups.items() if claims[v[0]]["kind"] == "unresolved"),
            default=0,
        ),
        "review": review,
    }


def _print(name: str, m: Dict[str, Any], total_runs: int) -> None:
    print(f"\n=== {name} ===")
    print(f"  {m['claims']} claims -> {m['clusters']} clusters ({m['merged']} merged)")
    print(f"  widest cluster spans {m['widest']}/{total_runs} runs; "
          f"{m['spanning_all']} span all {total_runs} "
          f"({m['demand_clusters_spanning_all']} of them demands)")
    print(f"  top unresolved group: {m['unresolved_top']}/{total_runs} runs")
    inv = m["invariant_demands"]
    print(f"  invariant demands: {inv or 'NONE for any persona'}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", required=True)
    ap.add_argument("--ensemble", required=True)
    ap.add_argument("--cache", default="/tmp/clustering-extractions.json")
    ap.add_argument("--refresh", action="store_true", help="re-extract even if cached")
    ap.add_argument("--variant", action="append", default=[],
                    choices=["none", "one-pass", "two-pass"],
                    help="repeatable; default: none and one-pass")
    ap.add_argument("--review", type=int, default=0,
                    help="print the N most suspicious merges for a human to check")
    args = ap.parse_args()

    body = await _extractions(
        args.owner, args.ensemble, Path(args.cache), args.refresh
    )
    runs = body["runs"]
    claims = _harvest(runs)
    pairs = [(c["kind"], c["text"]) for c in claims]
    total_runs = len(runs)
    print(f"\n{len(claims)} claims across {total_runs} runs: "
          f"{dict(Counter(c['kind'] for c in claims))}")

    variants = args.variant or ["none", "one-pass"]
    results: Dict[str, Dict[str, Any]] = {}

    if "none" in variants:
        results["none (text matching)"] = measure(claims, None, total_runs)
        _print("none (text matching)", results["none (text matching)"], total_runs)

    if "one-pass" in variants:
        clusters = await ensemble.cluster_claims(pairs)
        if not clusters:
            print("\none-pass: clustering returned nothing usable")
        else:
            keys = ensemble.apply_clusters(pairs, clusters)
            cost = float(clusters[0].get("_cost_usd") or 0.0)
            results["one-pass"] = measure(claims, keys, total_runs)
            _print(f"one-pass (${cost:.4f})", results["one-pass"], total_runs)

    if "two-pass" in variants:
        got = await ensemble.cluster_claims_twice(pairs)
        if not got:
            print("\ntwo-pass: clustering returned nothing usable")
        else:
            clusters, cost = got
            keys = ensemble.apply_clusters(pairs, clusters)
            results["two-pass"] = measure(claims, keys, total_runs)
            _print(f"two-pass (${cost:.4f})", results["two-pass"], total_runs)

    if args.review:
        for name, m in results.items():
            if name.startswith("none"):
                continue
            print(f"\n--- {name}: {args.review} most suspicious merges "
                  f"(lowest word overlap first) ---")
            for r in m["review"][: args.review]:
                print(f"\n  [{r['span']} runs, overlap {r['overlap']}] {r['label']}")
                for t in r["texts"]:
                    print(f"      {t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
