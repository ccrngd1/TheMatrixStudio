#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Phase 6 step 8: grants and revocation, against the real account.

The phase's "done when", from the plan:

  1. one document, uploaded once, is searchable by **two personas in two different
     conversations**;
  2. a revoked grant stops working **at query time**, not just at binding time.

Both are unit-tested. This exists because the unit tests run against `moto`, and `moto`
does not implement `QueryVectors` at all — so the one thing that decides whether a
grantee actually gets passages back is stubbed there and cannot be otherwise. Everything
below goes through the real S3 Vectors service and the real DynamoDB.

It is also the check that `dynamodb:LeadingKeys` has not quietly broken sharing. §2 calls
the shared KB the first documented exception to the isolation model, and an exception to
an isolation invariant is where breaches live — so the negative cases are asserted first
and by number, and a check that cannot be performed is a FAILURE rather than a skip.

Usage:
    AWS_PROFILE=... TABLE_PREFIX=... DATA_BUCKET=... VECTOR_BUCKET=... \
        python scripts/verify_kb_grants.py

Creates its own KBs, users and runs with a `verify-kb-` prefix and leaves them: deleting
in a live account is what §4.5 declines to do without a go-ahead. Re-runnable.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix_studio.bindings import searchable_for_turn  # noqa: E402
from matrix_studio.storage import Database  # noqa: E402
from matrix_studio.storage.vectors import ensure_kb_index, kb_index_name  # noqa: E402

OWNER = "verify-kb-owner-1111"
GRANTEE = "verify-kb-grantee-2222"
STRANGER = "verify-kb-stranger-3333"

PASSED: List[str] = []
FAILED: List[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    (PASSED if ok else FAILED).append(label)
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))


async def seed_kb(db: Database, name: str, text: str, vector: List[float]) -> str:
    """A KB owned by OWNER holding one real vector in a real index."""
    bound = db.for_owner(OWNER)
    existing = [
        kb for kb in await bound.list_knowledge_bases(owner_sub=OWNER)
        if kb.get("name") == name
    ]
    kb = existing[0] if existing else await bound.create_knowledge_base(name, owner_sub=OWNER)

    bucket = os.environ["VECTOR_BUCKET"]
    client = db._vectors_client()
    index = kb_index_name(kb["id"], db.table_prefix)
    await ensure_kb_index(client, bucket, index)
    await db._call(
        client.put_vectors,
        vectorBucketName=bucket,
        indexName=index,
        vectors=[{
            "key": f"{name}-doc:0",
            "data": {"float32": vector},
            "metadata": {
                "kb_id": kb["id"],
                "owner_sub": OWNER,
                "document_id": f"{name}-doc",
                "ordinal": 0,
                "text": text,
                "title": f"{name}.md",
            },
        }],
    )
    return kb["id"]


def unit(width: int = 1024) -> List[float]:
    """A unit vector along the first axis, at the index's real width."""
    v = [0.0] * width
    v[0] = 1.0
    return v


async def searchable(db: Database, run: Dict[str, Any], persona: Optional[str], sub: str,
                     groups: Optional[List[str]] = None) -> List[str]:
    return await searchable_for_turn(db.for_owner(sub), run, persona, sub, groups)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--table-prefix", default=None)
    args = parser.parse_args()

    prefix = args.table_prefix or os.environ.get("TABLE_PREFIX", "matrix-studio")
    for name in ("VECTOR_BUCKET", "DATA_BUCKET"):
        if not os.environ.get(name):
            raise SystemExit(f"{name} must be set (see the stack outputs).")

    db = Database(table_prefix=prefix, bucket=os.environ["DATA_BUCKET"], region=args.region)
    await db.connect()
    stamp = int(time.time())
    try:
        print("Seeding one shared knowledge base with one real vector …")
        kb_id = await seed_kb(db, f"verify-kb-shared-{stamp}", "the shared passage", unit())
        print(f"  KB {kb_id}, owner {OWNER}\n")

        # ------------------------------------------------------------------ #
        print("1. Negative cases — a binding is not permission")
        # A run in the GRANTEE's account that binds the owner's KB, before any grant.
        grantee_db = db.for_owner(GRANTEE)
        run_id = f"verify-kb-run-{stamp}"
        await grantee_db.create_run(
            run_id=run_id, topic="egress inspection", name=run_id,
            cast=[{"name": "Ada"}, {"name": "Dan"}],
            config={"knowledge_bases": [kb_id]},
        )
        run = await grantee_db.get_run(run_id)

        allowed = await searchable(db, run, "Ada", GRANTEE)
        check("a bound KB with no grant is not searchable", allowed == [], f"{allowed}")

        rows, failed = await grantee_db.vector_search_kbs(unit(), [kb_id], k=3)
        check(
            "and the authorised set is what gates it, not the index",
            bool(rows) and not failed,
            "the index itself is readable, so the refusal above came from the grant "
            "check rather than from an unreachable index",
        )

        stranger_allowed = await searchable(db, run, "Ada", STRANGER)
        check("a third party is not searchable either", stranger_allowed == [], f"{stranger_allowed}")

        # ------------------------------------------------------------------ #
        print("\n2. A grant makes it work — at query time, with no change to the run")
        await db.for_owner(OWNER).grant_kb(kb_id, user=GRANTEE, granted_by=OWNER)
        allowed = await searchable(db, run, "Ada", GRANTEE)
        check("the same binding now resolves", allowed == [kb_id], f"{allowed}")

        rows, failed = await grantee_db.vector_search_kbs(unit(), allowed, k=3)
        check(
            "and returns the passage from the real index",
            len(rows) == 1 and rows[0]["content"] == "the shared passage",
            f"{[r.get('content') for r in rows]}",
        )
        check(
            "carrying its title, which the grantee cannot look up",
            bool(rows) and rows[0]["title"].endswith(".md"),
            f"{rows[0]['title'] if rows else '—'}",
        )

        # ------------------------------------------------------------------ #
        print("\n3. Two personas in two different conversations — the plan's 'done when'")
        second_id = f"verify-kb-run2-{stamp}"
        await grantee_db.create_run(
            run_id=second_id, topic="a different conversation", name=second_id,
            cast=[{"name": "Priya"}],
            config={"knowledge_bases": [kb_id]},
        )
        second = await grantee_db.get_run(second_id)

        first_persona = await searchable(db, run, "Dan", GRANTEE)
        other_persona = await searchable(db, second, "Priya", GRANTEE)
        check(
            "one document, uploaded once, searchable by two personas in two runs",
            first_persona == [kb_id] and other_persona == [kb_id],
            f"run1/Dan={first_persona}, run2/Priya={other_persona}",
        )
        # And it really is ONE copy: the KB index holds a single vector.
        page = db._vectors_client().list_vectors(
            vectorBucketName=os.environ["VECTOR_BUCKET"],
            indexName=kb_index_name(kb_id, db.table_prefix),
        )
        check(
            "and there is exactly one stored copy of it",
            len(page.get("vectors", [])) == 1,
            f"{len(page.get('vectors', []))} vector(s)",
        )

        # ------------------------------------------------------------------ #
        print("\n4. Revocation takes effect at QUERY time")
        await db.for_owner(OWNER).revoke_kb(kb_id, user=GRANTEE)
        after = await searchable(db, run, "Ada", GRANTEE)
        check("a revoked grant stops resolving immediately", after == [], f"{after}")

        still_there = await grantee_db.get_run(run_id)
        check(
            "without the run being touched — the binding is still in its config",
            kb_id in (still_there.get("config_json") or ""),
            "so the refusal is a query-time decision, not a rewritten run",
        )

        other_after = await searchable(db, second, "Priya", GRANTEE)
        check("and in the other conversation too", other_after == [], f"{other_after}")

        # ------------------------------------------------------------------ #
        print("\n5. The owner never needed a grant")
        owner_run_id = f"verify-kb-ownerrun-{stamp}"
        owner_db = db.for_owner(OWNER)
        await owner_db.create_run(
            run_id=owner_run_id, topic="mine", name=owner_run_id,
            cast=[{"name": "Ada"}], config={"knowledge_bases": [kb_id]},
        )
        owner_run = await owner_db.get_run(owner_run_id)
        mine = await searchable(db, owner_run, "Ada", OWNER)
        check("the KB's owner can search it with no grant row", mine == [kb_id], f"{mine}")

        # ------------------------------------------------------------------ #
        print("\n6. Group grants")
        await db.for_owner(OWNER).grant_kb(kb_id, group="verify-kb-team", granted_by=OWNER)
        with_group = await searchable(db, run, "Ada", GRANTEE, ["verify-kb-team"])
        check("a group grant resolves for a member", with_group == [kb_id], f"{with_group}")
        without = await searchable(db, run, "Ada", GRANTEE, ["some-other-team"])
        check("and not for a non-member", without == [], f"{without}")
        no_groups = await searchable(db, run, "Ada", GRANTEE, [])
        check("and not for a caller with no groups at all", no_groups == [], f"{no_groups}")

    finally:
        await db.close()

    print(f"\n{len(PASSED)}/{len(PASSED) + len(FAILED)} checks passed")
    if FAILED:
        for label in FAILED:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
