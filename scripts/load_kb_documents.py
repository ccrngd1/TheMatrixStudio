#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Load a directory of documents into knowledge bases on the deployed stack — one KB per file.

## Why one KB per file

A collection is the unit of *binding*, so it is also the unit of asymmetry. Giving every
persona the same collection produces a conversation where nobody has anything the others
lack, which is the failure mode of a briefing rather than a debate. One collection per
persona, bound to that cast member only, is what makes "what does this speaker know" a real
question at retrieval time.

A shared collection is still useful for material the whole room has — the proposal under
discussion — and that one is bound at run level instead. This script does not decide which
is which: it creates one KB per file and prints the ids, and the run definition does the
binding.

## What it does per file

    create_knowledge_base          the row, owned by `--owner`
    ensure_kb_index               the per-KB vector index (immutable dimension/metric)
    add_kb_document               chunks + text bodies in S3
    embed_pending_kb_chunks       vectors, inline, and a failure here is fatal

Embedding inline rather than in the background is the same choice the API route makes: a
document that is stored but unembedded is listed in the collection and permanently
unretrievable, which reads as retrieval being bad rather than an upload having half-failed.

Idempotency is by NAME. A second run with the same `--prefix` refuses rather than creating a
duplicate set, because two collections with the same name is the state where a binding is
ambiguous and nothing says so.

Usage:
    export AWS_REGION=us-east-1 TABLE_PREFIX=matrix-studio
    export DATA_BUCKET=... VECTOR_BUCKET=... VECTOR_INDEX=matrix-studio-chunks
    scripts/load_kb_documents.py data/renewalBrief/kb --owner SUB --prefix renewal
    scripts/load_kb_documents.py data/renewalBrief/kb --owner SUB --prefix renewal --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix_studio.retrieval import embed_pending_kb_chunks  # noqa: E402
from matrix_studio.storage import Database  # noqa: E402
from matrix_studio.storage.vectors import (  # noqa: E402
    EMBEDDING_DIMENSION,
    ensure_kb_index,
    kb_index_name,
)


def title_of(path: Path, text: str) -> str:
    """The document's first markdown heading, or its filename."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", help="a directory of .md/.txt files, one per KB")
    parser.add_argument("--owner", required=True, help="Cognito sub that will own the KBs")
    parser.add_argument("--prefix", default="", help="prepended to each KB name")
    parser.add_argument("--exclude", action="append", default=["README"],
                        help="filename stems to skip (default: README)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    for name in ("AWS_REGION", "DATA_BUCKET", "VECTOR_BUCKET"):
        if not os.environ.get(name):
            raise SystemExit(f"{name} must be set (see the stack outputs).")

    root = Path(args.directory)
    files = sorted(
        p for p in root.iterdir()
        if p.suffix in (".md", ".txt") and p.stem not in set(args.exclude)
    )
    if not files:
        raise SystemExit(f"No .md/.txt files in {root}. Refusing to report success for a no-op.")

    print(f"{len(files)} file(s) from {root}, owner {args.owner}\n")
    for path in files:
        text = path.read_text()
        print(f"  {path.name:<16} {len(text):>7,} chars  {title_of(path, text)[:60]}")
    if args.dry_run:
        print("\nDry run: nothing was created.")
        return 0

    db = Database(
        table_prefix=os.environ.get("TABLE_PREFIX", "matrix-studio"),
        bucket=os.environ["DATA_BUCKET"],
        region=os.environ["AWS_REGION"],
    )
    await db.connect()
    bucket = os.environ["VECTOR_BUCKET"]
    created: List[Dict[str, object]] = []
    try:
        bound = db.for_owner(args.owner)
        # Refuse before creating anything, rather than half-way through: a partial load
        # leaves collections whose documents are missing, and the run that binds them
        # retrieves nothing with no error anywhere.
        existing = {
            str(kb.get("name")) for kb in await bound.list_knowledge_bases(
                owner_sub=args.owner, groups=[],
            )
        }
        wanted = [f"{args.prefix}-{p.stem}" if args.prefix else p.stem for p in files]
        clash = sorted(set(wanted) & existing)
        if clash:
            raise SystemExit(
                "These collections already exist for this owner: " + ", ".join(clash)
                + "\nUse a different --prefix, or delete them first. Two collections with "
                "one name makes every binding ambiguous."
            )

        total_cost = 0.0
        for path, name in zip(files, wanted):
            text = path.read_text()
            kb = await bound.create_knowledge_base(
                name, owner_sub=args.owner,
                description=f"{title_of(path, text)} (loaded from {path.name})",
            )
            kb_id = str(kb["id"])
            index = kb_index_name(kb_id, db.table_prefix)
            await ensure_kb_index(db._vectors_client(), bucket, index)
            doc_id = await bound.add_kb_document(
                kb_id, title=title_of(path, text), text=text, char_count=len(text),
            )
            # `dimensions` is stated rather than left to the provider's default: a KB
            # index is created at EMBEDDING_DIMENSION and its dimension is immutable, so
            # a default that happens to match today is a silent dependency.
            result = await embed_pending_kb_chunks(
                bound, kb_id, dimensions=EMBEDDING_DIMENSION,
            )
            if result.get("error"):
                raise SystemExit(
                    f"{path.name}: stored as {doc_id} in {kb_id} but NOT embedded, so it "
                    f"is not retrievable: {result['error']}"
                )
            total_cost += float(result.get("cost_usd") or 0.0)
            created.append({
                "file": path.name, "kb_id": kb_id, "name": name,
                "document_id": doc_id, "chunks": result.get("embedded"),
            })
            print(f"  ✓ {name:<26} {kb_id}  {result.get('embedded')} chunk(s)")

        print(f"\nEmbedding cost ${total_cost:.6f}")
        print(json.dumps({c["file"]: c["kb_id"] for c in created}, indent=2))
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
