# Phase 5 step 5b-4 — Measured FTS5 Retrieval Recall

**Status:** COMPLETE. Run 2026-09-05. This is the measurement
`docs/PHASE5-RETRIEVAL-DESIGN.md` deferred to, and it produces a verdict that
goes against the original preference.

> **Superseded for current numbers by `docs/PHASE3-RECALL-MEASUREMENT.md`
> (2026-09-11).** Every figure below was taken on **SQLite FTS5 + sqlite-vec**, all
> three of which the AWS port replaced — the lexical arm is in-process BM25 now, the
> vector store is S3 Vectors, and the similarity metric is cosine rather than L2. The
> re-measurement reproduces the vector arm and the central conclusion, so this
> document's *reasoning* stands; quote its numbers only as the pre-port baseline.
>
> Two things here are now known to be wrong rather than merely superseded, and both
> are corrected in the newer doc: the score-floor discussion assumes a conversion that
> made the floor **inert** in practice, and the "no absolute floor is calibrated" gap
> has since been closed and validated against 120 real queries.

Reproduce with:

```bash
# lexical bounds + the engine-shaped arm + the rejected query tuning
scripts/measure_retrieval_recall.py docs README.md PHASE4-REPORT.md CHANGELOG.md \
    --sample 60 --k 5 --compare --diluted --json-out /tmp/recall.json

# lexical vs vector vs hybrid (needs the 'vectors' extra + an embedding provider)
scripts/measure_retrieval_recall.py docs README.md PHASE4-REPORT.md CHANGELOG.md \
    --sample 60 --k 5 --diluted --modes baseline,vector,hybrid
```

`--diluted` adds the engine-shaped query arm; `--compare` A/Bs the rejected query
tuning against the baseline; `--modes` selects which retrieval pipelines to
evaluate. Without those flags only the two bounding arms of the lexical pipeline
are measured.

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

## Vectors, measured (5f)

`sqlite-vec` plus Titan Embed v2 embeddings were then built and measured against
the same ground truth, adding `vector` (embeddings only) and `hybrid`
(Reciprocal Rank Fusion of both) to the `fts` baseline.

**recall@5 (strict)**

| arm | fts | vector | hybrid |
|---|---|---|---|
| natural | 0.900 | 0.933 | **0.950** |
| **diluted** (engine-shaped) | 0.400 | **0.817** | 0.700 |
| paraphrased | 0.267 | **0.617** | 0.550 |

**recall@1 (strict)** — matters most at `k=1..3`, which is what a turn actually gets

| arm | fts | vector | hybrid |
|---|---|---|---|
| natural | 0.617 | 0.583 | **0.700** |
| **diluted** | 0.017 | **0.367** | 0.233 |
| paraphrased | 0.100 | **0.267** | 0.167 |

**MRR (strict)**: natural 0.725 / 0.723 / **0.814**; diluted 0.127 / **0.549** /
0.409; paraphrased 0.160 / **0.375** / 0.301.

### What this settles

**Vectors work, decisively, on the query shape this engine actually produces.**
Diluted recall@5 goes 0.400 → 0.817 (2.0×) and diluted recall@1 goes 0.017 →
0.367 (22×). The `recall@1` figure is the one that matters most in practice: a
turn injects only `k` passages, and lexical search was putting the right passage
first almost never.

**Hybrid is not uniformly better, and that is worth stating plainly.** It wins on
the *natural* arm (best recall@1, recall@5 and MRR of any mode) but **loses to
pure vector on both diluted and paraphrased**. Equal-weight RRF lets a weak
lexical ranking drag down a strong semantic one; when the query is well-formed the
lexical list is good and fusion helps, and when it is diluted the lexical list is
noise and fusion hurts. `reciprocal_rank_fusion` accepts weights and tuning them
would likely fix this, but that tuning has not been done, so no weighted result is
claimed.

Practical reading: **`mode="vector"` for engine turns** (diluted queries),
**`hybrid` for the human-facing `/documents/search`** (well-formed queries).

### Cost, which was the gate

| | measured |
|---|---|
| Embedding a 231,744-char corpus (387 chunks) | **$0.0014** (69,864 tokens) |
| Per-turn query embedding | ~**$0.0000001** |
| Generation, per turn, for comparison | ~$0.0006 |

Ingest is a one-off fraction of a cent per corpus and the per-turn embedding is
roughly **one four-thousandth** of the turn's generation cost. The cost objection
that motivated trying the free fixes first does not survive contact with the
numbers.

### Caveats on these figures

- **`diluted/fts` measured 0.400 here versus 0.509 in the earlier run.** Same
  method, different corpus size (387 vs 381 chunks) and randomly drawn filler, so
  there is real run-to-run variance of ~0.1 on this arm. Treat single-arm figures
  as ±0.1, and prefer the *direction* of the fts→vector gap (which is far larger
  than that variance) over its exact size.
- **One embedding model.** Titan Embed v2 only; Cohere and OpenAI embeddings were
  confirmed callable but not evaluated for quality.
- **Vector search post-filters for scope.** vec0's MATCH cannot join in-predicate,
  so the KNN scan is over-fetched then filtered by persona. Correctness holds (a
  test asserts a persona cannot cross slices) but on a run with many personas some
  scan effort is wasted.

## Unsupported-claim disclosure (5g)

The recall numbers say a persona often has no supporting passage. Nothing in the
transcript said so: a grounded claim and an ungrounded one looked identical to
whoever reads the conversation, with `document_refs` visible only in the event
log. So when retrieval runs and returns nothing, the persona is now asked to say
so in its own voice.

**The wording is about PROVENANCE, not evidentiary support**, and that distinction
is the whole design. The engine knows only "nothing was retrieved". It does *not*
know the corpus lacks support — at 0.82 recall the passage often exists and was
simply missed, so "no documentation supports this" would be **false roughly one
time in five**. An honesty feature that lies is worse than none. The shipped
wording therefore says "nothing in front of you", which is true by construction.

### Choosing the wording by measurement, and getting the instrument wrong twice

Three wordings were A/B'd live on a run where one persona has documents and the
other has none (so retrieval reliably returns nothing for that persona):

| wording | what the model actually produced |
|---|---|
| conditional — *"if you make a factual claim…"* | implicit only: *"I've been in enough customer conversations…"* — sounds experienced, never states absence |
| **directive** — *"say so explicitly, in your own words"* | **explicit, varied, in character: "I don't have the actual specs in front of me"** |
| directive + example phrase | explicit, but echoed the example near-verbatim — the stock-phrase tic the design set out to avoid |

**The directive wording ships.** Two measurement mistakes are worth recording,
because both nearly produced a wrong decision:

1. **A regex scored compliance 0/3 for every wording.** It was looking for
   "don't have a document/source"; the model said "I don't have the *specs* in
   front of me". The feature was working and the detector was blind. This is the
   same failure that nearly shipped the harmful query tuning: trusting an
   instrument that was never validated.
2. **An LLM judge then scored every wording 100%.** A negative control (5 hand-
   written cases with known answers) showed it scores **4/5**, wrongly crediting
   "I've closed enough deals to know…" as disclosing absence. It is lenient in
   exactly the direction that inflates the numbers, so those 100% figures are an
   upper bound, and the conditional arm — whose hedges were of precisely that
   experiential-but-not-absence kind — is the one most likely over-credited.

The decision was therefore made by **reading the generated text**, with the two
instruments used only to locate what to read. Verified end to end: 3/3 turns with
no retrieved passage disclosed absence, in three different phrasings, while the
persona that *did* have passages cited them by name and never hedged spuriously.

An unforced bonus: one turn produced *"I haven't seen that distribution
constraints doc you're referencing"* — the persona correctly noticing it cannot
see another persona's slice, which makes the SQL-level scoping legible inside the
conversation.

### Honest limits

- **n = 2-3 disclosure turns per arm, one topic, one model.** Directional, not a
  rate. No compliance percentage should be quoted from this.
- **It is a prompt request, not a gate.** A model can ignore it; the
  `document.unsupported` event is the authoritative record and a test asserts the
  event fires even when the model does not comply.
- **The inverse inference is unsound.** An un-hedged turn does *not* mean the claim
  is supported — a persona can hold a passage and still say something it does not
  back. The signal is one-directional on purpose, and no positive "this is
  documented" marker was added, because the engine cannot verify that.
- Default OFF (`disclose_unsupported: false`).

## Absolute similarity floor (5h) — calibrated, and narrower than hoped

The hazard recorded above was that retrieval always returns *something*, so a
persona can be handed a confidently irrelevant passage. The fix proposed was an
absolute score floor. It is now built — but calibration showed it can only do
**half** of what was intended, and the half it cannot do is the more interesting
one.

Calibration: 180 real retrievals from the vector arm, recording the best-match
cosine and whether the gold passage was actually found.

| | n | min | p10 | median | p90 | max |
|---|---|---|---|---|---|---|
| **HIT** (right passage retrieved) | 137 | +0.228 | +0.344 | +0.506 | +0.693 | +0.870 |
| **MISS** (wrong passage retrieved) | 43 | +0.166 | +0.280 | +0.426 | +0.564 | +0.699 |

**The distributions overlap almost completely.** A miss reached 0.699 — above the
*median* hit. Sweeping the threshold shows there is no knee, only a trade:

| floor | hits kept | junk rejected |
|---|---|---|
| 0.15 | 100% | 0% |
| 0.30 | 96% | 12% |
| 0.35 | 90% | 21% |
| 0.40 | 81% | 40% |

So **no threshold separates a right passage from a wrong one.** A floor tuned to
catch wrong-passage retrieval would pay real recall for it.

### What the floor *can* do

Genuinely off-topic queries are a different matter. Probed directly, queries with
nothing to do with the corpus score cosine **−0.00 to +0.07** — far below the
*lowest* observed genuine hit (0.228):

| probe query | cosine |
|---|---|
| "measured token delta cost" | +0.43 |
| "external service install story" | +0.39 |
| "banana bread proofing time" | +0.07 |
| "medieval falconry glove" | −0.00 |

**The floor therefore ships as an off-topic guard, not a relevance filter**, at
`min_similarity: 0.15` — below the weakest measured hit with margin, so its
measured cost is **0 of 137 hits**. Note the sweep shows 0% junk rejected at 0.15;
that is expected and not a failure, because every calibration query was *about*
the corpus by construction. The guard's value is demonstrated by the probe, not by
the sweep.

Verified end to end on a deliberately off-topic run (falconry glove leather, with
a distribution/cost corpus attached): the floor rejected both matches on every
turn (`floor_rejected=2`), which produced no passages, which fired the 5g
disclosure, and all three personas stated the absence in their own words —
*"I don't have any reference materials in front of me for this one, so I'm working
purely from what I've seen on the job."* Without the floor, vector search would
have offered a cost-observations passage as background for a falconry question.

### Limits

- **BM25 gets no floor.** Equally relevant queries scored −0.677 and −3.760 in the
  same corpus; the scale is query-dependent, so no fixed value transfers. `fts`
  mode is unguarded, though it does naturally return nothing for queries whose
  vocabulary is absent from the corpus.
- **Wrong-passage retrieval remains undetected.** This is measured, not assumed,
  and a test locks it so the default is not "improved" upward on intuition.
- **Only valid for unit-norm embeddings.** Titan Embed v2 is exactly unit-norm
  (verified); a provider that is not would make the cosine conversion meaningless,
  so the floor is skipped with a warning rather than misapplied.
- One corpus, one embedding model, one run.

## Citation provenance (5i) — attributed hearsay, not suppression

### The observed failure

Scanning 92 turns across 10 run databases for citations and checking each against
the turn's `document_refs`:

| | count |
|---|---|
| turns containing a citation | 14 |
| citation matched a passage in context | 14 |
| **document NOT in this turn's context** | **1** |
| wrong chunk ordinal of a held document | 0 |
| cited when no passages were retrieved | 0 |

The single failure, in full: Priya retrieved `phase4-report.md #31` (her own
document) and cited it. On the next turn **Dana** — whose only document is
`project-spec.md`, and who retrieved only from it — wrote *"which is what
phase4-report.md #31 actually specifies"*. She lifted the label out of Priya's
turn and asserted what a document she had never seen contains.

**This is an architectural gap, not a model quirk.** SQL scoping stops a persona
*reading* another's slice; nothing stopped it *citing* one. A persona could borrow
another's evidential authority by name, which defeats the purpose of per-persona
slices.

### Why suppression was the wrong fix

The first plan was "only cite documents you have read". That destroys information:
in a real review evidence legitimately propagates through people — an SME shows
you a document, you report back, and the record says *"Priya cited X as saying
Y"*. Dana **should** be able to reason with what Priya surfaced. The error is not
using it; it is presenting second-hand evidence as first-hand.

So a citation is legitimate when it is either:

- **first-hand** — the label is among the passages this turn retrieved; or
- **second-hand** — attributed to a participant who really did cite it, with the
  document in their own retrieved passages.

Anything else attributive is **unverified**. This completes a distinction the
engine already drew — `document_refs` (I retrieved this), `memory_refs` (I
remember this), the 5g disclosure (I have nothing) — with "someone else surfaced
this".

### Only attributive use is judged, and that mattered immediately

Merely naming a document is honest and must never be flagged. A live run produced
*"I haven't seen that distribution-constraints.md doc you're referencing"* — the
correct behaviour, and a naive membership test would have rejected it.

A second real case settled the design: a persona wrote *"see [UPGRADE-PATH.md]
for rationale"* **inside a proposed code comment** — suggesting a document be
created, not asserting what one says. Classified `mention`, non-attributive,
correctly left alone. A binary "label not in my refs -> violation" rule would have
regenerated a perfectly good turn.

So the check requires an attribution cue (per / according to / specifies / states
/ shows / confirms ...) near the label, and no disclaimer ("haven't seen", "don't
have", "you're referencing").

### Where it lives

The Phase 4a pre-emit gate, as principle `citation_integrity`, ranked with
coherence at the top of the hierarchy — a false citation corrupts the exported
record, which is the artifact this tool exists to produce. Handling follows 4a
exactly: reject and regenerate on the existing budget, then emit verbatim with a
flag. Output is never rewritten. Zero LLM calls: the check is a set-membership
test against data the engine already holds.

`agent.response` now carries `citation_provenance` —
`{label, kind, attributive, via?}` per citation — which makes an evidence chain
machine-readable. That serves the traceability goal from the spec that started
this work (*"trace from each requirement to the concerns, personas and corpus
documents it derives from"*); previously the second-hand hop was invisible.

### Limits

- **Prevalence is low (1 in 92 turns, 1 in 14 cited turns) on a small sample.**
  The mechanism is real and now guarded, but no rate should be quoted.
- **The gate's live catch rate is unverified.** A re-run of the original scenario
  produced no illegitimate citation at all, so the rejection path is proven only
  by tests with a mocked bad citation — not yet by catching a real one.
- **Entailment is still not checked** — DEFERRED with evidence and a revisit
  trigger; see `docs/BACKLOG.md`. A first-hand citation of a passage that does not
  actually support the claim remains undetected. That needs an LLM judge and a
  measured base rate, and it consumes this stage's output (a first-hand cite is
  verified against the passage, a second-hand one against the transcript).
- **Drift across hops is not modelled.** Dana can faithfully relay a claim Priya
  got wrong; hop count is recorded implicitly via `via` but not capped.

## Verdict

**The design doc's stated trigger for moving to vectors has been met.** It said:
*"Measured recall on a real corpus shows FTS5 missing passages a persona needed to
defend a position."* Directly measured under the engine's own query shape, lexical
recall@5 is **0.40-0.51** across two runs — a persona misses its supporting
passage about half the time, and recall@1 is near zero (0.017).

And the cheap alternatives are now *tested rather than assumed*: query-side
tuning did not help, so the remaining levers are the ones that cost something.

1. **Embeddings via `sqlite-vec`** — BUILT and MEASURED (see the section above).
   Diluted recall@5 0.400 -> 0.817, diluted recall@1 0.017 -> 0.367, at $0.0014
   to embed a 231k-char corpus and ~$1e-7 per turn. The migration was additive as
   predicted: `doc_chunks` already held the text, so chunking, scoping and the
   retrieval interface did not change, and the index still lives in the one
   SQLite file.
2. **An absolute score floor** remains unbuilt and still worth doing, separately
   from recall, so a weak match can return nothing instead of confidently wrong
   grounding. Vector distances are better behaved for thresholding than BM25
   scores, so this is more tractable now than it was, but it still needs
   calibration data this measurement did not gather. The zero-result rate remains
   0.000 in every mode.
3. **Better query construction may still help**, but not via rarity. Extracting
   the salient noun phrases from the *current utterance* rather than ORing a
   window of loose terms is a different hypothesis, and it is untested. It matters
   less now: embedding the raw conversational text sidesteps keyword construction
   entirely, which is part of why the vector arm wins.

4. **Weighted hybrid fusion** is the clearest remaining lever. Hybrid already
   beats every mode on well-formed queries; equal weighting is what makes it lose
   on diluted ones. `reciprocal_rank_fusion` takes weights; tuning them against a
   held-out split is untested work.

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
