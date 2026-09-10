# Embedding width: 256 vs 512 vs 1024, measured

**Status:** COMPLETE. Run 2026-09-10. This settles AWS implementation plan item **0.4**,
and the verdict goes against the hoped-for answer.

## Why this had to be measured before anything is built

On the AWS target an S3 Vectors index's **dimension is fixed at creation** — changing it
means creating new indexes and re-populating every one. So this is a decide-once choice, and
the AWS Well-Architected Generative AI Lens raises it twice as a cost and performance
practice: **GENCOST04-BP01** "Reduce vector length on embedded tokens" and **GENPERF04-BP02**
"Optimize vector sizes for your use case", with **GENPERF04-BP01** saying to test embeddings
for relevance rather than assume.

The hope was that 256 would hold recall, which would have been a **4× reduction** in vector
storage and query cost for free. It does not.

## Reproduce

```bash
# Generate the query set ONCE, then reuse it for every width. Without this the widths
# are not comparable: query generation is non-deterministic (see "Two instrument
# defects" below), so each run would measure different ground truth.
scripts/measure_retrieval_recall.py docs README.md CHANGELOG.md \
    --sample 200 --k 5 --diluted --modes vector --seed 11 --concurrency 12 \
    --dimensions 1024 --queries-out /tmp/q.json --json-out /tmp/d1024.json

for D in 256 512; do
  scripts/measure_retrieval_recall.py docs README.md CHANGELOG.md \
      --sample 200 --k 5 --diluted --modes vector --seed 11 \
      --dimensions $D --queries-in /tmp/q.json --json-out /tmp/d$D.json
done
```

Corpus: 21 files, 669 chunks, 462,354 chars. 200 chunks sampled, **128 produced usable
queries**, so n=128 and the resolution is **1/128 = 0.008 per query** — differences smaller
than that are one query and mean nothing.

Cost: **$0.3234** for query generation (once, then cached) plus $0.0024 per embedding pass.

## Results, all three widths against the identical 128 queries

**recall@1 (strict)** — the figure that matters most, because a turn injects only k=1..3

| arm | 256 | 512 | 1024 |
|---|---|---|---|
| natural | **0.6719** | 0.6562 | 0.6641 |
| **paraphrased** | 0.2109 | 0.2891 | **0.3281** |
| **diluted** (engine-shaped) | 0.5625 | **0.5703** | 0.5625 |

**recall@3 / recall@5 (strict)**

| arm | 256 | 512 | 1024 |
|---|---|---|---|
| natural | 0.8203 / 0.8594 | 0.8281 / 0.8828 | **0.8359 / 0.8906** |
| **paraphrased** | 0.4297 / 0.5469 | 0.5078 / 0.5938 | **0.5469 / 0.6250** |
| diluted | 0.7422 / 0.7812 | 0.7578 / 0.8047 | **0.7656 / 0.8125** |

**MRR (strict)**

| arm | 256 | 512 | 1024 |
|---|---|---|---|
| natural | **0.7525** | 0.7465 | 0.7512 |
| **paraphrased** | 0.3374 | 0.4070 | **0.4382** |
| diluted | 0.6552 | 0.6637 | **0.6668** |

## What this settles

**Width does not matter for natural queries, and matters a lot for paraphrased ones.**

- **Natural**: flat. Every difference across the three widths is one or two queries. If the
  only queries were well-formed questions sharing vocabulary with the passage, 256 would be
  free money.
- **Paraphrased**: **1024 is decisively better.** recall@1 goes 0.2109 → 0.3281, which is
  **+15 queries out of 128** — nearly twenty times the one-query resolution. recall@3 moves
  by the same 15 queries, recall@5 by 10, and MRR by 0.101.
- **Monotonic in four metrics.** 256 < 512 < 1024 holds for paraphrased recall@1, recall@3,
  recall@5 *and* MRR. Four independent metrics agreeing on direction is a signal, not a
  coincidence.
- **Diluted** — the shape the engine actually produces — favours 1024 by 3–4 queries at
  recall@3/@5 and by a hair on MRR. On its own that would be too close to call; in the
  context of the monotonic paraphrased result it is consistent with the same effect, weaker.

The mechanism makes sense: truncating dimensions costs you most exactly where the query
does **not** share vocabulary with the passage — which is the capability embeddings exist to
provide, and the reason vector retrieval was chosen over lexical in the first place
(`PHASE5-RETRIEVAL-MEASUREMENT.md` §5f: lexical recall@1 of 0.017 on diluted queries).
Buying a 4× cost reduction by giving up paraphrase robustness is trading away the thing that
was being paid for.

## Decision

**Use 1024 dimensions** — Titan Text Embeddings v2's default, and what Phase 5f measured, so
its numbers transfer without re-baselining.

The cost of that decision, stated plainly: 4× the vector storage and query cost of 256. At
the scale in question (a persona holds 1–5 documents ≈ 63 chunks) this is a rounding error
against Bedrock inference, so the trade is easy. It would deserve revisiting only for an
org-wide corpus in the millions of chunks, where storage starts to matter and where a
paraphrase-recall loss could be measured against the actual query mix rather than this
synthetic one.

512 is a reasonable middle if storage ever becomes a real constraint: it recovers about
two-thirds of the paraphrased gap for half the width.

## Two instrument defects found and fixed on the way

Both mattered more than the result, because both would have silently corrupted any future
measurement.

**1. Query generation was failing for every sample, and the script reported zeros.**
`generate_queries` hardcoded `temperature=0`, and the configured model
(`claude-sonnet-5`) accepts **only** `temperature=1` — so every call raised
`UnsupportedParamsError`. The script printed a warning per failure to stderr and then a
results table full of dashes with `n (queries) = 0`, which reads as "retrieval found nothing"
rather than "nothing was measured". This is the same defect the engine fixed with
`litellm.drop_params = True`; the script never got the fix.

Now: `drop_params` is set, **and an empty measurement exits non-zero** with the cause, rather
than printing a table. A measurement instrument that reports zeros when it is broken is worse
than one that crashes.

**2. Comparisons were silently incomparable.** With `drop_params` set, generation runs at
temperature=1 and is non-deterministic — a first attempt at this comparison produced n=30 for
one width and n=25 for another, i.e. two different ground truths. Added `--queries-out` /
`--queries-in` so a query set is generated once and reused, and an A/B differs in exactly one
variable. It refuses only when the cache matches *none* of the sample; a cache legitimately
covers fewer chunks than were sampled, because some passages never produce usable queries.

## Also added

`dimensions` is now threaded through `embed_texts`, `embed_query` and
`embed_pending_chunks`, with a guard: **if the provider returns a different width than was
asked for, the call fails.** This is not paranoia — `litellm.drop_params = True` is set
globally by the engine, so an unsupported parameter is *dropped rather than refused*, and the
request would succeed at the provider's default width. Every vector would be silently wrong
for the index it was destined for, discovered only when the index rejected it.
