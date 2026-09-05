#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Measure FTS5 retrieval recall on a real corpus (Phase 5 step 5b-4).

`docs/PHASE5-RETRIEVAL-DESIGN.md` chose SQLite FTS5 over vectors on operational
grounds and deferred the quality question to a measurement. This is that
measurement. It exists to produce a number that can justify keeping FTS5 or
justify adding embeddings — not to confirm a preference.

## Method

Ground truth is known BY CONSTRUCTION: for a sampled chunk, a model writes a
question that the chunk answers, so the correct passage is known without hand
labelling. Two variants are generated per chunk, and reporting both is the point:

  natural     the question someone would actually type, free to reuse the
              document's own words. This is the OPTIMISTIC bound.
  paraphrased the same information need, deliberately avoiding the chunk's
              distinctive vocabulary (synonyms and plain language only). This is
              the ADVERSARIAL bound, and it is where lexical search should fail.

The gap between those two arms IS the lexical-vs-semantic gap, quantified. A
single number would hide it.

## Honesty notes, read before quoting any figure

- The query generator is an LLM, so "paraphrased" is only as adversarial as the
  model chose to be. Measured lexical overlap between query and gold chunk is
  reported alongside recall so the reader can see how hard each arm actually was.
- Because chunks overlap by design, a neighbouring chunk often contains part of
  the gold text. Strict recall (exact chunk) and lenient recall (gold or an
  adjacent ordinal in the same document) are both reported; strict alone
  understates real-world usefulness, lenient alone overstates it.
- A random-guess baseline is computed so the reader can tell the metric apart
  from chance on this corpus size.

Usage:
    scripts/measure_retrieval_recall.py docs README.md PHASE4-REPORT.md
    scripts/measure_retrieval_recall.py docs --sample 40 --json-out /tmp/r.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matrix_studio.documents import ExtractionError, ingest_file  # noqa: E402
from matrix_studio.retrieval import (  # noqa: E402
    build_fts_query,
    extract_terms,
    filter_by_score,
    select_discriminative_terms,
)
from matrix_studio.settings import get_settings  # noqa: E402
from matrix_studio.storage import Database  # noqa: E402

GEN_PROMPT = """You are building an evaluation set for a document search system.

Below is one passage from a technical document. Write TWO questions that this
passage answers.

1. "natural": the question a colleague would actually type. Use whatever wording
   comes naturally, including terms from the passage.
2. "paraphrased": the SAME information need, but deliberately avoid the
   passage's distinctive vocabulary. Use synonyms and plain language. Someone who
   had never read this passage should be able to ask it this way. Do NOT reuse
   the passage's technical nouns.

Both questions must be answerable from this passage alone, and must be specific
enough that a different passage would not answer them.

PASSAGE
{passage}

Reply with ONLY a JSON object: {{"natural": "...", "paraphrased": "..."}}"""


def collect_files(targets: Sequence[str]) -> List[Path]:
    """Expand paths and directories into a flat list of ingestible files."""
    out: List[Path] = []
    for target in targets:
        p = Path(target)
        if p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.is_file() and child.suffix.lower() in {
                    ".md", ".markdown", ".txt", ".text", ".pdf", ".docx"
                }:
                    out.append(child)
        elif p.is_file():
            out.append(p)
        else:
            print(f"warning: skipping {target} (not found)", file=sys.stderr)
    return out


def lexical_overlap(query: str, passage: str) -> float:
    """Fraction of the query's content terms that literally appear in the passage.

    This is the difficulty dial for the experiment: 1.0 means the query reused the
    passage's words (trivial for BM25); 0.0 means no shared vocabulary at all
    (impossible for BM25 by construction).
    """
    q = set(extract_terms(query, limit=100))
    if not q:
        return 0.0
    body = set(re.findall(r"[a-z0-9]+", passage.lower()))
    return len(q & body) / len(q)


async def generate_queries(
    passage: str, model: str, semaphore: asyncio.Semaphore
) -> Optional[Dict[str, str]]:
    import litellm

    async with semaphore:
        try:
            resp = await litellm.acompletion(
                model=model,
                messages=[{"role": "user", "content": GEN_PROMPT.format(passage=passage)}],
                temperature=0,
                max_tokens=300,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"warning: query generation failed: {exc}", file=sys.stderr)
            return None
    raw = resp.choices[0].message.content or ""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not obj.get("natural") or not obj.get("paraphrased"):
        return None
    cost = 0.0
    try:
        cost = litellm.completion_cost(resp) or 0.0
    except Exception:  # noqa: BLE001
        pass
    return {
        "natural": str(obj["natural"]),
        "paraphrased": str(obj["paraphrased"]),
        "_cost": cost,
    }


def rank_of_gold(
    rows: Sequence[Dict[str, Any]], gold_chunk_id: int, gold_doc: str, gold_ordinal: int
) -> Dict[str, Optional[int]]:
    """1-indexed rank of the gold chunk (strict) and of an adjacent chunk (lenient).

    Lenient exists because chunks overlap by design, so an adjacent ordinal in the
    same document genuinely contains part of the gold text.
    """
    strict: Optional[int] = None
    lenient: Optional[int] = None
    for i, row in enumerate(rows, start=1):
        if strict is None and int(row["chunk_id"]) == gold_chunk_id:
            strict = i
        if (
            lenient is None
            and str(row["document_id"]) == gold_doc
            and abs(int(row["ordinal"]) - gold_ordinal) <= 1
        ):
            lenient = i
    return {"strict": strict, "lenient": lenient}


async def run_pipeline(
    db: Any,
    query: str,
    k: int,
    pipeline: str,
    term_limit: int,
    max_df_ratio: float,
    score_ratio: float,
) -> tuple[List[Dict[str, Any]], str]:
    """Retrieve with either the pre-measurement or the tuned query pipeline.

    ``baseline`` is the original behaviour: OR every extracted term, no score
    filtering. ``tuned`` narrows to discriminative terms and trims the weak tail.
    Running both over identical ground truth is the only honest way to claim the
    change helped.
    """
    terms = extract_terms(query, limit=24)
    if not terms:
        return [], ""
    if pipeline == "tuned":
        doc_freq = await db.term_document_frequencies("eval", terms, persona_name=None)
        total = await db.chunk_count("eval", persona_name=None)
        terms = select_discriminative_terms(
            terms, doc_freq, total, limit=term_limit, max_df_ratio=max_df_ratio
        )
    fts = " OR ".join(f'"{t}"' for t in terms if '"' not in t)
    if not fts:
        return [], ""
    rows = await db.search_documents(
        run_id="eval", query=fts, persona_name=None, k=max(k * 2, k)
    )
    if pipeline == "tuned":
        rows = filter_by_score(rows, score_ratio)
    return rows[:k], fts


def summarise(
    results: List[Dict[str, Any]],
    arm: str,
    ks: Sequence[int],
    pipeline: Optional[str] = None,
) -> Dict[str, Any]:
    rows = [
        r for r in results
        if r["arm"] == arm and (pipeline is None or r.get("pipeline") == pipeline)
    ]
    n = len(rows)
    if not n:
        return {"n": 0}
    out: Dict[str, Any] = {"n": n}
    for mode in ("strict", "lenient"):
        for k in ks:
            hits = sum(
                1 for r in rows if r["rank"][mode] is not None and r["rank"][mode] <= k
            )
            out[f"recall@{k}_{mode}"] = round(hits / n, 4)
        # Mean reciprocal rank over the retrieved window.
        rr = [1.0 / r["rank"][mode] for r in rows if r["rank"][mode] is not None]
        out[f"mrr_{mode}"] = round(sum(rr) / n, 4)
    out["zero_result_rate"] = round(
        sum(1 for r in rows if r["matched"] == 0) / n, 4
    )
    out["mean_lexical_overlap"] = round(
        sum(r["overlap"] for r in rows) / n, 4
    )
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="+", help="Files and/or directories to index")
    ap.add_argument("--sample", type=int, default=40, help="Chunks to sample (default 40)")
    ap.add_argument("--k", type=int, default=5, help="Retrieval depth (default 5)")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--model", default=None, help="Query-generation model")
    ap.add_argument("--json-out", type=Path, default=None)
    ap.add_argument(
        "--compare", action="store_true",
        help="Also evaluate the tuned query pipeline (discriminative terms + "
             "score filter) against the pre-measurement baseline",
    )
    ap.add_argument(
        "--diluted", action="store_true",
        help="Add a 'diluted' arm: the natural question padded with unrelated "
             "terms, modelling the engine's conversation-window query",
    )
    ap.add_argument("--term-limit", type=int, default=8)
    ap.add_argument("--max-df-ratio", type=float, default=0.5)
    ap.add_argument("--score-ratio", type=float, default=0.25)
    args = ap.parse_args()

    model = args.model or get_settings().litellm_model
    files = collect_files(args.targets)
    if not files:
        print("No ingestible files found.", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(Path(tmp) / "eval.db"))
        await db.connect()
        try:
            await db.create_run(run_id="eval", topic="retrieval eval", cast=[])
            total_chars = 0
            for f in files:
                try:
                    doc = ingest_file(f)
                except ExtractionError as exc:
                    print(f"warning: {exc}", file=sys.stderr)
                    continue
                await db.add_document(
                    run_id="eval",
                    title=doc.title,
                    chunks=[c.content for c in doc.chunks],
                    persona_name=None,
                    source_path=doc.source_path,
                    media_type=doc.media_type,
                    char_count=doc.char_count,
                )
                total_chars += doc.char_count

            async with db._conn.execute(
                "SELECT id, document_id, ordinal, content FROM doc_chunks WHERE run_id='eval'"
            ) as cur:
                chunks = [dict(r) for r in await cur.fetchall()]

            print(f"corpus: {len(files)} files, {len(chunks)} chunks, "
                  f"{total_chars:,} chars")
            if not chunks:
                print("Nothing indexed.", file=sys.stderr)
                return 1

            # Only sample chunks with enough substance to ask a specific question
            # about; a 40-character fragment cannot ground a fair query.
            eligible = [c for c in chunks if len(c["content"]) >= 300]
            rng = random.Random(args.seed)
            sample = rng.sample(eligible, min(args.sample, len(eligible)))
            print(f"sampling {len(sample)} of {len(eligible)} eligible chunks "
                  f"(>=300 chars), k={args.k}\n")

            sem = asyncio.Semaphore(args.concurrency)
            generated = await asyncio.gather(
                *(generate_queries(c["content"], model, sem) for c in sample)
            )

            gen_cost = sum(g["_cost"] for g in generated if g)
            pipelines = ("baseline", "tuned") if args.compare else ("baseline",)
            arms = ["natural", "paraphrased"]
            if args.diluted:
                # The engine does not query with a tidy question — it ORs terms
                # from a window of recent conversation, so the real query is a
                # good question buried in unrelated chatter. This arm models that
                # by padding the natural question with terms from an unrelated
                # chunk, which is the case discriminative term selection targets.
                arms.append("diluted")
            results: List[Dict[str, Any]] = []
            for chunk, queries in zip(sample, generated):
                if not queries:
                    continue
                filler_pool = [c for c in chunks if c["id"] != chunk["id"]]
                filler = " ".join(
                    extract_terms(rng.choice(filler_pool)["content"], limit=18)
                ) if filler_pool else ""
                queries = dict(queries)
                queries["diluted"] = f"{queries['natural']} {filler}"
                for arm in arms:
                    query = queries[arm]
                    for pipeline in pipelines:
                        rows, fts = await run_pipeline(
                            db, query, args.k, pipeline,
                            args.term_limit, args.max_df_ratio, args.score_ratio,
                        )
                        results.append({
                            "arm": arm,
                            "pipeline": pipeline,
                            "query": query,
                            "fts_query": fts,
                            "gold_chunk_id": chunk["id"],
                            "matched": len(rows),
                            "overlap": lexical_overlap(query, chunk["content"]),
                            "rank": rank_of_gold(
                                rows, chunk["id"], chunk["document_id"], chunk["ordinal"]
                            ),
                        })

            ks = [k for k in (1, 3, args.k) if k <= args.k]
            ks = sorted(set(ks))
            summary = {
                f"{arm}/{pipeline}": summarise(results, arm, ks, pipeline)
                for arm in arms
                for pipeline in pipelines
            }

            # Chance baseline: probability a uniformly random top-k selection
            # contains the gold chunk, for this corpus size.
            baseline = round(min(1.0, args.k / len(chunks)), 6)

            cols = list(summary)
            width = max(12, max(len(c) for c in cols) + 1)
            header = f"{'metric':28s}" + "".join(f"{c:>{width}s}" for c in cols)
            print(header)
            print("-" * len(header))
            for key in [f"recall@{k}_strict" for k in ks] + \
                       [f"recall@{k}_lenient" for k in ks] + \
                       ["mrr_strict", "mrr_lenient", "zero_result_rate",
                        "mean_lexical_overlap"]:
                line = f"{key:28s}"
                for c in cols:
                    line += f"{summary[c].get(key, '-')!s:>{width}s}"
                print(line)
            print("-" * len(header))
            line = f"{'n (queries)':28s}"
            for c in cols:
                line += f"{summary[c]['n']!s:>{width}s}"
            print(line)
            print(f"{'random-guess recall@k':28s}" +
                  "".join(f"{baseline!s:>{width}s}" for _ in cols))
            print(f"\nquery-generation cost: ${gen_cost:.4f}")

            report = {
                "corpus": {
                    "files": [str(f) for f in files],
                    "chunks": len(chunks),
                    "chars": total_chars,
                },
                "k": args.k,
                "sample": len(sample),
                "seed": args.seed,
                "model": model,
                "random_baseline_recall_at_k": baseline,
                "summary": summary,
                "results": results,
                "generation_cost_usd": round(gen_cost, 6),
            }
            if args.json_out:
                args.json_out.write_text(json.dumps(report, indent=2) + "\n")
                print(f"wrote {args.json_out}")
            return 0
        finally:
            await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
