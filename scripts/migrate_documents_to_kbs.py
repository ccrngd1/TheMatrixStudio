#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Phase 6 step 6: copy existing run documents into per-KB vector indexes.

`docs/project/PHASE6-KB-DESIGN.md` §4. What this is *for* is worth being blunt about: the 48
documents in the deployed account are **all my own test data** — 46 from the recall
measurement corpora, 2 from Phase 2 verification, no user documents at all. So this
script is not rescuing anything. It is a **rehearsal on disposable data**, which is a
better position than either migrating precious data or never writing the script.

## What it does

One KB per `(run, persona)` for persona-scoped documents and one KB per run for
cast-wide ones (§4.4), which is the faithful mapping and exercises both binding levels.
Vectors are **copied, not re-embedded**: `ListVectors(returnData=True)` returns the full
1024-float vector, so this costs nothing at Bedrock and makes recall identical by
construction rather than a thing to measure afterwards.

## What it deliberately does NOT do

- **It does not delete the old shared index** (§4.5). Deleting an index in a live account
  is irreversible and the copies want verifying first. `BACKLOG.md` holds it.
- **It does not bind the new KBs to their original runs** unless asked with `--bind`.
  Retrieval keeps the run slice (§8.1), so a migrated run already finds its documents;
  binding as well would make every passage reachable twice. That is de-duplicated in the
  merge, but a binding whose only effect is to be de-duplicated is noise.
- **It writes nothing without `--apply`.** A dry run is the default because this touches
  a live account, and a migration whose first execution is the real one is a migration
  nobody has read the output of.

## The item that is not a document

The `documents` table holds an `EMBEDDING`/`META` marker with no `run_id` and no
`owner_sub`. A scan that treats every item as a document would create a KB for it. This
filters on `pk` beginning `RUN#` and **asserts** that everything skipped is recognised,
so a future non-document item is a loud failure rather than a silent skip.

Usage:
    AWS_PROFILE=... python scripts/migrate_documents_to_kbs.py            # dry run
    AWS_PROFILE=... python scripts/migrate_documents_to_kbs.py --apply
    AWS_PROFILE=... python scripts/migrate_documents_to_kbs.py --apply --bind
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix_studio.storage import Database  # noqa: E402
from matrix_studio.storage.vectors import ensure_kb_index, kb_index_name  # noqa: E402

# Non-document items this script knows about. Anything else with a non-`RUN#` partition
# key is an ERROR, not a skip: the whole hazard here is treating an unrecognised item as
# a document and minting a knowledge base for it.
KNOWN_NON_DOCUMENTS = {("EMBEDDING", "META")}


def kb_name_for(run_id: str, persona: Optional[str]) -> str:
    """A readable, deterministic KB name, so a re-run finds its own work.

    Deterministic matters more than pretty: this is how the script is idempotent without
    keeping a ledger of what it did. Which is also why the run id is used **whole**.

    The first version truncated it to eight characters, and the dry run against the real
    account caught what that does: four separate runs printed as `migrated-eval-178`,
    because the measurement corpora are named `eval-178…` and share a prefix. The name is
    the idempotence key, so the second of those four would have "reused" the first's KB
    and four distinct corpora would have been merged into one — and with `--bind`, bound
    into conversations they do not belong to. The same reasoning `kb_index_name` gives for
    truncating the prefix rather than the id, arrived at the hard way.
    """
    if not persona:
        return f"migrated-{run_id}"
    # Slugified: a persona name is free text ("Someone Who Left"), and a KB name with
    # spaces in it is awkward to type at every later step that names one.
    slug = re.sub(r"[^a-z0-9]+", "-", persona.lower()).strip("-") or "persona"
    return f"migrated-{run_id}-{slug}"


async def scan_documents(db: Database) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run documents to migrate, plus every item that is not one. `(documents, others)`.

    Three kinds of item live in this table now, and only the first is this script's
    business:

        RUN#{run_id} / DOC#…   a run's document — what this migrates
        KB#{kb_id}  / DOC#…    a document already IN a knowledge base — skipped
        EMBEDDING   / META     the old global embedding-model marker — skipped

    The KB case appeared when knowledge bases gained their own documents, and it had to
    be added here for a reason worth recording: the unrecognised-item check is a
    `SystemExit`, so a `KB#` item would have **aborted the migration entirely** rather
    than being quietly mishandled. That is the check working — it refused to guess about
    an item shape nobody had told it about — and it is why the check is loud.
    """
    client = db._table("documents")
    documents: List[Dict[str, Any]] = []
    others: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {}
    while True:
        page = await db._call(client.scan, **kwargs)
        for item in page.get("Items", []):
            pk = str(item.get("pk") or "")
            is_doc = str(item.get("sk") or "").startswith("DOC#")
            # `DOC#` in the sort key as well as `RUN#` in the partition key: a run
            # partition also holds nothing else today, but "documents are the items that
            # look like documents" is the assumption worth being explicit about.
            if pk.startswith("RUN#") and is_doc:
                documents.append(item)
            else:
                others.append(item)
        token = page.get("LastEvaluatedKey")
        if not token:
            return documents, others
        kwargs["ExclusiveStartKey"] = token


def is_known_non_document(item: Dict[str, Any]) -> bool:
    """Whether a non-run-document item is one this script knows to skip."""
    pk, sk = str(item.get("pk") or ""), str(item.get("sk") or "")
    if (pk, sk) in KNOWN_NON_DOCUMENTS:
        return True
    # A document that already belongs to a knowledge base. Nothing to migrate: it is
    # already where this script would put it.
    return pk.startswith("KB#") and sk.startswith("DOC#")


async def vectors_for_run(db: Database, run_id: str, owner_sub: str) -> List[Dict[str, Any]]:
    """Every stored vector for a run, WITH its data, from the old shared index.

    `ListVectors` has no filter parameter — unlike `QueryVectors` — so this pages the
    whole index and filters on returned metadata. Acceptable because it is a one-off
    migration rather than anything on the per-turn path.
    """
    import os

    bucket = os.environ.get("VECTOR_BUCKET", "")
    if not bucket:
        raise SystemExit("VECTOR_BUCKET is not set, so there is no index to read.")
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
            if str(meta.get("run_id") or "") != run_id:
                continue
            if str(meta.get("owner_sub") or "") != owner_sub:
                continue
            out.append(entry)
        token = page.get("nextToken")
        if not token:
            return out


def group_key(meta: Dict[str, Any]) -> Optional[str]:
    """Which KB a vector belongs to: its persona, or None for cast-wide.

    `cast_wide: True` rather than an absent `persona_name`, matching what
    `store_chunk_vectors` writes — absent metadata cannot be matched by a filter, which
    is why the flag exists at all.
    """
    persona = meta.get("persona_name")
    return str(persona) if persona else None


async def migrate(db: Database, *, apply: bool, bind: bool) -> int:
    documents, others = await scan_documents(db)

    unknown = [o for o in others if not is_known_non_document(o)]
    if unknown:
        # Loud, not skipped. See the module docstring.
        for item in unknown[:10]:
            print(f"  UNRECOGNISED: pk={item.get('pk')!r} sk={item.get('sk')!r}")
        raise SystemExit(
            f"{len(unknown)} item(s) in the documents table are neither a document nor a "
            "known marker. Refusing to migrate: an unrecognised item treated as a "
            "document would have a knowledge base minted for it. Add it to "
            "KNOWN_NON_DOCUMENTS once you have looked at it."
        )

    print(f"{len(documents)} document(s), {len(others)} recognised non-document item(s)")

    # (run_id, owner_sub, persona) -> documents. The owner is part of the key because a
    # KB has exactly one owner, and two tenants' documents must never land in one.
    groups: Dict[Tuple[str, str, Optional[str]], List[Dict[str, Any]]] = defaultdict(list)
    for doc in documents:
        run_id = str(doc.get("run_id") or "")
        owner = str(doc.get("owner_sub") or "")
        if not run_id or not owner:
            raise SystemExit(
                f"document {doc.get('id')!r} has no run_id or owner_sub, so it cannot be "
                "assigned to a knowledge base. Investigate rather than guessing."
            )
        persona = doc.get("persona_name") or None
        groups[(run_id, owner, str(persona) if persona else None)].append(doc)

    print(f"{len(groups)} knowledge base(s) to create\n")

    created = 0
    copied_total = 0
    per_run_vectors: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

    for (run_id, owner, persona), docs in sorted(groups.items(), key=lambda kv: str(kv[0])):
        name = kb_name_for(run_id, persona)
        level = "persona" if persona else "run"
        doc_ids = {str(d.get("id")) for d in docs}

        # One listing per (run, owner), reused across that run's persona groups: paging
        # the whole shared index once per KB would be the same read repeated.
        if (run_id, owner) not in per_run_vectors:
            per_run_vectors[(run_id, owner)] = await vectors_for_run(db, run_id, owner)
        entries = [
            e for e in per_run_vectors[(run_id, owner)]
            if str((e.get("metadata") or {}).get("document_id") or "") in doc_ids
            and group_key(e.get("metadata") or {}) == persona
        ]

        print(
            f"{name}: {len(docs)} document(s), {len(entries)} vector(s), "
            f"{level}-level, owner {owner}"
        )
        if not entries:
            # Documents with no embeddings. Real: the corpora were embedded per run and
            # some runs never got that far. A KB with no vectors is not useful and would
            # be indistinguishable from a broken one later.
            print("  no stored vectors — skipped (nothing to copy)")
            continue

        if not apply:
            copied_total += len(entries)
            created += 1
            continue

        bound = db.for_owner(owner)
        existing = [kb for kb in await bound.list_knowledge_bases(owner_sub=owner) if kb.get("name") == name]
        if existing:
            kb = existing[0]
            print(f"  reusing existing KB {kb['id']}")
        else:
            kb = await bound.create_knowledge_base(name, owner_sub=owner)
            print(f"  created KB {kb['id']}")
        created += 1

        import os

        bucket = os.environ["VECTOR_BUCKET"]
        index = kb_index_name(kb["id"], db.table_prefix)
        client = db._vectors_client()
        await ensure_kb_index(client, bucket, index)

        titles = {str(d.get("id")): str(d.get("title") or "") for d in docs}
        payload = []
        for entry in entries:
            meta = dict(entry.get("metadata") or {})
            doc_id = str(meta.get("document_id") or "")
            payload.append({
                "key": entry["key"],
                "data": entry["data"],
                "metadata": {
                    "kb_id": kb["id"],
                    "owner_sub": owner,
                    "document_id": doc_id,
                    "ordinal": int(meta.get("ordinal") or 0),
                    "text": str(meta.get("text") or ""),
                    # §8.2: the title must travel with the vector, because a grantee
                    # cannot read the owner's document rows to look it up.
                    "title": titles.get(doc_id, doc_id),
                },
            })

        for start in range(0, len(payload), 500):
            await db._call(
                client.put_vectors,
                vectorBucketName=bucket,
                indexName=index,
                vectors=payload[start:start + 500],
            )
        copied_total += len(payload)
        print(f"  copied {len(payload)} vector(s) into {index}")

        if bind:
            await bind_kb_to_run(bound, run_id, owner, kb["id"], persona)
            print(f"  bound to run {run_id} at {level} level")

    print(
        f"\n{'APPLIED' if apply else 'DRY RUN'}: {created} knowledge base(s), "
        f"{copied_total} vector(s)"
    )
    if not apply:
        print("Nothing was written. Re-run with --apply.")
    return 0


async def bind_kb_to_run(
    db: Database, run_id: str, owner: str, kb_id: str, persona: Optional[str]
) -> None:
    """Add a binding to the run's stored config, at run or persona level.

    The write happens HERE rather than through a storage method, deliberately. Doing it
    properly would mean adding a general "rewrite any field on a run" method, and a
    read-modify-write over `config_json` is exactly the operation the rest of the
    codebase avoids — Phase 5 gave `budget` its own attribute rather than rewriting that
    blob, for precisely this reason. A one-off migration over finished runs may do it; the
    application may not, and it should not gain the tool.

    Which is also why `--bind` is opt-in: two concurrent writers would lose one binding.
    """
    import json

    from matrix_studio.storage.dynamo import _run_sk, _user_pk

    run = await db.get_run(run_id)
    if not run:
        raise SystemExit(f"run {run_id} vanished between the scan and the binding")

    if persona:
        cast = json.loads(run.get("cast_json") or "[]")
        for member in cast:
            if str(member.get("name") or "") != persona:
                continue
            bound = list(member.get("knowledge_bases") or [])
            if kb_id not in bound:
                member["knowledge_bases"] = bound + [kb_id]
            break
        else:
            raise SystemExit(
                f"run {run_id} has no persona named {persona!r}, so a persona-level "
                "binding cannot be written. The document's persona_name does not match "
                "the cast — investigate rather than binding it cast-wide."
            )
        field, value = "cast_json", json.dumps(cast)
    else:
        config = json.loads(run.get("config_json") or "{}")
        bound = list(config.get("knowledge_bases") or [])
        if kb_id not in bound:
            config["knowledge_bases"] = bound + [kb_id]
        field, value = "config_json", json.dumps(config)

    await db._call(
        db._table("runs").update_item,
        Key={"pk": _user_pk(owner), "sk": _run_sk(run_id)},
        UpdateExpression=f"SET {field} = :v",
        ExpressionAttributeValues={":v": value},
        # The run must still exist. Without this an update_item would CREATE a stub run
        # item with nothing but a config, which would then list in the UI as a run with
        # no topic and no cast.
        ConditionExpression="attribute_exists(pk)",
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="actually write. Without this the script only reports what it would do.",
    )
    parser.add_argument(
        "--bind", action="store_true",
        help="also add each new KB to its original run's bindings. Off by default: "
             "retrieval keeps the run slice, so a migrated run already finds its own "
             "documents and the binding's only effect would be a de-duplicated hit.",
    )
    parser.add_argument("--table-prefix", default=None)
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    import os

    prefix = args.table_prefix or os.environ.get("TABLE_PREFIX", "matrix-studio")
    bucket = os.environ.get("DATA_BUCKET", "")
    if not bucket:
        raise SystemExit("DATA_BUCKET is not set.")

    db = Database(table_prefix=prefix, bucket=bucket, region=args.region)
    await db.connect()
    try:
        return await migrate(db, apply=args.apply, bind=args.bind)
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
