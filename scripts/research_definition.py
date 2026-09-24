#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Research a conversation definition and print what was found. Stores nothing.

`docs/PERSONA-RESEARCH.md` §11 step 1, and it is first on purpose: **if what the researcher finds
does not read as useful to a human, none of the wiring is worth building.** This is where that
judgement gets made, before a Research state, before a UI toggle, and before any of it can put a
corpus in front of six personas.

Stores nothing by design. `--write` exists to dump the corpora to a directory as files you can read
and diff; ingesting into a knowledge base is a later step, because that is where ownership and
binding rules live (§5.1) and they should not be exercised by a tuning tool.

Usage:
    export AWS_REGION=us-east-1                 # for the model calls (Bedrock)
    export BRAVE_API_KEY=...                    # or TAVILY_API_KEY / EXA_API_KEY
    scripts/research_definition.py data/renewalBrief/run.json --dry-run   # queries only
    scripts/research_definition.py data/renewalBrief/run.json
    scripts/research_definition.py data/renewalBrief/run.json --write /tmp/corpora
    scripts/research_definition.py data/renewalBrief/run.json --only Casey

Costs money: search calls, plus one model call per persona and per corpus for tiering. Cheap against
a conversation — cents — but not free, and `--dry-run` shows the queries without paying for search.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix_studio import research as rs  # noqa: E402
from matrix_studio import webfetch, websearch  # noqa: E402


def load(path: str) -> Dict[str, Any]:
    """Validate through the API's own model, so a definition this accepts is one the UI would."""
    from matrix_studio.api.app import CreateRunModel

    raw = json.loads(Path(path).read_text())
    if not raw.get("cast"):
        raise SystemExit(f"{path}: at least one persona is required")
    return CreateRunModel(**raw).model_dump(exclude_none=True)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("definition")
    ap.add_argument("--dry-run", action="store_true",
                    help="generate and print the queries, then stop. No search, no fetch.")
    ap.add_argument("--only", action="append", default=[],
                    help="restrict to these personas (repeatable). 'shared' selects the shared "
                         "corpus. Useful when tuning one persona's queries.")
    ap.add_argument("--write", metavar="DIR",
                    help="write each corpus to DIR as readable files, for diffing between runs")
    ap.add_argument("--provider", help="force a search provider (brave | tavily | exa)")
    ap.add_argument("--results", type=int, default=rs.RESULTS_PER_QUERY)
    ap.add_argument("--fetch", type=int, default=rs.FETCH_PER_QUERY,
                    help="pages fetched per query. The slow part; 0 disables fetching entirely, "
                         "which with a snippet provider means documents with no text.")
    args = ap.parse_args()

    request = load(args.definition)
    # The same brief the Research state builds, deliberately — this script's whole job is to let a
    # human judge what the researcher produces, and it cannot if it briefs it differently.
    brief = rs.brief_for(str(request.get("topic") or ""), request.get("cast") or [])
    print(f"{args.definition}\n  topic    {str(request.get('topic'))[:90]}")
    print(f"  brief    {len(brief)} chars")
    print(f"  cast     {len(request.get('cast') or [])}")

    from matrix_studio.analysis import _acompletion

    provider: Optional[websearch.Provider] = None
    if not args.dry_run:
        try:
            provider = websearch.select(args.provider)
        except websearch.SearchUnavailable as exc:
            raise SystemExit(f"\n{exc}")
        print(f"  search   {provider.name} (supplies_text={provider.supplies_text})")
        if not provider.supplies_text and args.fetch == 0:
            print("  WARNING: a snippet provider with --fetch 0 finds URLs and no text.")

    # --- queries -------------------------------------------------------- #

    wanted = {w.lower() for w in args.only}
    corpora: List[rs.Corpus] = []

    if not wanted or "shared" in wanted:
        shared = rs.Corpus(persona=None)
        shared.queries, shared.cost_usd = await rs.shared_queries(brief, call=_acompletion)
        corpora.append(shared)

    for member in request.get("cast") or []:
        name = str(member.get("name") or "").strip()
        if not name or (wanted and name.lower() not in wanted):
            continue
        viewpoints = ((member.get("structured") or {}).get("viewpoints")) or []
        if not viewpoints:
            print(f"  {name}: no viewpoints, so no research is possible for them")
            continue
        corpus = rs.Corpus(persona=name)
        corpus.queries, corpus.cost_usd = await rs.persona_queries(
            name, viewpoints, brief=brief, call=_acompletion,
        )
        corpora.append(corpus)

    print("\n--- queries ---")
    for c in corpora:
        print(f"\n{c.persona or 'SHARED'}")
        for q in c.queries:
            label = {"support": "support   ", "opposition": "OPPOSITION",
                     "background": "background"}.get(q.intent, q.intent)
            print(f"  [{label}] {q.text}")
    if args.dry_run:
        print(f"\nDry run: nothing was searched. Model cost "
              f"${sum(c.cost_usd for c in corpora):.4f}.")
        return 0

    # --- search, fetch, tier -------------------------------------------- #

    assert provider is not None

    async def search(query: str, count: int):
        return await provider.search(query, count=count)

    async def fetch(url: str):
        return await webfetch.fetch(url)

    print(f"\nsearching and reading ({args.results} results, {args.fetch} fetches per query)…")
    await asyncio.gather(*(
        rs.gather(c, search=search, fetch=fetch, call=_acompletion,
                  results_per_query=args.results, fetch_per_query=args.fetch)
        for c in corpora
    ))

    # --- what came back -------------------------------------------------- #

    print("\n--- found ---")
    print(rs.summarise(corpora))

    for c in corpora:
        print(f"\n=== {c.persona or 'SHARED'} ===")
        for d in sorted(c.documents, key=lambda d: rs.TIERS.index(d.authority)):
            print(f"  [{d.authority:<11}] {len(d.text):>7} chars  {d.title[:58]}")
            print(f"                 {d.url[:96]}")
            if d.authority_reason:
                print(f"                 why: {d.authority_reason}")
        for url, why in c.unreadable:
            print(f"  [UNREADABLE  ] {why:<24} {url[:70]}")
        if c.negative:
            print("\n  --- documented negative ---")
            for line in c.negative.splitlines():
                print(f"  {line}")

    if args.write:
        out = Path(args.write)
        out.mkdir(parents=True, exist_ok=True)
        for c in corpora:
            stem = (c.persona or "shared").replace(" ", "_").replace("/", "_")
            body = [f"# corpus: {c.persona or 'SHARED'}", ""]
            for d in c.documents:
                body += [f"## [{d.authority}] {d.title}", f"<{d.url}>",
                         f"found by: {', '.join(d.found_by)}", "", d.text, ""]
            if c.negative:
                body += ["", c.negative]
            (out / f"{stem}.md").write_text("\n".join(body))
        print(f"\nwrote {len(corpora)} file(s) to {out}")

    print(f"\nmodel cost ${sum(c.cost_usd for c in corpora):.4f}. "
          "Read the above before deciding this feature is worth wiring up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
