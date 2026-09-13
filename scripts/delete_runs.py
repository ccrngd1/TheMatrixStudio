#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Delete runs and everything that belongs to them, across all three stores.

## Why this is a script and not an API route

There is no `delete_run` anywhere in the codebase — not in the storage layer, not as a
route. §7 of the architecture lists "the deletion path across all three stores" as
outstanding work, and it is genuinely more than one delete: a run's data is spread over
six DynamoDB partitions, an S3 prefix and a vector index, and the shapes differ.

Getting it wrong is quiet rather than loud. Miss the **name marker** and the run's name
stays permanently reserved for its owner, so re-creating it fails with a duplicate-name
error naming a run that no longer exists. Miss the **vectors** and a deleted document's
passages are still retrievable. Miss the **S3 bodies** and they cost storage forever with
nothing referencing them. None of those surfaces as an error at deletion time.

## What a run owns

    runs         USER#{sub} / RUN#{id}                 the run row
    runs         USER#{sub} / NAME#{name}              the name-uniqueness marker
    events       USER#{sub} / RUN#{id}#{seq}           the event log
    snapshots    USER#{sub} / RUN#{id}#{turn}          per-turn snapshots
    summaries    RUN#{id}   / …                        the post-run summary
    threads      RUN#{id}   / …                        aside threads
    documents    RUN#{id}   / DOC#{doc}                attachments (+ S3 body each)
    S3           snapshots/{sub}/…, docs/{sub}/…       bodies
    S3 Vectors   matrix-studio-chunks                  one vector per chunk

Snapshot bodies in S3 are found from the snapshot rows rather than guessed, so a key
format change cannot silently orphan them.

Usage:
    python scripts/delete_runs.py --owner SUB                    # dry run
    python scripts/delete_runs.py --owner SUB --apply
    python scripts/delete_runs.py --all-owners --apply --except-owner local-single-user

Dry run by default. This deletes conversation history irreversibly and there is no
undo — the event log IS the run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix_studio.storage import Database  # noqa: E402
from matrix_studio.storage.dynamo import (  # noqa: E402
    _document_sk,
    _name_sk,
    _run_pk,
    _run_prefix,
    _run_sk,
    _user_pk,
)


async def run_rows(db: Database) -> List[Dict[str, Any]]:
    """Every run row in the table, with its owner."""
    return [
        item for item in await db._scan_all("runs")
        if str(item.get("sk", "")).startswith("RUN#")
    ]


async def delete_one(db: Database, run: Dict[str, Any], apply: bool) -> Dict[str, int]:
    """Delete one run's data everywhere. Returns a count per store."""
    run_id, owner = str(run["id"]), str(run.get("owner_sub") or "")
    name = run.get("name")
    counts: Dict[str, int] = defaultdict(int)
    bound = db.for_owner(owner)

    # --- documents, which own S3 bodies and vectors ------------------------------- #
    docs = await bound.list_documents(run_id)
    for doc in docs:
        counts["documents"] += 1
        if apply:
            # `delete_document` already removes the metadata row and the S3 object.
            await bound.delete_document(str(doc["id"]))
    if docs:
        # Vectors are keyed `document_id:ordinal`, so they are derivable from
        # `chunk_count` without listing the index — which is what makes deletion bounded
        # (§4a). `delete_document` does NOT do this today: it predates the vector store.
        #
        # Counted OUTSIDE the `apply` guard, deliberately. The first version counted only
        # when applying, so a dry run under-reported its own scope — which is the exact
        # failure a dry run exists to prevent, and it would have been discovered by the
        # numbers not matching afterwards.
        keys = [
            db._vector_key(str(d["id"]), i)
            for d in docs for i in range(int(d.get("chunk_count") or 0))
        ]
        counts["vectors"] += len(keys)
        bucket = os.environ.get("VECTOR_BUCKET", "")
        if apply and keys and bucket:
            for start in range(0, len(keys), 500):
                try:
                    await db._call(
                        db._vectors_client().delete_vectors,
                        vectorBucketName=bucket,
                        indexName=db._vector_index(),
                        keys=keys[start:start + 500],
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"      ! vectors for {run_id}: {exc}")

    # --- snapshots, whose bodies live in S3 -------------------------------------- #
    snaps = await db._query_all(
        "snapshots",
        KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
        ExpressionAttributeValues={":pk": _user_pk(owner), ":prefix": _run_prefix(run_id)},
    )
    for snap in snaps:
        counts["snapshots"] += 1
        if apply:
            key = snap.get("s3_key")
            if key and db.bucket:
                db._ensure_clients()
                try:
                    await db._call(db._s3.delete_object, Bucket=db.bucket, Key=str(key))
                except Exception:  # noqa: BLE001
                    pass  # an orphaned body costs storage, not correctness
            await db._call(
                db._table("snapshots").delete_item,
                Key={"pk": snap["pk"], "sk": snap["sk"]},
            )

    # --- events ------------------------------------------------------------------ #
    events = await db._query_all(
        "events",
        KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
        ExpressionAttributeValues={":pk": _user_pk(owner), ":prefix": _run_prefix(run_id)},
    )
    for event in events:
        counts["events"] += 1
        if apply:
            await db._call(
                db._table("events").delete_item,
                Key={"pk": event["pk"], "sk": event["sk"]},
            )

    # --- run-partitioned tables -------------------------------------------------- #
    for table in ("summaries", "threads"):
        rows = await db._query_all(
            table,
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": _run_pk(run_id)},
        )
        for row in rows:
            counts[table] += 1
            if apply:
                await db._call(
                    db._table(table).delete_item,
                    Key={"pk": row["pk"], "sk": row["sk"]},
                )

    # --- the run row, and the NAME marker that outlives it ----------------------- #
    #
    # The marker is the one people forget. It exists so a name is unique per owner, and
    # leaving it behind reserves that name for ever: re-creating the run fails with a
    # duplicate-name error pointing at something that no longer exists.
    if name:
        counts["name_markers"] += 1
        if apply:
            await db._call(
                db._table("runs").delete_item,
                Key={"pk": _user_pk(owner), "sk": _name_sk(str(name))},
            )
    if apply:
        await db._call(
            db._table("runs").delete_item,
            Key={"pk": _user_pk(owner), "sk": _run_sk(run_id)},
        )
    counts["runs"] += 1
    return dict(counts)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", action="append", help="delete this owner's runs")
    parser.add_argument("--all-owners", action="store_true")
    parser.add_argument("--except-owner", action="append", default=[])
    parser.add_argument("--apply", action="store_true", help="actually delete")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--table-prefix", default=None)
    args = parser.parse_args()

    if not args.owner and not args.all_owners:
        raise SystemExit("Pass --owner SUB (repeatable) or --all-owners.")
    prefix = args.table_prefix or os.environ.get("TABLE_PREFIX", "matrix-studio")
    if not os.environ.get("DATA_BUCKET"):
        raise SystemExit("DATA_BUCKET must be set (see the stack outputs).")

    db = Database(table_prefix=prefix, bucket=os.environ["DATA_BUCKET"], region=args.region)
    await db.connect()
    try:
        runs = await run_rows(db)
        wanted = [
            r for r in runs
            if (args.all_owners or str(r.get("owner_sub")) in set(args.owner or []))
            and str(r.get("owner_sub")) not in set(args.except_owner)
        ]
        if not wanted:
            print("Nothing matched. Refusing to report success for a no-op.")
            return 1

        totals: Dict[str, int] = defaultdict(int)
        by_owner = defaultdict(list)
        for r in wanted:
            by_owner[str(r.get("owner_sub"))].append(r)

        for owner, rows in sorted(by_owner.items()):
            print(f"\n{owner} — {len(rows)} run(s)")
            for run in rows:
                counts = await delete_one(db, run, args.apply)
                for key, value in counts.items():
                    totals[key] += value
                detail = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()) if k != "runs")
                print(f"  {str(run.get('name') or run['id'])[:32]:34s} {detail}")

        print(f"\n{'DELETED' if args.apply else 'WOULD DELETE'}:")
        for key, value in sorted(totals.items()):
            print(f"  {value:>6}  {key}")
        if not args.apply:
            print("\nNothing was deleted. Re-run with --apply.")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
