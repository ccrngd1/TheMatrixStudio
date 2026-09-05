# Phase 5 step 5b-4 — Measured FTS5 Retrieval Recall

**Status:** COMPLETE. Run 2026-09-05. This is the measurement
`docs/PHASE5-RETRIEVAL-DESIGN.md` deferred to, and it produces a verdict that
goes against the original preference.

Reproduce with:

```bash
scripts/measure_retrieval_recall.py docs README.md PHASE4-REPORT.md CHANGELOG.md \
    --sample 60 --k 5 --json-out /tmp/recall.json
```

## Method

Ground truth is known **by construction**: for a sampled chunk, a model writes a
question that the chunk answers, so the correct passage is known without hand
labelling. Two variants per chunk bracket the result:

- **natural** — the question someone would actually type, free to reuse the
  document's wording. The *optimistic* bound.
- **paraphrased** — the same information need, deliberately avoiding the chunk's
  distinctive vocabulary. The *adversarial* bound, and where lexical search should
  fail.

Reporting both is the point; a single number would hide the lexical gap rather
than measure it. Measured query/passage lexical overlap is reported alongside
recall so the reader can see how hard each arm actually was.

Corpus: this project's own documentation — **15 files, 369 chunks, 220,494
chars** — which is a realistic instance of the intended use case (attach specs
and reports to a persona). 60 sampled chunks ≥ 300 chars, k=5, seed 11.

## Results

| Metric | natural | paraphrased |
|---|---|---|
| recall@1 (strict) | 0.700 | 0.100 |
| recall@3 (strict) | 0.883 | 0.250 |
| **recall@5 (strict)** | **0.967** | **0.300** |
| recall@5 (lenient) | 0.983 | 0.333 |
| MRR (strict) | 0.800 | 0.170 |
| zero-result rate | 0.000 | 0.000 |
| mean lexical overlap | 0.708 | 0.178 |
| n (queries) | 60 | 60 |

Random-guess recall@5 on this corpus: **0.0136**. Query-generation cost: $0.045.

"Lenient" counts an adjacent chunk in the same document as a hit, because chunks
overlap by design and a neighbour genuinely contains part of the gold text. It
barely moves the numbers, so chunk overlap is not masking or inflating anything.

## What this says

**FTS5 is excellent when the query shares the document's vocabulary and poor when
it does not.** 0.967 vs 0.300 recall@5 — a 3.2× difference driven purely by
wording. That is the lexical gap, quantified rather than asserted.

Both arms beat chance by a wide margin (0.300 is still 22× the 0.0136 baseline),
so paraphrased retrieval is not *useless* — it is just unreliable.

### The real operating point is in the middle, and that is the problem

The arms are bounds, not predictions. This tool's queries are not typed by a
human — they are auto-built from the topic plus recent conversation. So the
question is how much conversational text overlaps the attached documents'
vocabulary.

Measured over the actual `document.retrieved` events from the live runs
(**n = 14** turn-queries, real documents):

| | mean overlap |
|---|---|
| paraphrased arm | 0.178 |
| **real turn-queries** | **0.394** (median 0.417, range 0.208–0.867) |
| natural arm | 0.708 |

Real usage sits between the bounds, nearer the lower half. Interpolating
crudely against the two measured arms puts real recall@5 in the region of
**45–65%** — i.e. **FTS5 plausibly misses the right passage a third to a half of
the time in this tool's actual query pattern.**

That interpolation is the weakest claim in this document: n=14 is a very small
sample, overlap is not the only determinant of BM25 success, and two points do
not establish a curve. Treat it as a located estimate, not a measurement.

### A hazard the recall numbers do not show

**The zero-result rate is 0.000 in both arms.** FTS5 essentially always returns
*something*. Combined with 30% recall in the paraphrased arm, that means in ~70%
of hard queries a persona receives a **confidently irrelevant passage** rather
than nothing. There is currently no score threshold below which retrieval says
"no supporting passage found", so the failure mode is silent and the persona may
cite material that does not support its claim.

This is precisely the objection the premise-validation panel raised — *"a
retrieval feature that looks grounded in a demo and returns irrelevant passages
the moment the corpus is real."* It was recorded as `overgeneralised` at the time;
on this evidence the concern was better founded than the calibration assumed.

## Verdict

**The design doc's stated trigger for moving to vectors has been met.** It said:
*"Measured recall on a real corpus shows FTS5 missing passages a persona needed to
defend a position."* At an estimated 45–65% recall@5 in real query conditions,
that condition holds.

But vectors are **not** the first thing to change, because the measurement also
shows the dominant variable is the *query*, not the index:

1. **Improve query construction first — free, no new dependencies.** Real queries
   average 0.394 overlap partly because they are an unweighted OR over raw
   conversational text, filler included. Weighting topic and utterance terms
   above chat filler, or extracting salient noun phrases instead of bare tokens,
   should raise overlap toward the natural arm without adding an embedding
   provider. The harness now exists to verify that rather than hope for it.
2. **Add a score threshold**, so weak matches return nothing instead of a
   confidently wrong passage. Recall matters less than not fabricating grounding.
3. **Then, if recall is still short, add embeddings** via `sqlite-vec` — the
   migration stays additive because `doc_chunks` already holds the text, and the
   atomicity and single-file arguments for staying inside SQLite are unaffected by
   this result.

Nothing in the storage decision is invalidated: the reason for choosing FTS5 over
FAISS was operational (atomicity, no embedding provider, one file), and this
measurement speaks only to *retrieval quality*, which was always the open
question.

## Limitations

- **The query generator is an LLM.** "Paraphrased" is only as adversarial as the
  model chose to be; a human adversary could do worse. The overlap figures
  (0.708 / 0.178) are the honest calibration of how hard each arm actually was.
- **n = 60 chunks per arm, one corpus, one seed.** The corpus is this project's
  own documentation, which is dense technical prose. A corpus of policy documents
  or transcripts could behave differently.
- **n = 14 real turn-queries** for the operating-point estimate. This should be
  re-run after more real usage before it carries any weight.
- **Recall is not the whole story.** A retrieved passage that is topically right
  but does not actually support the persona's claim still counts as a hit here.
  Faithfulness of use is not measured; `document_refs` exists so it can be
  audited separately.
