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

*(Empty. Structured personas — the only entry this section ever held — shipped as
Phase 6; see below.)*

---

## Measured, with open follow-ups

### Phase 6 dismissal rule: FIXED and measured
**Status:** RESOLVED 2026-09-06. Pre-registered criterion, both conditions passed.

`retuned` suppressed dismissal to 0.067 (the control's rate, two of three runs at
zero). `mandatory` — declining REQUIRED rather than permitted, prohibitions cut from
three to one — measures **0.333** (Arm B is 0.355) with talking-past **1.00**, the
best engagement score of any arm. It is now the default.

The criterion was committed in `6927607` **before** any wording existed
(`docs/PHASE6-DISMISSAL-RETUNE.md`), because the previous round's error was choosing
what counted as success after seeing output.

**The transferable finding**, from four data points:

> A rendered persona instruction must **require an utterance**, not license an
> omission.

| wording | delivery | dismissal rate |
|---|---|---|
| blunt (*"Ignore the things you consider not your problem"*) | hand-written prose | 0.355 |
| blunt — **identical words** | Phase 6 rendering | **0.000** |
| retuned (*"say once, briefly"*) | Phase 6 rendering | 0.067 |
| mandatory (*"you MUST say plainly … not optional"*) | Phase 6 rendering | **0.333** |

The rendering is not a blanket blocker — `mandatory` works through it. Arm B's prose
got away with a permission because it wrapped that sentence in a block of conduct
imperatives supplying force the sentence lacks alone. Worth applying to any future
rendered instruction that wants visible behaviour.

### Phase 6 structured personas: what remains unsupported at n = 3
**Status:** OPEN. The dismissal finding above is resolved; these are not.

- **Nothing about divergence, accommodation, citation rate or turn length is
  callable.** Within-arm spread (B cross-speaker 0.1117-0.1289, spread 0.0172)
  exceeds the between-arm gaps. Even "Arm D writes 37% longer turns", which
  motivated the length-normalisation work, is within run-to-run variation.
- **Evidence-driven position change did not replicate.** "2 of 2" was one run; the
  repeats produced zero. Across the rule arms it stays noisy (E: 0, 0, 2).
- **`distinct_positions` is unstable in every rendered arm** — E scored 5, 5, 3 and
  F scored 2, 2, 5, against Arm B's consistent 5, 5, 5. **Third time this has
  appeared**, and the only signal pointing at a real cost to rendering convictions
  from data rather than prose. Judge variance at n = 3 is unknown, so it is recorded,
  not concluded. Highest-value remaining Phase 6 question.
- **`requires-escalation` FIRED** — 2 of 3 cognition-on runs, 0 of 3 cognition-off
  (`docs/PHASE6-COGNITION-INTERACTION.md`). First time in fifteen runs. Suggestive, not
  established: 2/3 vs 0/3 at n=3 is ~p 0.4, and the all-persona escalation count is
  below the noise gate. Plausible mechanism — escalation needs *sustained* pressure to
  concede and without memory every turn starts fresh. **The single most worthwhile
  thing to run more of** — but not by repeating identical runs. The efficient route is
  a brief engineered so a persona genuinely gets **overruled**, which raises the base
  rate so fewer runs settle it. Same trick that made the dismissal question tractable.
- **The concern-reveal path is still untested** — nobody asked "why" in any of fifteen
  runs. Unchanged.
- ~~**Cognition ON** has never been run in any arm.~~ DONE — Arm G, n=3 at 30 turns.
  Found and fixed two engine defects on the way (see below). Result: the pre-registered
  concern-leak does **not** happen (100 memories read, zero leaks); reflections
  *reinforce* convictions rather than eroding them; the only callable metric is that
  cognition makes turns **~30% shorter** (807 → 563 chars), cause unmeasured.
- **Arms A and C are still n = 1**, so their own noise is unmeasured.

**Resolution floor for this harness:** at 15 turns and 5 personas it cannot resolve
differences below ~**0.02** in cross-speaker similarity or ~**0.2** in the rate
metrics. Several previously published conclusions, including the original
experiment's headline 0.183-vs-0.160, sit inside that band.

### Engine: two shipped features were silently broken against real models
**Status:** FIXED 2026-09-06, locked by `tests/test_jsonio.py`.

Both found by trying to run cognition for the first time. Both fail *silently* — the
run completes and reports success — which is why neither was caught earlier.

1. **Strict JSON parsing.** Haiku 4.5 wraps structured output in a markdown fence;
   `json.loads` rejected it. Consequences: **cognition completely inert** (30/30 turns,
   0 memories, 0 reflections, 0 rationales, while costing *more* than not using it —
   shipped since v0.2), and **the Phase 4a validation gate silently dropping every
   suspicion** (its confirmation call fails open, so a `JSONDecodeError` became
   `violation: False` — so the selective LLM confirmation had never confirmed
   anything). Fixed by one tolerant parser, `matrix_studio/jsonio.py`, now used by all
   five call sites. Two other modules had already solved this independently
   (`analysis.py` in Phase 1.5, `naming.py`) while the engine and gate never learned it
   — that duplication is why the lesson did not spread.
2. **Provider parameter restrictions.** Sonnet 5 accepts only `temperature=1`; the
   engine passes 0.7 / 0.3 / 0.0. Every call raised `UnsupportedParamsError` and the
   engine wrote the error text into the transcript **as the character's speech**, with
   the run reporting `complete` and `$0.0000` cost. Fixed with
   `litellm.drop_params = True`.

The two are independent and both needed: the JSON fix alone still breaks on Sonnet,
`drop_params` alone still breaks on Haiku.

**Revisit trigger:** any new model. This class of defect is only findable by running
against the real thing, and `PHASE4-REPORT.md` §4 had flagged exactly this gap.

### Premise-validation scorer: instrument defects found and fixed
**Status:** FIXED 2026-09-06, locked by `tests/test_validation_scoring.py`.

Recorded because both defects had already produced a wrong published conclusion,
and because the failed fixes are worth not retrying.

**Length bias.** Every overlap metric on accumulated text grows with volume: the
same arm truncated to 300-char turns scores 0.105 and at full length 0.160, same
speakers, same positions. Arm D's turns are 37% longer than Arm B's, so the raw
comparison was unreadable and the first reported reading of it ("Arm D is less
divergent than the control") was **backwards**.

Three fixes tried and REJECTED, each with the measurement that killed it:
- Subsample the token *stream* to a fixed count — equalises count, not vocabulary
  size (0.298 vs 0.243 on a fixture with identical true overlap).
- Subsample the *vocabulary* to a fixed size — worse (0.287 vs 0.145); drawing N
  words from differently-sized vocabularies changes the chance of drawing the
  shared ones.
- TF-cosine instead of Jaccard — also length-sensitive (0.235 → 0.392 under the
  same sweep).

What works: truncate every speaker to a common token volume, then apply the
original metric. Deterministic, no seed.

**A too-aggressive robustness check.** The first budget sweep included 40-80 token
budgets, where the ordering scrambles completely. Eighty content tokens is a couple
of sentences — too little for vocabulary overlap to mean anything. Including them
reported *every* arm pair as uncallable and hid the real result. Now floored at 100
tokens with the sweep recorded.

**Dismissal idiom.** `DISMISSAL` was authored against the original three arms and
missed bare-possessive forms. Arms B and D were hand-labelled first
(`docs/labels/dismissal-labels.json`), then patterns patched until they reproduced
the reading — that order matters, because tuning patterns against a number rather
than a reading is how the defect got in. Finding: the regex under-counted **both**
arms by one turn, so Arm D's lower dismissal rate is real behaviour, not an
artifact.

**Revisit trigger:** arms A and C are still unlabelled, so their dismissal rates
rest on the regex alone. Label them before quoting those two rates as measured.

### Phase 6: no validation gate for abandoned convictions
**Status:** OPEN (deliberately not built yet).

The obvious 4a-style gate would flag a persona conceding a `firm`-or-above
position when nothing on its `evidence_that_shifts` list appeared in the
conversation — the structured-persona analogue of `citation_integrity`. Not built,
because "conceded a position" is a degree judgment and a false positive would
regenerate a *good* turn where a persona legitimately changed its mind. Same
reasoning that deferred entailment checking below.

**Revisit trigger:** a run shows convictions being abandoned at a measurable rate
despite the prompt rule. Arm D is weak evidence *against* needing it — both position
changes there were legitimate (one fired on its exact `evidence_that_shifts`, one
was a `negotiable` position shifting on argument, which is permitted), so the gate
would have had nothing correct to catch and two chances to be wrong. If built,
recommend **flag-only** first, like the entailment note below.

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
**Status:** OPEN — but one of its questions was answered, accidentally and badly.

From `PHASE4-REPORT.md` §4: how often the 4a heuristics fire, how often the selective
LLM confirmation triggers, how well real models use the 4b `thread_updates` schema, and
the quality of 4c pressure events were all unmeasured against a live model.

**"How often does the selective LLM confirmation trigger?" now has an answer: never.**
It parsed its response strictly inside a fail-open handler, so against a fenced-JSON
model every `JSONDecodeError` became `violation: False`. Fixed 2026-09-06 (see *Engine:
two shipped features were silently broken*), but the rate it fires at is **still
unmeasured** — now for the first time measurable.

The rest stands, and today is the argument for doing it: this gap was hiding two
silently-broken shipped features, not merely missing numbers.

### Scoring phrase lists are validated against Haiku only
**Status:** OPEN — free to fix, and it gates every future rate.

`ACCOMMODATION` and `DISMISSAL` in `scripts/score_validation.py` were hand-labelled
against **Haiku 4.5 output at 15 turns** (`docs/labels/dismissal-labels.json`). The
default model is now **Sonnet 5**, so every future run inherits lists that were never
checked against the idiom they will be scoring.

Evidence this is not theoretical: the same Arm E configuration scores accommodation
**0.033** on Sonnet at 30 turns against **0.400** on Haiku at 15. Model and turn count
both changed at once, so the gap is not attributable — but a 12× difference in a rate
metric is not something to quote past.

**Until this is done, no rate from a Sonnet run may be compared to any Haiku number,
and the pre-registered ≥0.30 dismissal criterion does not transfer across turn counts.**

**To resolve:** hand-label a batch of Sonnet turns the way B and D were labelled, then
patch the patterns until they reproduce the reading — labelling *first*, because tuning
patterns against a number rather than a reading is exactly how the original defect got
in. Arms A and C remain unlabelled on Haiku too.

### Citation gate: false-positive rate unmeasured
**Status:** OPEN. The gate fired **16 times across 48 turns** in one batch, and the
catches read as correct on inspection, but no false-positive rate has been measured
and 4 activations exhausted the retry budget and emitted flagged. Worth labelling a
batch of activations the way the entailment set was labelled.

---

## Open — new capability: consultable non-participants

### Non-participant SMEs you can query
**Status:** OPEN — never built, and never previously recorded, which is why this entry
exists.

Every way of asking a question today targets someone **already in the conversation**.
Aside conversations (Phase 1.5, `CreateThreadModel.target`) accept `analyst`,
`persona` or `room` — the analyst reasons over the transcript, and the other two are
participants. There is no notion of a person who was *not* in the room, holds their own
documents, and can be consulted without joining the discussion.

**Why it matters.** A five-stakeholder panel arguing about retrieval quality contains
nobody who actually knows how FTS5 behaves — they argue from priors. That is visible in
the real transcripts: personas repeatedly demand a measurement rather than knowing an
answer. A consultable SME injects grounded fact instead of a sixth opinion.

**Most of the machinery already exists:**

| piece | state |
|---|---|
| Documents scoped to a named persona | built (Phase 5) |
| A question-and-answer path outside the main turn loop | built (Phase 1.5 asides) |
| Provenance for "X told me and pointed at Y" | built (Phase 5i — `via` already models exactly this hop) |
| A cast member excluded from speaker selection | **missing** |

So the gap is narrow: a flag on the persona plus a filter in `_select_next_speaker`,
reusing the aside path for the query. The provenance model needs no change — this is
the situation the attributed-hearsay design was written for, and the README's own
justification is literally *"an SME shows you a document, you report back"*
(`README.md:245`).

**Design questions to settle first, not during:**

- Does an SME's answer enter the transcript as a turn, or as an aside that a
  participant must then *relay* (making it second-hand and citable as such)? The second
  is more faithful to the citation model and keeps the SME out of turn order.
- Can a participant consult an SME mid-run on its own initiative, or only the operator
  between turns? Agent-initiated consultation is a new causal path and would need to
  appear in the event log to stay auditable.
- Does the SME hold convictions (Phase 6) or only knowledge? An SME with `dismisses`
  is arguably a sixth stakeholder wearing a lab coat.

**Revisit trigger:** none needed — this is a standing capability gap with a clear
motivation. It is small enough to build and the honesty machinery already covers it.

### SME web search
**Status:** OPEN — design settled, not built. The architecture question is answered by
the project's own existing seam; the open work is the two pieces that seam does not
cover.

The ask: an SME should be able to search the web, not only attached documents.

**The replay conflict is resolved, by decision (operator, 2026-09-06):** anything found
online is **brought down into the SME's own document repo and knowledge base**, so it is
referenced later and on replay exactly like any other attached document. That makes
search a **URL-discovery mechanism**, not a live per-turn lookup, and it means the
event-sourcing invariant is preserved rather than worked around — replaying a run reads
the ingested snapshot, not the live page.

#### Use LiteLLM's `search()`, not a chosen vendor

`litellm.search()` / `asearch()` already exist and take the same shape as the
completion and embedding calls this project already routes through LiteLLM:

```python
litellm.search(query=..., search_provider=..., max_results=...,
               search_domain_filter=[...], api_key=..., api_base=...)
# -> SearchResponse.results[].{title, url, snippet, date}
```

Nineteen providers are supported, including **both** candidates that were being weighed
— `SEARXNG` and `AGENTCORE` (Bedrock) — plus `TAVILY`, `BRAVE`, `DUCKDUCKGO`, `EXA_AI`,
`FIRECRAWL`, `PERPLEXITY`, `SERPER`, `GOOGLE_PSE` and others.

So *"SearXNG or Bedrock?"* is **not an architecture decision** — it is one config value,
exactly as `LITELLM_MODEL` and `RetrievalConfig.embedding_model` already are. Copy that
precedent. It also means the choice does not have to be right on the first attempt,
which matters: this project was bitten twice in one session by assuming a provider's
behaviour (fenced JSON, temperature restrictions), and hard-coding a search vendor
would be the same mistake with a larger blast radius.

**Perplexica: rejected, on design grounds rather than cost.** It is SearXNG plus an LLM
*synthesis* layer, and synthesis is the one thing this design must not have — it would
answer the question *for* the SME, bypassing the persona's own reasoning and returning
prose rather than a citable source. The entire citation-provenance model assumes a
persona read a **source**, not that a tool handed it a conclusion. It also adds a second
container on top of SearXNG for negative value here.

#### What is actually missing

Discovery is the only new step in the pipeline; everything after it is built:

```
litellm.search()  ->  urls  ->  fetch  ->  extract  ->  chunk  ->  add_document(persona)  ->  retrieval
    MISSING            ok      MISSING     documents.py   built        Phase 5                built
```

1. **HTML extraction.** `documents.py` supports `.txt .md .pdf .docx` and **not HTML**
   (verified). Needs `trafilatura`, or readability + BeautifulSoup, behind a **`[web]`
   extra** — the same pattern as `[documents]` and `[vectors]`, so the base install
   stays light per `PROJECT-SPEC.md` §7.
2. **The fetch itself.** Today the only outbound calls are LLM and embedding providers.
   Fetching arbitrary URLs is a new class of egress and needs a timeout, a response
   size cap, and content-type checking before anything is written to the corpus.

**Worth measuring rather than assuming:** `search()` takes `max_tokens_per_page`, so
some providers return page *content* and not merely snippets. Where they do, the separate
fetch step may be unnecessary. That is per-provider and should be measured, not guessed.

#### Shape to build

A `SearchConfig` mirroring `RetrievalConfig`: `enabled: false`, `provider: ""`,
`max_results`, `domain_filter`, and a hard cap on pages ingested per query. **Off by
default, degrading to "no search available"**, so a run without it is byte-identical to
today — the standing convention for every optional capability here
(`cognition`, `retrieval`, `personas` all work this way).

#### Still open

- **Provenance needs a new kind.** A web-sourced claim is none of `firsthand` /
  `secondhand` / `mention` / `unverified`: first-hand to the SME, but the source is not
  something a participant can reliably re-open. It needs the URL **and the fetch
  timestamp**, or web claims silently inherit the credibility of attached documents —
  the exact quiet upgrade the Phase 5i citation work exists to prevent. Note that once
  content is ingested per the decision above, later *retrieval* of it is ordinary
  first-hand document retrieval; the distinct kind is needed for the **origin**, which
  should be recorded on the document rather than on each citation.
- Whether fetched content is subject to the Phase 5 per-turn `max_chars` budget. It
  should be — that budget is the feature, not a safety valve.
- Whether the off-topic similarity guard (`min_similarity`) applies to fetched content,
  which is likely to be noisier than curated documents.
- A search API is a third provider, key and failure mode alongside the LLM and embedding
  providers. That is acceptable as an opt-in extra, but the **single-node,
  no-external-service** constraint (`PROJECT-SPEC.md` §8.2) means it must never appear
  in the quickstart.

**Revisit trigger:** none needed for wanting it. The design above is settled enough to
start from; the first thing to write is the fetch-and-ingest path, because that is what
makes any provider choice reversible.

## Open — features

- ~~**No UI for structured personas.**~~ BUILT 2026-09-06. The Dossier now has a
  **Convictions** section: role, positions with their firmness badge and named exit
  condition, `formed_by`, `optimises_for` / `will not weigh` / `persuaded_by`, and
  formative events with their lessons. Defended positions are visually distinguished
  from negotiable ones, and a defended position with **no** exit condition is flagged
  in amber as unfalsifiable — an authoring gap only the operator can fix.

  The leakage guard is the important part and is **mutation-tested**: a test feeds the
  component a payload that *does* contain `underlying_concern` and `validity` and
  asserts neither is displayed. Deliberately breaking the component to render the
  concern makes that test fail, so it is not vacuous. 22 frontend tests (was 18).

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

- ~~**Push to origin.**~~ RESOLVED 2026-09-06. `origin/master` is current, and
  `v0.4.0` / `v0.5.0` are tagged and pushed. The `PHASE4-REPORT.md` §5 claim that
  pushes fail in this environment is stale and stays stale.
- ~~**Frontend verification.**~~ RESOLVED 2026-09-06. Node 18.20.8 / npm 10.8.2
  installed from `dnf`. `tsc --noEmit` typechecks **29 source files with zero errors**,
  including the Phase 5c and Phase 6 Dossier/types changes that had only ever been
  reviewed by eye. `npm run build` succeeds; **all 18 vitest tests pass** (7 files).
- ~~**Docker build.**~~ RESOLVED 2026-09-06 — verified for the first time in any
  environment. Docker 25.0.14 was *already installed* here; the blocker was that nobody
  had run it, not that it was missing. `docker build` succeeds and produces
  `matrix_sim_studio-0.5.0`. The container serves end to end: `/api/health` 200,
  `/api/runs` 200, `/api/models` 200, and the built UI at `/` with the same JS asset
  hash as the local build. `readiness` correctly reports no provider keys when none are
  passed.

  Also checked, since the image is now something people may actually run: **no
  credentials are baked in.** No `.env` in the image, and the only credential-shaped
  string anywhere in its filesystem is the README's placeholder
  `AWS_BEARER_TOKEN_BEDROCK=your_bearer_token`.

## Housekeeping

- **Four README screenshot placeholders remain `TODO`.** Now actually fixable: the
  Docker image builds and the container serves the UI (verified 2026-09-06), so real
  screenshots can be captured rather than mocked up. This was previously impossible.
- **`PROJECT-SPEC.md` §4a is stale** — it says the priority hierarchy is "a design
  principle, not yet a code gate". It has been a code gate since Phase 4a, and 5i
  added `citation_integrity` to it.

### Config could silently come from the wrong place: FIXED

Both found on 2026-09-08, same family — a value quietly resolved against the
process's working directory instead of the project, with no log line to reveal it.

1. **Relative `DATA_DIR` resolved against cwd.** Starting the server from
   `frontend/` made `./data` mean `frontend/data`; SQLite created a second empty
   database and the UI truthfully reported no previous conversations while 37 runs
   sat untouched in the real file. A cwd mistake was indistinguishable from data
   loss. Fixed: `Settings.resolved_data_dir` / `Settings.db_file` anchor a relative
   path to the checkout root (located by the `pyproject.toml` above the package),
   falling back to cwd only when installed with no checkout. Absolute paths are
   untouched, so the container's `/app/data` is unaffected. `Database.connect()`
   now logs the absolute path plus the existing run count, and logs at WARNING
   when it created the file. Mutation-tested: reverting to `path.resolve()` fails
   the new test.

2. **Test env isolation was order-dependent.** `clean_env` cleared the
   `.env`-derived variables, then the `mock_analysis_llm` fixture below it imported
   `matrix_studio.analysis` -> `litellm` -> `load_dotenv()`, which put them back.
   The whole suite passed because litellm was already imported by collection time,
   so `load_dotenv` never re-ran; `pytest tests/test_settings.py` alone failed.
   Whether a test saw code defaults or the developer's local config depended on
   which other files were selected. Fixed by importing litellm inside `clean_env`
   before the clearing. All 47 test files now pass in isolation, verified
   file-by-file.

### Start over with an existing conversation's setup: BUILT

Added 2026-09-08. The scrubber's branch controls answer "what if something had been
different at turn N?"; nothing answered "what if the premise had been different?".
Editing a persona's convictions, the question itself, who is in the room, or the
background documents all require starting again, and re-typing an eight-persona cast
by hand is enough friction that nobody does it.

`GET /api/runs/{ref}/setup` returns the run's definition **shaped as a create-run
request body** — the same schema the setup-file importer already reads, so a setup
round-trips through either path and the two cannot drift. The scrubber's
"✎ Start over with this setup" loads it into the new-run form for editing; submitting
creates a fresh ROOT run (no parent, no branch turn, nothing from the transcript).

Three things the export has to get right, each mutation-tested:

- **Document text is rebuilt from `doc_chunks`, de-overlapped.** Chunks are stored
  with a carried sentence-aligned tail, so concatenating them repeats every boundary.
  Measured on two real 17-20k documents: naive joining added 4,253 and 4,859
  duplicated characters (~24%), and re-ingesting that would compound per re-run.
  `join_chunks()` in `documents.py` discovers the overlap per boundary rather than
  assuming a setting, and degrades to plain concatenation on foreign chunks so it can
  duplicate but never delete.
- **`document_texts` replaces rather than merges.** The cast row still holds the
  inline documents the run was created with, and those were ingested into the
  documents table at run start; appending would double every one.
- **Config is filtered to the create-run contract.** A branch's config carries
  `mutation`/`imported` describing how it was derived; replaying those into a fresh
  root run would assert a history it does not have.

Known limitation, surfaced as a warning rather than papered over: **cast-wide
documents cannot be carried.** `document_texts` is per-persona only, so a shared
document has nowhere to go in a create-run request. Attaching it to the first persona
would silently change who can retrieve it. Worth fixing properly by allowing
cast-wide `document_texts` at creation.

Also fixed while here: the form assigned the models-endpoint default unconditionally,
which raced the setup load and could silently reset which model a run was billed to.
It now only fills in a default when nothing has chosen one, tested by resolving the
model list *after* the setup.
