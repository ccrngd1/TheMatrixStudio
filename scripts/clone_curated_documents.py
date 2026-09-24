#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Copy the CURATED documents out of a collection into a fresh one. Research is left behind.

## Why this exists

`docs/PERSONA-RESEARCH.md` §5.1 has research ingest into the collection already bound at a scope,
which is right — a persona with a curated collection and a research collection would be two places
to look for the same kind of thing. The consequence is that a collection can hold both, and the
`origin` field is what tells them apart.

On 2026-09-24 that consequence arrived by accident: run `602ddffe` wrote research into the six
`renewal-*` persona collections, because the verify definition kept their persona-level bindings and
nobody checked. Nothing was lost — every curated document survived — but **§9's control arm was
contaminated**, because a "without research" conversation bound to those collections now retrieves
research.

So this reconstructs the pre-research baseline: a new collection per source, holding only the
documents research did not put there. `docs/BACKLOG.md` records the decision not to delete the
research instead — those documents are the corpus behind run `602ddffe`'s own citations, and the
transcript records which passage each persona saw WITHOUT storing its text, so deleting them would
make the first live proof of the feature unauditable.

## What it does and does not copy

Copies a document when its `origin` is anything other than `researched` — so `uploaded`, and the
`None` that every document written before the field existed carries. That inclusive test is
deliberate: the failure to avoid is silently dropping somebody's hand-made document because it
predates a schema change, and the cost of being wrong the other way is a researched document
appearing in a baseline, which the printed manifest makes visible.

**Creates; never deletes.** The source collections are untouched, so a mistake here costs an unused
collection rather than a corpus.

Usage:
    export AWS_REGION=us-east-1 TABLE_PREFIX=matrix-studio
    export DATA_BUCKET=... VECTOR_BUCKET=... VECTOR_INDEX=matrix-studio-chunks
    scripts/clone_curated_documents.py --owner SUB --definition data/renewalBrief/run.json --dry-run
    scripts/clone_curated_documents.py --owner SUB --definition data/renewalBrief/run.json \
        --suffix -baseline --write data/renewalBrief/run.baseline.json

Costs a little: one embedding call per batch of chunks. Cents.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix_studio.storage import Database  # noqa: E402

RESEARCHED = "researched"


def bindings_of(definition: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Every (scope, kb_id) a definition binds, cast-wide first then per persona."""
    out: List[Tuple[str, str]] = []
    for kb in (definition.get("config") or {}).get("knowledge_bases") or []:
        out.append(("cast-wide", str(kb)))
    for member in definition.get("cast") or []:
        for kb in member.get("knowledge_bases") or []:
            out.append((str(member.get("name") or "?"), str(kb)))
    return out


async def clone(
    db: Any, privileged: Any, kb_id: str, *, owner: str, suffix: str, dry_run: bool
) -> Optional[Dict[str, Any]]:
    """Copy one collection's curated documents into a new one. Returns what happened."""
    source = await db.get_knowledge_base(kb_id)
    if source is None:
        print(f"  {kb_id}  MISSING — skipped")
        return None
    if str(source.get("owner_sub") or "") != owner:
        # Ownership, not readability: writing a copy is fine, but a collection this caller does
        # not own is one whose curated set they may be misreading, and the point of a baseline is
        # that it is exactly what was there.
        print(f"  {kb_id}  NOT OWNED by {owner} — skipped")
        return None

    docs = await db.list_kb_documents(kb_id)
    curated = [d for d in docs if str(d.get("origin") or "") != RESEARCHED]
    research = len(docs) - len(curated)
    name = f"{source.get('name') or kb_id}{suffix}"
    print(f"  {kb_id}  {str(source.get('name'))[:28]:<30} "
          f"{len(curated)} curated, {research} researched -> {name!r}")
    for d in curated:
        print(f"      keep  origin={str(d.get('origin')):<10} {str(d.get('title'))[:56]}")
    if dry_run:
        return None

    created = await db.create_knowledge_base(
        name,
        owner_sub=owner,
        description=(
            f"Curated documents copied from {kb_id} ({source.get('name')}), excluding anything a "
            "research pass added. A pre-research baseline — see PERSONA-RESEARCH.md §9."
        ),
    )
    new_id = str(created["id"])

    # The index is created with THESE credentials, which must be unscoped: the tenant role holds
    # `s3vectors:GetIndex` and deliberately not `CreateIndex`.
    from matrix_studio.storage.vectors import ensure_index_for_kb

    await ensure_index_for_kb(privileged, new_id)

    copied = 0
    for d in curated:
        text = await db.document_text(str(d["id"]))
        if not text.strip():
            # A document whose body is gone would copy as an empty row that retrieves nothing —
            # worse than absent, because the baseline would look complete.
            print(f"      SKIP  {str(d.get('title'))[:50]} — no readable body")
            continue
        await db.add_kb_document(
            new_id,
            title=str(d.get("title") or "untitled"),
            text=text,
            source_path=d.get("source_path"),
            media_type=d.get("media_type") or "txt",
            char_count=len(text),
            # Carried through rather than defaulted. A copy of an upload is an upload; inventing
            # an origin here would make the baseline disagree with the thing it copies.
            origin=d.get("origin"),
            authority=d.get("authority"),
        )
        copied += 1

    from matrix_studio.retrieval import embed_pending_kb_chunks
    from matrix_studio.storage.vectors import EMBEDDING_DIMENSION

    # Without this the copies are stored and unretrievable — a chunk with no vector is invisible
    # to a k-NN query, and a baseline that retrieves nothing would read as a baseline that had
    # nothing to say.
    embedded = await embed_pending_kb_chunks(db, new_id, dimensions=EMBEDDING_DIMENSION)
    if embedded.get("error"):
        print(f"      EMBED FAILED: {embedded['error']}")
    print(f"      -> {new_id}  {copied} document(s), {embedded.get('embedded', 0)} passage(s)")
    return {"source": kb_id, "new_id": new_id, "documents": copied,
            "embedded": embedded.get("embedded", 0), "error": embedded.get("error")}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", required=True)
    ap.add_argument("--definition", required=True,
                    help="a conversation definition; every collection it binds is cloned")
    ap.add_argument("--suffix", default="-baseline")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the manifest and create nothing")
    ap.add_argument("--write", metavar="PATH",
                    help="write a copy of the definition rebound to the new collections")
    args = ap.parse_args()

    definition = json.loads(Path(args.definition).read_text())
    bound = bindings_of(definition)
    if not bound:
        raise SystemExit(f"{args.definition} binds no knowledge bases; nothing to clone")

    print(f"{args.definition} binds {len(bound)} collection(s)\n")
    store = Database(
        table_prefix=os.environ.get("TABLE_PREFIX", "matrix-studio"),
        bucket=os.environ["DATA_BUCKET"],
        region=os.environ["AWS_REGION"],
    )
    await store.connect()
    try:
        db = store.for_owner(args.owner)
        mapping: Dict[str, str] = {}
        for scope, kb_id in bound:
            print(f"{scope}:")
            result = await clone(
                db, store, kb_id, owner=args.owner, suffix=args.suffix, dry_run=args.dry_run,
            )
            if result:
                mapping[kb_id] = result["new_id"]
    finally:
        await store.close()

    if args.dry_run:
        print("\nDry run: nothing was created.")
        return 0

    if args.write and mapping:
        # Rebind by SUBSTITUTION, so a collection that failed to clone keeps pointing at the
        # original rather than silently vanishing from the definition.
        out = json.loads(json.dumps(definition))
        cfg = out.setdefault("config", {})
        if cfg.get("knowledge_bases"):
            cfg["knowledge_bases"] = [mapping.get(k, k) for k in cfg["knowledge_bases"]]
        for member in out.get("cast") or []:
            if member.get("knowledge_bases"):
                member["knowledge_bases"] = [
                    mapping.get(k, k) for k in member["knowledge_bases"]
                ]
        out["_note"] = (
            "Bound to PRE-RESEARCH baseline collections, cloned by "
            "scripts/clone_curated_documents.py. Use this as §9's control arm: the collections "
            f"bound by {args.definition} hold research as of 2026-09-24."
        )
        Path(args.write).write_text(json.dumps(out, indent=2) + "\n")
        print(f"\nwrote {args.write} — rebound {len(mapping)} collection(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
