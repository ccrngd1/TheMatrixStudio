# Phase 5 step 5b-4 — Measured FTS5 Retrieval Recall

**Status:** COMPLETE. Run 2026-09-05. This is the measurement
`docs/PHASE5-RETRIEVAL-DESIGN.md` deferred to, and it produces a verdict that
goes against the original preference.

Reproduce with:

```bash
scripts/measure_retrieval_recall.py docs README.md PHASE4-REPORT.md CHANGELOG.md \
    --sample 60 --k 5 --compare --diluted --json-out /tmp/recall.json
```

`--diluted` adds the engine-shaped query arm; `--compare` A/Bs the tuned query
pipeline against the baseline. Without those flags only the two bounding arms of
the baseline pipeline are measured.

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

Corpus: this project's own documentation — **16 files, 381 chunks, 227,946
chars** — which is a realistic instance of the intended use case (attach specs
and reports to a persona). 60 sampled chunks >= 300 chars, k=5, seed 11; 59
yielded usable generated queries. All figures below come from that single run so
the tables are mutually consistent.

## Results

| Metric | natural | paraphrased |
|---|---|---|
| recall@1 (strict) | 0.627 | 0.102 |
| recall@3 (strict) | 0.864 | 0.220 |
| **recall@5 (strict)** | **0.966** | **0.254** |
| recall@5 (lenient) | 0.983 | 0.271 |
| MRR (strict) | 0.757 | 0.157 |
| zero-result rate | 0.000 | 0.000 |
| mean lexical overlap | 0.701 | 0.184 |
| n (queries) | 59 | 59 |

Random-guess recall@5 on this corpus: **0.0131**. Query-generation cost: $0.044.

"Lenient" counts an adjacent chunk in the same document as a hit, because chunks
overlap by design and a neighbour genuinely contains part of the gold text. It
barely moves the numbers, so chunk overlap is not masking or inflating anything.

## What this says

**FTS5 is excellent when the query shares the document's vocabulary and poor when
it does not.** 0.966 vs 0.254 recall@5 — a 3.8x difference driven purely by
wording. That is the lexical gap, quantified rather than asserted.

Both arms beat chance by a wide margin (0.254 is still 19x the 0.0131 baseline),
so paraphrased retrieval is not *useless* — it is just unreliable.

### The real operating point, measured directly

A third arm models the engine's actual query shape. The engine does not send a
tidy question — it ORs terms from a window of recent conversation, so the real
query is a good question buried in unrelated chatter. The **diluted** arm
reproduces that by padding the natural question with terms drawn from an
unrelated chunk:

| arm | mean overlap | recall@5 strict | recall@3 strict | MRR |
|---|---|---|---|---|
| natural | 0.701 | 0.966 | 0.864 | 0.757 |
| **diluted** (engine-shaped) | **0.272** | **0.509** | **0.288** | **0.184** |
| paraphrased | 0.184 | 0.254 | 0.220 | 0.157 |

**Real-shape recall@5 is ~0.51** — FTS5 finds the right passage about half the
time under the engine's own query construction. This supersedes the interpolated
45–65% estimate below, which is retained only because it was arrived at
independently and landed on the same answer.

### The interpolated estimate (superseded, kept for cross-check)

The arms are bounds, not predictions. This tool's queries are not typed by a
human — they are auto-built from the topic plus recent conversation. So the
question is how much conversational text overlaps the attached documents'
vocabulary.

Measured over the actual `document.retrieved` events from the live runs
(**n = 14** turn-queries, real documents):

| | mean overlap |
|---|---|
| paraphrased arm | 0.184 |
| **real turn-queries** | **0.394** (median 0.417, range 0.208–0.867) |
| natural arm | 0.701 |

Real usage sits between the bounds, nearer the lower half. Interpolating
crudely against the two measured arms puts real recall@5 in the region of
**45–65%** — i.e. **FTS5 plausibly misses the right passage a third to a half of
the time in this tool's actual query pattern.**

That interpolation is the weakest claim in this document: n=14 is a very small
sample, overlap is not the only determinant of BM25 success, and two points do
not establish a curve. Treat it as a located estimate, not a measurement.

### A hazard the recall numbers do not show

**The zero-result rate is 0.000 in every arm.** FTS5 essentially always returns
*something*. Combined with 25% recall in the paraphrased arm, that means in ~75%
of hard queries a persona receives a **confidently irrelevant passage** rather
than nothing. There is currently no score threshold below which retrieval says
"no supporting passage found", so the failure mode is silent and the persona may
cite material that does not support its claim.

This is precisely the objection the premise-validation panel raised — *"a
retrieval feature that looks grounded in a demo and returns irrelevant passages
the moment the corpus is real."* It was recorded as `overgeneralised` at the time;
on this evidence the concern was better founded than the calibration assumed.

## The cheap fixes were tried first, and both failed

Before recommending embeddings, the two free changes were implemented and A/B'd
against the same ground truth. **Both made recall worse, on every arm.**

| recall@5 strict | natural | diluted | paraphrased |
|---|---|---|---|
| baseline | 0.966 | **0.509** | 0.254 |
| tuned (discriminative terms + score filter) | 0.966 | **0.339** | 0.237 |

`recall@3` fell too (0.864→0.848, 0.288→0.220, 0.220→0.170), as did MRR in every
arm. The regression is largest precisely on the diluted arm the changes were
designed for.

**Why discriminative term selection fails — a structural reason, not a tuning
problem.** Document frequency measures *rarity*, not *relevance*. When a query
mixes a real question with conversational filler, the filler frequently supplies
the rarest terms, so rarity-ranking promotes noise and can discard the
common-but-relevant terms that were doing the matching. No choice of
`term_limit` or `max_df_ratio` fixes that; the signal is simply wrong.

**Why the score filter fails, twice over.** It was meant to make "no supporting
passage found" reachable. It cannot: the filter is *relative*, so the best row
always clears its own threshold, and the zero-result rate stayed **0.000** with it
enabled. Meanwhile the gold passage is often ranked below the top match, so
trimming the weak tail trims real hits. Making an empty result reachable needs an
**absolute** score floor, which requires calibration this measurement has not done.

Both knobs are retained in `RetrievalConfig` but **default to off**
(`term_limit=0`, `score_ratio=0`), with the negative result recorded in their
docstrings so nobody re-enables them on the strength of the plausible-sounding
rationale that motivated them here.

## Verdict

**The design doc's stated trigger for moving to vectors has been met.** It said:
*"Measured recall on a real corpus shows FTS5 missing passages a persona needed to
defend a position."* At a directly measured **0.509 recall@5 under the engine's own
query shape**, that condition holds — a persona misses its supporting passage
roughly half the time.

And the cheap alternatives are now *tested rather than assumed*: query-side
tuning did not help, so the remaining levers are the ones that cost something.

1. **Embeddings via `sqlite-vec`** are now the justified next step. The migration
   stays additive — `doc_chunks` already holds the text, chunking and scoping and
   the retrieval interface are unchanged — and the operational reasons for staying
   inside SQLite (atomicity, one file, no extra service) are untouched by this
   result. The cost that must be accepted is an embedding provider: API calls at
   ingest and per query, or `torch` locally.
2. **An absolute score floor** remains worth building, separately from recall, so
   a weak match can return nothing instead of confidently wrong grounding. It
   needs calibration data this measurement did not gather.
3. **Better query construction may still help**, but not via rarity. Extracting
   the salient noun phrases from the *current utterance* rather than ORing a
   window of loose terms is a different hypothesis, and it is untested.

Nothing in the storage decision is invalidated: the reason for choosing FTS5 over
FAISS was operational (atomicity, no embedding provider, one file), and this
measurement speaks only to *retrieval quality*, which was always the open
question.

Nothing in the storage decision is invalidated: the reason for choosing FTS5 over
FAISS was operational (atomicity, no embedding provider, one file), and this
measurement speaks only to *retrieval quality*, which was always the open
question.

## Limitations

- **The query generator is an LLM.** "Paraphrased" is only as adversarial as the
  model chose to be; a human adversary could do worse. The overlap figures
  (0.701 / 0.184) are the honest calibration of how hard each arm actually was.
- **n = 59 queries per arm, one corpus, one seed.** The corpus is this project's
  own documentation, which is dense technical prose. A corpus of policy documents
  or transcripts could behave differently.
- **n = 14 real turn-queries** for the interpolated cross-check. This should be
  re-run after more real usage before it carries any weight.
- **The diluted arm is a model, not a recording.** It pads a real question with
  terms from one unrelated chunk. Real conversation drifts more coherently than
  that and would echo the topic more, so 0.509 is plausibly a slight
  underestimate of real recall@5. It is nonetheless the closest measurement
  available to the engine's actual query shape, and it agrees with the
  independent interpolation from real queries (45-65%).
- **Recall is not the whole story.** A retrieved passage that is topically right
  but does not actually support the persona's claim still counts as a hit here.
  Faithfulness of use is not measured; `document_refs` exists so it can be
  audited separately.
