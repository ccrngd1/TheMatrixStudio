#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Phase 6 step 7: prove the KB fan-out returns EXACTLY what one index returned.

`docs/project/PHASE6-KB-DESIGN.md` §5 asks for recall to be re-measured across two KBs, on the
grounds that "querying two indexes and merging could rank differently from one index with
a filter" — and that this should be measured rather than reasoned about, because the
`distance_to_cosine` bug is what happens when a metric is reasoned about.

**This checks something strictly stronger, for free.** §4.2 makes the vectors
bit-identical, so the only thing that could differ is the fan-out and merge. Rather than
re-deriving recall@1 and recall@5 and comparing them to 0.650 / 0.825 — two numbers, each
with sampling noise, needing a judgement about whether a difference matters — this
compares the **top-k result sets themselves**, per query, between:

    A. one index, metadata-filtered      (`vector_search`, the pre-Phase-6 path)
    B. two KB indexes, fanned out and merged  (`vector_search_kbs`)

If A and B return the same passages in the same order for every query, then every recall
metric is identical *by definition* and there is nothing left to measure. A mismatch, by
contrast, tells you the exact query and the exact passage that moved — which a pair of
aggregate recall figures never would.

It also costs **nothing at Bedrock**: the query vectors are drawn from the corpus's own
stored vectors, so no embedding calls are made. That buys hundreds of queries instead of
the 40 the recall harness affords, over the same distribution the corpus occupies.

Why it must be a script rather than a test: `moto` does not implement `QueryVectors`, so
the ranking this verifies cannot be exercised in the suite at all.

Usage:
    AWS_PROFILE=... python scripts/verify_kb_fanout_equivalence.py --run-id eval-...
    ... --queries 200 --k 5

Exits non-zero on any mismatch. Leaves the two temporary KBs in place — deleting an index
in a live account is irreversible (§4.5) — and prints their ids so they can be removed on
an explicit go-ahead.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix_studio.storage import Database  # noqa: E402
from matrix_studio.storage.vectors import ensure_kb_index, kb_index_name  # noqa: E402


async def all_vectors(db: Database, run_id: str, owner: str) -> List[Dict[str, Any]]:
    """Every stored vector for a run, with data and metadata, from the shared index."""
    bucket = os.environ["VECTOR_BUCKET"]
    client = db._vectors_client()
    out: List[Dict[str, Any]] = []
    token = None
    while True:
        kwargs: Dict[str, Any] = {
            "vectorBucketName": bucket,
            "indexName": db._vector_index(),
            "returnData": True,
            "returnMetadata": True,
            "maxResults": 500,
        }
        if token:
            kwargs["nextToken"] = token
        page = await db._call(client.list_vectors, **kwargs)
        for entry in page.get("vectors", []):
            meta = entry.get("metadata") or {}
            if str(meta.get("run_id") or "") == run_id and str(
                meta.get("owner_sub") or ""
            ) == owner:
                out.append(entry)
        token = page.get("nextToken")
        if not token:
            return out


async def build_split_kbs(
    db: Database, owner: str, entries: List[Dict[str, Any]], suffix: str
) -> Tuple[str, str]:
    """Two KBs holding the corpus split by DOCUMENT, alternating.

    Split by document rather than by vector, because that is how a real corpus divides —
    a document lives in exactly one KB (§1). Splitting by vector would put one document's
    chunks in two collections, which the model forbids and which would make the test
    easier than reality.
    """
    bucket = os.environ["VECTOR_BUCKET"]
    client = db._vectors_client()
    bound = db.for_owner(owner)

    doc_ids = sorted({str((e.get("metadata") or {}).get("document_id") or "") for e in entries})
    left_docs = set(doc_ids[0::2])
    right_docs = set(doc_ids[1::2])
    print(f"  split {len(doc_ids)} documents into {len(left_docs)} + {len(right_docs)}")

    kb_ids = []
    for label, docs in (("a", left_docs), ("b", right_docs)):
        name = f"fanout-check-{suffix}-{label}"
        existing = [
            kb for kb in await bound.list_knowledge_bases(owner_sub=owner)
            if kb.get("name") == name
        ]
        kb = existing[0] if existing else await bound.create_knowledge_base(
            name, owner_sub=owner
        )
        index = kb_index_name(kb["id"], db.table_prefix)
        await ensure_kb_index(client, bucket, index)

        payload = []
        for entry in entries:
            meta = dict(entry.get("metadata") or {})
            doc_id = str(meta.get("document_id") or "")
            if doc_id not in docs:
                continue
            payload.append({
                "key": entry["key"],
                "data": entry["data"],
                "metadata": {
                    "kb_id": kb["id"],
                    "owner_sub": owner,
                    "document_id": doc_id,
                    "ordinal": int(meta.get("ordinal") or 0),
                    "text": str(meta.get("text") or ""),
                    "title": str(meta.get("title") or doc_id),
                },
            })
        for start in range(0, len(payload), 500):
            await db._call(
                client.put_vectors,
                vectorBucketName=bucket,
                indexName=index,
                vectors=payload[start:start + 500],
            )
        print(f"  KB {kb['id']} ({name}): {len(payload)} vector(s)")
        kb_ids.append(kb["id"])
    return kb_ids[0], kb_ids[1]


def identity(row: Dict[str, Any]) -> Tuple[str, int]:
    return str(row.get("document_id") or ""), int(row.get("ordinal") or 0)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, help="a run whose corpus is embedded")
    parser.add_argument("--owner", default="local-single-user")
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--table-prefix", default=None)
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    prefix = args.table_prefix or os.environ.get("TABLE_PREFIX", "matrix-studio")
    if not os.environ.get("VECTOR_BUCKET") or not os.environ.get("DATA_BUCKET"):
        raise SystemExit("VECTOR_BUCKET and DATA_BUCKET must be set.")

    db = Database(table_prefix=prefix, bucket=os.environ["DATA_BUCKET"], region=args.region)
    await db.connect()
    try:
        print(f"Reading vectors for run {args.run_id} …")
        entries = await all_vectors(db, args.run_id, args.owner)
        if not entries:
            raise SystemExit(
                f"no stored vectors for run {args.run_id!r} owned by {args.owner!r}. "
                "A comparison over an empty corpus would report success having checked "
                "nothing."
            )
        print(f"  {len(entries)} vector(s)")

        left, right = await build_split_kbs(db, args.owner, entries, args.run_id[-6:])

        # Query vectors drawn from the corpus, evenly spaced so they are not all from the
        # same document. Free, and in the distribution the corpus actually occupies.
        step = max(1, len(entries) // args.queries)
        queries = entries[::step][: args.queries]
        print(f"\nComparing {len(queries)} queries at k={args.k} …")

        bound = db.for_owner(args.owner)
        mismatches: List[str] = []
        empty_single = 0
        for i, entry in enumerate(queries):
            vector = entry["data"]["float32"]

            single = await bound.vector_search(
                run_id=args.run_id, vector=vector, persona_name=None, k=args.k
            )
            # `per_kb_floor=0` keeps this an exactness test. The default floor of 1
            # reserves a slot per collection and therefore CAN differ from a single index
            # by design — that is retrieval policy (see `merge_with_source_floor`), and
            # mixing it in here would turn a proof about the merge into a test of the
            # policy, losing the property this script exists to establish.
            fanned, failed = await bound.vector_search_kbs(
                vector, [left, right], k=args.k, per_kb_floor=0
            )
            if failed:
                raise SystemExit(f"KB index unavailable: {failed}. Cannot compare.")

            if not single:
                empty_single += 1

            a = [identity(r) for r in single]
            b = [identity(r) for r in fanned]
            if a != b:
                mismatches.append(
                    f"query {i} ({identity(entry.get('metadata') or {})}):\n"
                    f"    one index: {a}\n"
                    f"    fan-out:   {b}"
                )

        print()
        if empty_single == len(queries):
            raise SystemExit(
                "every single-index query returned nothing, so the comparison proved "
                "nothing. Check the run id and owner."
            )

        if mismatches:
            print(f"MISMATCH on {len(mismatches)} of {len(queries)} queries:\n")
            for line in mismatches[:10]:
                print(line)
            if len(mismatches) > 10:
                print(f"  … and {len(mismatches) - 10} more")
            print(
                "\nThe fan-out does NOT reproduce the single index. Every recall figure "
                "measured before Phase 6 is therefore not transferable."
            )
            return 1

        print(
            f"IDENTICAL: all {len(queries)} queries returned the same top-{args.k} "
            "passages in the same order from both paths."
        )
        print(
            "Recall across two KBs is therefore identical to the single-index "
            "measurement (fts 0.125 / vector 0.650 / hybrid 0.450 at recall@1; "
            "0.650 / 0.825 / 0.875 at recall@5) by construction, not by re-sampling."
        )
        print(f"\nTemporary KBs left in place: {left}, {right}")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
