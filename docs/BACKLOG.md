# Backlog — open and deferred work

Single index of what is *not* done and why. Open work was previously scattered as
prose across six documents (`Still open`, `deferred`, `Future`), with no way to see
it in one place.

**Every deferral records a revisit trigger.** A deferral without one is just
forgetting: the project's own convention is explicit triggers rather than vibes
(`docs/PHASE5-RETRIEVAL-DESIGN.md`, "What would trigger a move to vectors").

Status key: **OPEN** (wanted, unstarted) · **DEFERRED** (decided against *for now*,
with evidence) · **BLOCKED** (waiting on something external) · **REJECTED**
(measured as not worth doing; kept so it is not retried on intuition).

---

## Validated but unbuilt

### Structured personas (premise-validation Arm B)
**Status:** OPEN — highest-value unbuilt item.

`docs/PHASE5-PREMISE-VALIDATION.md` ran a three-arm experiment and concluded
**"PROCEED with structured personas (Arm B)"**: `dismisses` / `formedBy` /
`firmness` / `evidenceThatShifts` cut the accommodation rate 40%, produced the
lowest cross-speaker similarity *and* the best engagement score of any arm, and
was the only arm in which any participant changed position.

That verdict was never acted on — Phase 5 went to document retrieval instead. The
engine still has no axis for *"what I believe and won't give up"*: goals are
satisfiable, convictions are defended.

Also recorded there: ship this **with the dismissal rule re-tuned**, because Arm C
showed personas retreating into repetitive parallel monologues (talking-past 4/5)
when a hard "judge only against your own priorities" rule met a large always-present
source block.

**Revisit trigger:** none needed — this is a standing recommendation with evidence.

---

## Deferred with evidence

### Entailment checking of citations (Phase 5 "Stage 3")
**Status:** DEFERRED.

Would check whether a legitimately-retrieved passage actually *supports* the claim
attributed to it. Deferred because the problem that motivated it turned out to be a
chunking defect, not model unfaithfulness:

| | before chunker fix | after |
|---|---|---|
| Chunks opening mid-sentence | 90% (1 file) | 1% (171 chunks, 6 files) |
| Cited passages that were fragments | 1 of 18 | 0 of 28 |
| **Clear misreads** | **1 of 18** | **0 of 28** |

The single misread was caused by a chunk beginning `". This is a correctness
requirement, not hardening."` — the antecedent of "This" had been cut off, and the
persona quoted it verbatim and inferred the opposite of the source's meaning. With
readable chunks the failure class disappeared. Residual issues are 5 mild
"partials" (inference slightly beyond the cited chunk, a right quote attributed to
the wrong chunk, one paraphrase inside quotation marks) — too mild and too
ambiguous to justify an LLM call on ~10% of turns plus a false-positive risk that
would regenerate good turns.

**Revisit trigger:** a corpus produces misreads at a measurable rate *with readable
chunks*. The labelling harness exists to detect that
(`scripts/measure_retrieval_recall.py`, plus the extraction approach in
`docs/PHASE5-RETRIEVAL-MEASUREMENT.md`).

**If revisited:** first-hand citations verify against the passage; second-hand ones
verify against the transcript. Recommend **flag-only** initially rather than 4a's
usual reject-and-regenerate, because entailment is a degree judgment and a false
positive degrades a good turn.

### Absolute score floor as a *relevance* filter
**Status:** REJECTED (measured).

Calibrated over 180 retrievals: correct matches span cosine 0.228–0.870 and
incorrect ones 0.166–0.699 — near-total overlap. No threshold separates a right
passage from a wrong one; rejecting 40% of junk costs 19% of hits. Shipped instead
as an **off-topic guard** at `min_similarity: 0.15` (zero measured cost). A test
locks this so the default is not raised on intuition.

**Revisit trigger:** a different embedding model shows separable hit/miss
distributions on the same calibration.

### Query-side tuning (discriminative terms, relative score filter)
**Status:** REJECTED (measured harmful).

Recall fell on all three arms, worst on the arm the change targeted (engine-shaped
recall@5 0.509 → 0.339). Document frequency measures rarity, not relevance, and a
relative score filter can never produce an empty result. Both retained but default
off (`term_limit=0`, `score_ratio=0`) with the negative result in their docstrings.

---

## Open — measurement gaps

### Chunker fix: recall effect is confounded
**Status:** OPEN.

Sentence-aligned overlap measurably fixed fragment chunks, but recall appeared to
drop (engine-shaped vector recall@5 0.817 → 0.712). That comparison is **not a
clean A/B**: changing the chunker changes the gold units and the generated
questions, and measured question difficulty differed (`mean_lexical_overlap` 0.680
→ 0.645). Documented ±0.1 run-to-run variance on that arm covers the whole
difference.

**To resolve:** repeat at several seeds, or score recall at document+span level
rather than chunk level so re-chunking does not move the gold.

### Phase 4 behaviour against real models
**Status:** OPEN. From `PHASE4-REPORT.md` §4 — how often the 4a heuristics fire,
how often the selective LLM confirmation triggers, how well real models use the 4b
`thread_updates` schema, and the quality of 4c pressure events are all unmeasured
against a live model. Phase 5 showed live runs contradicting mocked expectations
three separate times, so this is not a formality.

### Citation gate: false-positive rate unmeasured
**Status:** OPEN. The gate fired **16 times across 48 turns** in one batch, and the
catches read as correct on inspection, but no false-positive rate has been measured
and 4 activations exhausted the retry budget and emitted flagged. Worth labelling a
batch of activations the way the entailment set was labelled.

---

## Open — features

- **Weighted hybrid fusion.** `hybrid` beats every mode on well-formed queries but
  loses to pure `vector` on conversational ones, because equal-weight RRF lets a
  weak lexical ranking drag down a strong semantic one. `reciprocal_rank_fusion`
  already accepts weights; tuning them against a held-out split is untested.
- **Retrieval diversity.** Consecutive turns build near-identical queries and can
  re-retrieve the same passage (observed: one persona got chunk #31 on both its
  turns). No novelty pressure against already-retrieved chunks.
- **Score floor for `fts` mode.** BM25 scores are query-dependent — two equally
  relevant queries scored −0.677 and −3.760 — so no fixed threshold transfers.
- **Hop drift in second-hand citation chains.** A persona can faithfully relay a
  claim another persona got wrong. `via` records one hop; depth is not capped.
- **Embedding-based *memory* retrieval.** Distinct from document retrieval (built
  in 5f). Agent memory is still ranked by importance + recency only; deferred since
  Phase 2c.
- **Cognition-state replay on branch.** `reconstruct_at_turn` replays transcript,
  cost and thread events, but not memory streams / goals / relationships.
  Pre-existing since 2a; unchanged by Phases 4 and 5.

---

## Blocked

- **Push to origin.** 14 commits sit on local `master`. The remote is HTTPS GitHub
  and no credential is available to this user (`gh` absent). Nothing else depends
  on it.
- **Frontend verification.** The Phase 5c Dossier/types changes were reviewed by
  eye but never typechecked or covered by the 18 frontend tests: this machine has
  no Node toolchain (`npm`/`npx` absent, `frontend/node_modules` absent). Needs
  `tsc -b` and `vitest` on a machine with Node.
- **Docker build.** Never verified in any environment used so far (noted in README
  since Phase 3).

---

## Housekeeping

- **CHANGELOG has no Phase 5 section.** 14 commits are unreleased; no version bump
  from 0.4.0.
- **README roadmap** now lists Phase 5 (fixed), but the four screenshot
  placeholders remain `TODO`.
- **`PROJECT-SPEC.md` §4a is stale** — it says the priority hierarchy is "a design
  principle, not yet a code gate". It has been a code gate since Phase 4a, and 5i
  added `citation_integrity` to it.
