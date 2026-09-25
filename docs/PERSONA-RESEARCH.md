# Pre-conversation research: what the room could not find out for itself

Opened 2026-09-23 as a design written before any code, so the reasoning is on the record rather
than reconstructed afterwards.

**Status 2026-09-25: §11 steps 1–7 are done, and §9's pre-registered comparison FAILED its primary
criterion.** Research reached every conversation and the legal question was still left open in all
five research runs, as in all five controls (§9.2). It also did no harm — convergence, concessions
and the brief's share of the prompt all held — so the feature is harmless and not yet useful. §9.2
records a specific design gap as the likely cause: the documented negative is computed per corpus,
and the room's question is per query. Where the build contradicted
this design, the design is corrected in place and says so rather than being quietly edited; §7.1 and
§5.1 are the two places that matter, and §11 records what each step forced.

Verified on the deployed path: single run `602ddffe` (94 sources, 18 controlling, $0.2581) and
ensemble `7c3448ee` (one pass for two members, identical bindings, $0.0935).

**The short version.** Every renewal conversation asked for a citation nobody in the room could
produce, and none of the nine ever got one. This feature answers that request: before turn 1, a
**researcher** builds a shared corpus, and each persona researches **their own stance and its
opposition** into a private one. It reuses the Phase 6 knowledge-base machinery wholesale — the
two scopes already exist — so the new code is a search adapter, an authority tier, and one state
machine state. **Research ingests into the KB already bound at that scope** where there is one, and
creates a collection only where there is not.

Two things make it more than "personas with footnotes", and both come from the discussion that
produced this doc. **Authorities are tiered and controlling ones get a retrieval floor**, so a
settled board ruling cannot be talked past. And **the absence of an authority is recorded as a
finding**, so the conversation can tell "nobody looked" from "we looked and there is nothing" —
which is the single most reusable artefact the feature can produce.

---

## 1. The evidence that the conversations need this

Not a hypothetical. Across five replicate runs of the same brief, the group asked for the same
missing citation and never obtained it:

> **Casey:** "A state statute defining specialty plans as regulated-only is the only thing
> that would change her sign-off; **none was produced**."
>
> **Dr. Jordan:** "Requires an actual complaint pattern or statute before he'll weigh in."
>
> **base3:** "**Someone must produce** an actual state practice act reaching 'continuation of
> treatment' before launching on theory alone."

Six personas, five runs, one unmet request. The `unresolved` tally of the two-cell ensemble has a
group with **count 5** — the same question left open in every run.

A conversation cannot satisfy that from inside itself. That is what this feature is for, and it
is why the researcher is **not** forbidden from searching the central question. An earlier draft
of this design said a researcher searching "is renewal legal" would pre-decide the
conversation. That was wrong: the question is unsettled, which is the entire premise, and if it
turns out to be *settled* then that fact is the most important input the room could receive.

The narrow version of that concern survives, and it is a constraint on output shape only:

> **Return the authorities, not the verdict.** "Yes, it is legal" pre-decides. "Here are two state
> practice acts, one board opinion and a settled case; they disagree as follows" is what is vital.

---

## 2. Two tiers, because two scopes already exist

| | scope | built by | holds |
|---|---|---|---|
| **shared** | `config.knowledge_bases` — every persona may search | the researcher | the subject matter as it stands: authorities, their disagreements, documented negatives |
| **private** | `cast[].knowledge_bases` — this persona alone | that persona | evidence for their stance **and** the evidence they said would change their mind |

Each tier writes into the collection **already bound at that scope**, and only creates one when
nothing is bound — with the ownership and provenance conditions in §5.1, which are what make
writing into a curated collection safe rather than merely convenient.

Phase 6 built both scopes and the query-time grant re-check that governs them. Using that
distinction for its natural purpose is most of why this design is small.

The asymmetry also mirrors a real meeting — shared facts everyone can see, plus what each person
brought — and because the two are separable, a report can ask **which one moved somebody**.

### 2.1 The researcher is not in the cast

It builds a corpus and leaves. A researcher that participates becomes the authority whose
citations outrank everyone's, and these personas already defer to whatever sounds official. Its
output is a knowledge base, not a turn.

### 2.2 Private research must include the opposition

Searching only for support builds a confirmation-bias engine: every persona arrives armed, nobody
can move, and the conversation becomes citation warfare. That defeats a tool whose purpose is
measuring whether positions shift.

The counter is already in the schema. `evidence_that_shifts` holds "1-2 specific things that would
genuinely change their mind", authored per viewpoint and rendered alongside `firmness`. Each
persona's research therefore runs **two** searches per viewpoint: the position, and the stated
condition that would defeat it. **Not optional** — a toggle here would be a toggle for whether the
feature is worth having.

---

## 3. Authorities are tiered, and controlling ones cannot be crowded out

A board ruling on this fact pattern and a law-firm blog speculating about it are not the same
epistemic object, and flattening them is how a persona ends up quoting a blog with the authority of
the brief.

| tier | example | treatment |
|---|---|---|
| **controlling** | statute, board opinion, settled case on this fact pattern | retrieval FLOOR: guaranteed in the prompt while it is relevant |
| **persuasive** | a sister-state statute, a regulator's guidance letter | ordinary retrieval |
| **commentary** | trade press, law-firm blog, forum | ordinary retrieval, and labelled as commentary in the chunk |

**The floor is the mechanism that matters.** The personas' own `evidence_that_shifts` fields name
what would move them — "a statute or board case". If the researcher finds one and it is then left
to compete for 1,200 characters against thirty commentary chunks, the feature has found the answer
and hidden it.

Implemented as `vectors.apply_floors`, and the composition with the existing per-collection floor is
where the difficulty lives. `merge_with_source_floor` reserves per COLLECTION; research ingests into
the collection already bound at a scope, so a statute and thirty commentary chunks sit in the SAME
collection competing for the SAME reserved slot — and the winner is whichever matches a query drawn
from conversation text, which is the commentary. The `2d2ac45b` bug one level down.

So the source floor selects first, and only if its selection holds no controlling authority is one
slot bought. **Only a SURPLUS row may pay** — one whose collection has more than one passage in the
selection. An earlier version dropped the worst-RANKED row, which reads as fair and is not: the
worst-ranked row is usually the RESERVED one, because a collection wins its slot on its own best
passage rather than on global rank. On the measured shape it evicted a persona's only passage to make
room for a statute from the shared collection, trading an invisible failure for a visible one in the
wrong direction. A test caught it.

With no surplus, the statute gets no slot. A collection contributing nothing at all is invisible —
the original bug's symptom was personas saying "I don't have a citation in front of me", which read
as caution — while a missing authority is visible in the corpus as one no turn cited. Given a choice
of failures, take the one somebody notices.

`authority_floor=0` is the default and is exactly the previous behaviour, asserted by test, because a
run with no researched documents must pay nothing for this.

Tiering is a model judgement and will sometimes be wrong. It is therefore **recorded per chunk and
visible in the KB view**, so a wrong tier is arguable rather than invisible — the same discipline as
the ensemble report's `variants`.

---

## 4. A documented negative is a first-class result

The most valuable thing this feature can produce is not a citation. It is this:

> Searched state practice acts in CA, TX, NY and FL as of 2026-09-23 for language reaching
> "continuation of treatment"; **none found**. Queries and sources listed.

Right now the conversation cannot distinguish **"nobody looked"** from **"we looked and there is
nothing"**, and it keeps re-litigating the difference — five runs, five times, no progress. A
documented negative converts an open question into a defensible input to a launch decision.

It must be written as a document in the shared KB, with its queries, sources and date, so it is
citable and falsifiable rather than an assertion. A negative with no method is worthless.

**This is also a prediction.** A documented negative should make runs CONVERGE rather than each
re-arguing the same missing citation — see §9.

---

## 5. Where it runs: a state before the turns

The flow asked for, and it works: the operator toggles research on, submits, and **is not made to
watch**. The run exists immediately, researches, then talks.

```
POST /api/runs  ──▶  run row written (status: pending)
                     ├─ bound KBs are the ingest target; an id is pre-allocated
                     │  ONLY for a scope with nothing bound yet  (§5.1)
                     └─ StartExecution
                              │
                    ┌─────────▼─────────┐
                    │  Research (new)   │  searcher + fetch + ingest_text, per corpus
                    └─────────┬─────────┘
                    ┌─────────▼─────────┐
                    │  Prepare          │  already ingests and embeds the corpus
                    └─────────┬─────────┘
                       Turn ▸ Turn ▸ … ▸ Finalise
```

A new state machine state, not a background task: **Lambda freezes the sandbox when the handler
returns**, so an `asyncio` task started from the API dies. That mistake is on the record three
times in this project. The machine also supplies retry, a timeout and visibility for free.

### 5.1 Ingest into the KB that is already bound, when there is one

**An existing collection is the target.** If the cast-wide binding or a persona's binding already
names a KB, research ingests into it rather than creating a second one. A persona with a curated
collection and a research collection would have two places to look for the same kind of thing, and
`retrieval` would be splitting a budget between them for no reason.

So the ordinary case needs no new identity at all: the binding exists, `ingest_text` takes a
`kb_id`, and the Research state has somewhere to put things before it starts.

A **persona's** target is resolved against **their own** bindings only, never the run-level ones.
`bindings.bound_kbs` returns the union, which is the right answer for a *turn* — a persona may
search the cast-wide collection — and the wrong one here: it would put one persona's private
research, including the case against them, into the collection every other persona reads.

**A new KB is created only when nothing is bound at that scope**, and then its id is
**pre-allocated at create time** — written into `config.knowledge_bases` or
`cast[].knowledge_bases` before anything is searched. A run's bindings live in `config_json`,
written once at creation, so "research, then associate the KB" would mean a read-modify-write of a
JSON blob afterwards; the codebase already refuses that shape once, for `budget`, with a comment
saying why. Same pattern as the ensemble parent row, which lists member ids before any member
exists so an interrupted fan-out knows what is missing.

#### A read grant is not a write grant

**Reuse the bound KB only if the caller owns it.** Phase 6 is explicit that write permission is
ownership ALONE — "a grant says *may read*, and §8b defines no other kind" — and `_owned_kb` is the
chokepoint that enforces it, returning 404 to a grantee so that "you cannot write here" is not
information about who can.

A cast-wide binding may therefore be somebody else's collection, shared read-only. Research must
not write into it: that is a side effect on data another user curated, arriving without their
knowledge from a run they cannot see. When the bound KB is not owned by the caller, research
creates its own and binds that **in addition**, leaving the shared one read-only as intended.

#### Measured 2026-09-24: sharing a collection makes curation compete with research

Reuse has a cost the design did not anticipate, and run `602ddffe` showed it. The source floor
reserves one slot per **collection**, not one per kind of thing in a collection — so once research
writes into a curated collection, the operator's own document competes with the searcher's finds
for that one reservation. On Dr. Jordan's turn:

```
authority_floor=0   commentary (researched) · Source material (CURATED) · persuasive (researched)
authority_floor=1   commentary (researched) · persuasive (researched)  · controlling (researched)
```

His hand-picked source material was displaced entirely, and his collection still "contributed" —
three passages, all of them found by a searcher rather than chosen by him.

This is §3's argument one level down. §3 says a statute and thirty commentary chunks in the same
collection compete for the same slot, which is why controlling authority needs its own floor; the
same reasoning applies to curated versus found material the moment they share a collection. The
difference is that **curation is the thing a human chose**, which makes its silent displacement the
more surprising failure of the two.

**Decided: accept the behaviour and make it visible.** A third floor keyed on `origin` was
rejected — at `k=3`, three floors leave every slot reserved and ranking stops deciding anything, and
an irrelevant reserved passage in every prompt is worse than a missing one because it reads as the
room citing at random. Always giving research its own collection was rejected too: it reverses the
reuse this section exists to describe, and it splits the retrieval budget, since a persona with two
collections spends two of three slots on floors before rank gets a say.

So `origin` now travels with the vector exactly as `authority` does — **verified by live probe, not
reasoned about**: a fresh ingest into a new index returned
`authority=controlling origin=researched` for the found document and both absent for the curated
one. A turn's `document.retrieved` event records `origin` and `authority` per passage plus a
`researched_passages` count, and the dossier renders `3 of 3 researched` with `[found]` / `[yours]`
badges. A run with no research emits none of these fields and renders no badges, so nothing changes
for a conversation that never asked.

**Corpora embedded before this change carry `authority` but not `origin`**, because a vector's
metadata is written once at embed time. Measured against `renewal-jordan`: every row comes back with
its tier and `origin` absent. The consequence is bounded and in the safe direction — the badges are
gated on the per-turn `researched_passages` count, which is itself derived from `origin`, so an old
corpus produces **no badges** rather than labelling found material as the operator's own. The
retrieval floor is unaffected, since it reads `authority`.

No backfill. Re-embedding would mean deleting vectors to make the chunks eligible again, which is
destructive and costs money to redo, and every pass from now on carries the field. There is
deliberately no "has an authority tier, therefore researched" fallback either: the clone script
copies `authority` through to a curated document, so that inference would be wrong by construction.

That converts the failure from *invisible* to *legible*: an operator sees "3 of 3 researched" on a
turn where their own upload should have appeared. It does not prevent the displacement, and the
honest version of that is a per-run "keep research in its own collection" option if it turns out to
matter — a cleaner lever than a floor. Tracked in `docs/BACKLOG.md`.

#### Research documents must be distinguishable from curated ones

Once research writes into a collection somebody assembled by hand, "the document I uploaded" and
"what the searcher found" become indistinguishable — and that is exactly the distinction the
authority tier of §3 depends on.

`_DOCUMENT_FIELDS` has `source_path` and no notion of origin, so this needs two new fields on a
document: **`origin`** (`uploaded` | `researched`) and **`authority`** (§3's tier). They buy three
things that are not optional:

- the KB view can separate curated from found, so a reader knows what they are looking at
- the retrieval floor can privilege a controlling authority (§3) at all
- research can be **undone** without touching curation — which it must be, because a research pass
  the operator disagrees with should not force them to rebuild a collection by hand

#### Re-running research must not accumulate

Run the same definition twice with research on and the collection gets two copies of everything.
So a research pass carries a batch identity, and a later pass over the same scope **replaces its
predecessor** rather than adding to it. Deduplicating by URL alone is not enough: the same page can
legitimately be re-fetched with different content, and the newer fetch is the one that should win.

### 5.2 Research failing must not fail the run

Research is additive. A search outage, a fetch timeout, a provider rate limit — none is a reason to
lose a conversation the operator asked for. The state records what it managed, and
`retrieval.disclose_unsupported` already makes a persona say in-voice when retrieval found nothing.

A run whose research found nothing is therefore an honest run, not a broken one. The status should
say which, because "researched and found nothing" and "research failed" are different facts.

### 5.3 Reviewable afterwards, even though nobody watched

"Do not make me watch" is not "give me no visibility". The KB view already lists a collection's
documents, so the corpus is inspectable after the fact, and the run's setup export shows the
bindings. Nothing about the async flow costs the operator the ability to check what was found.

---

## 6. An ensemble researches ONCE

The caveat. For a single run, research is a state in that run's machine. For an **ensemble** it
cannot be, because live search per member would give members different inputs — and
`ENSEMBLE-CONVERSATIONS.md` §2 rests on the opposite: same config, same brief, so divergence is
evidence about the *brief*. Independent searches would make divergence unattributable.

So the fan-out researches once, before creating members, and every member binds the same KBs and
skips its own Research state. One snapshot, N conversations. Cheaper, and it keeps replicates
being replicates.

### 6.1 Research is a new kind of ensemble axis

The override allowlist is deliberately two keys, and §3.2 justifies that by saying the exposed
settings are **traffic control** — they change who speaks, not what anyone believes. Research
breaks that premise: it changes what is *knowable*.

So research-on versus research-off is **not** a cell override like `method`. It is closer to §5.2's
brief paraphrase: a different epistemic condition, needing its own cell type, its own replicates,
and **never pooled** into one count with non-research runs. Settle that before anyone adds a third
key to `CELL_OVERRIDES` casually.

---

## 7. The UI

A toggle plus config on the new-conversation form, next to the existing KB picker — which is where
bindings already live, so research is a KB-*authoring* step and the form merely binds what it made.

| control | default | why |
|---|---|---|
| **Research before starting** | off | it spends money and time on every run; opt in |
| Shared research (the researcher) | on, with research | the corpus everyone sees |
| Per-persona research | on, with research | and its opposition half is not separately switchable — §2.2 |
| Sources per query | 5 | how wide each search goes |
| Queries per viewpoint | 2, **fixed** | one for the position, one for `evidence_that_shifts` |
| Recency limit | none | a settled case from 2011 is not stale; a blog from 2011 is. Tiering handles it better than a date filter |

The button should name the multiplier, as the ensemble one does: research is 1 + N corpora, and an
operator should see "researching 7 collections" before agreeing to it.

### 7.1 Built, step 5 — and one control in the table above is wrong

**"Queries per viewpoint" is displayed, not editable**, and the table as first written contradicted
§2.2. The two queries are *one for the position and one for the evidence the persona said would
change their mind*; exposing the count as a dial invites setting it to 1, which silently drops the
opposition query — the single thing §2.2 says must not be separately switchable. A persona who only
ever sees support for what they already think cannot be moved by evidence, and measuring whether
they would be is the entire point of the tool. So the form explains the 2 and offers **sources per
query** instead, which is a real dial: it decides how wide each search goes and nothing about whose
case gets made.

Three further things the build settled:

- **Research forces `retrieval.enabled` on.** `retrieve_for_turn` is never called with retrieval
  disabled, so an operator who enabled research and attached nothing else would have paid for a
  full pass and then held a conversation that queried none of it. A complete, invisible corpus,
  with no symptom but a conversation that seemed no better informed. The form also sends
  `authority_floor: 1` with it — a run that went looking for statutes should not then let them
  lose every slot.
- **The ensemble warning is in the form, not only in the 422.** §6 means the route refuses
  research on an ensemble until step 6; saying so at the toggle means an operator does not fill in
  a whole form to discover it.
- **§5.3 needed a panel.** "Do not make me watch" left no way to see what a pass did, so
  `GET /api/runs/{ref}` now returns the record and `ResearchPanel` renders it. It keeps
  `found-nothing`, `unavailable` and `failed` visually distinct, because those are three different
  facts and only one is worth retrying — and it says **"not retrievable"** rather than a count of
  zero when documents were stored and could not be embedded, since every other symptom of that
  looks like a researcher who found nothing useful.

---

## 8. What it costs

Search APIs are ~$1–10 per thousand queries and embeddings are cheap, so with 6 personas at 2
queries per viewpoint the research pass is **cents** — negligible against a $0.95 conversation, and
paid once per ensemble rather than per member.

The real cost is **indirect and worth watching**: more retrieved text per turn means larger prompts
on every turn of every run. `max_chars` is 1200 by default and 3000 in the renewal definition.
Three corpora now compete for it — brief, shared, private — and the brief is the one that must not
be squeezed out. Retrieval tuning is part of this feature.

---

## 9. Pre-registered criterion

Recorded before any code, so it cannot be fitted afterwards. "Personas cite more sources" is
trivially achievable and is **not** the goal; if research makes every position harder and
convergence rarer, this is a worse tool and should be reverted.

Measured as a research cell against a no-research cell on the same brief, **5 replicates each**,
`stop_when_converged` on, everything else identical. Research is worth keeping if:

1. **The unmet request is met or documented.** The "produce a statute or board case" demand that
   recurred in 5 of 5 runs either gets an authority or a documented negative. This is the primary
   criterion — it is the thing the conversations asked for.
2. **Convergence does not get worse.** Median convergence turn no later than the no-research cell.
   A documented negative should *help* here, by removing a question the room re-litigates.
3. **Concessions do not fall.** If private research only entrenches, concession counts drop and the
   opposition half of §2.2 is not doing its job — that is a prompt failure to fix, not a reason to
   keep shipping it.
4. **The brief is not crowded out.** Retrieved chunks from the original brief per turn do not fall
   materially against the no-research cell. This matters MORE now that research ingests into the
   bound collection: found documents and curated ones compete inside the same KB, so the `origin`
   field of §5.1 is what makes this measurable at all.

Failing (1) means the feature does not do its job. Failing (2) or (3) with (1) passing is a tuning
problem. Failing (4) is a retrieval-floor bug.

### 9.1 Operationalised 2026-09-25, before either arm was launched

The criteria above say WHAT counts. They do not say how to count it, and choosing that after seeing
output is the error the dismissal retune records. So this section is committed before any run
exists, and the analysis must use it as written.

**The arms.** Two ensembles, one cell each, n=5, rather than one ensemble with a research axis —
§6.1's reason: a cell varying research would need two corpora and "the parent researched once"
would stop being true.

- **control** — `examples/renewalBrief/run.section9-control.json`
- **research** — `examples/renewalBrief/run.section9-research.json`

Built from one config and **asserted byte-identical** once the research block and the collection ids
are removed. `stop_when_converged` and `authority_floor: 1` are on in BOTH (the floor is a no-op on
the control, which has nothing tiered). Collections are **asserted disjoint**: the research arm binds
its own clone of the same seven curated documents (passage counts identical, 32/23/18/18/15/19/14),
because binding the control's would have let research write into the control's corpus.

**Censoring.** A run that hits the 40-turn ceiling without converging is scored at 40 and counted
separately as "did not converge". It is not dropped — dropping it would make the cell that converges
less often look faster.

**n=5 cannot resolve small differences.** `ENSEMBLE-CONVERSATIONS.md` §3.3, and I have walked into
it once in this project. So criteria 2–4 fail only on a difference larger than the CONTROL cell's own
spread, defined below per criterion. A smaller difference is reported as "no detectable difference",
never as a pass or a fail.

| # | measured as | passes if |
|---|---|---|
| **1a** | per research run: did ANY turn's `document.retrieved` carry a passage with `authority == "controlling"`, or one titled `No controlling authority found…` (the documented negative) | ≥ 4 of 5 research runs |
| **1b** | per run: does the extraction's `unresolved` list contain a question matching the rule below | research count < control count |
| **2** | median of `turn_count` at stop | research median ≤ control median + ½ × control range |
| **3** | median of total `concessions` per run, summed over participants | research median ≥ control median − ½ × control range |
| **4** | mean per turn of retrieved passages whose `origin` is not `researched` | research ≥ 50% of control |

**1b's matching rule**, fixed now because it is the one place a reader's judgement could drift: an
`unresolved` item matches if, lowercased, it contains any of `statute`, `practice act`,
`board opinion`, `board case`, `board ruling`, `regulation`, `legal authority`, `case law`,
`controlling`. Items are also read by hand and the reading reported, but the rule decides.

**1 is the primary criterion** and needs both halves: 1a is whether research REACHED the room, 1b is
whether the room's question got answered. 1a passing with 1b failing means the authority was in the
prompt and did not settle anything, which is a different finding from research not finding one.

#### Amendment 1 — 2026-09-25, both arms running, NEITHER with a report

Two corrections to the measurement above, both forced by testing `scripts/analyse_section9.py`
against PRIOR ensembles before any §9 output existed. Recorded rather than silently edited, because
an amendment to a pre-registration is only legitimate if it is visible and dated.

**Where open questions live.** "The extraction's `unresolved` list" per run is not persisted — only
the clustered tally in `agreements.unresolved_by_frequency`, one entry per cluster, holding a
representative RAW text (the first run's own wording, not a generated label) and every run
clustered with it. So the rule is applied to each representative and credited to every run in its
cluster. The arms are clustered in separate reports; matching on raw representatives rather than on
labels is what keeps that from being two different labelling passes compared.

**The matching rule as first written could not see the question.** Calibrated on `renewal-cells`
and `renewal-ens`: the most-repeated open question in the project — *"Whether CA's 'treatment of
whatever nature' language has been interpreted to reach plan renewal"*, left open in **5
runs** — matched **none** of the original terms. Nor did `practice-act` (hyphenated), *"board
enforcement history"*, *"legally sufficient"* or *"statutory breadth"*. A rule that cannot see the
question cannot tell whether it was answered, so 1b would have scored both arms near zero and could
never have failed.

The amended rule is word-bounded regexes (so `board` cannot match "onboarding"): `statute(s)`,
`statutory`, `practice act` / `practice-act`, `board(s)`, `regulation(s)`, `regulatory`,
`legal(ly)`, `case law`, `controlling`, `interpret(ed|ation|s)`, `enforcement`,
`practicing licensed medicine`, `regulated product`, `legend`, `law`, `citation(s)`.

On the two prior ensembles it matches 10 of 31 and 12 of 22 open questions. Known borderline
calls, stated so a reader can disagree with them rather than discover them:

- **missed**: *"Whether other states have narrower drug-specific language…"* — legal, but adding
  `language` would also match "Final SLA language".
- **excluded on purpose**: *"records review satisfies the knowledge standard"* and *"who owns
  securing outside counsel"* — the first is a clinical-standard question, the second procedural.
- **over-matched**: *"Whether/when Riley will sign off pending the legal/compliance response"* —
  about WAITING on the legal answer rather than the legal question itself.

**Criterion 4 is expected to be at risk.** On run `602ddffe` a persona's curated source material lost
every slot to research that shared its collection (§5.1), and that behaviour was accepted and made
visible rather than fixed. If 4 fails, that decision is what it indicts.


### 9.2 Result — 2026-09-25. **Criterion 1 FAILS as registered.**

Control ensemble `8b71fedb`, research ensemble `cfce13ed`, scored by `scripts/analyse_section9.py`
exactly as §9.1 and Amendment 1 specify. $9.50 in total: $7.10 conversations, $2.09 reports, $0.31
research.

| # | control | research | verdict |
|---|---|---|---|
| **1a** research reached the room | — | **5 of 5** runs saw a controlling authority | **PASS** |
| **1b** the question got answered | 5 of 5 left it open | **5 of 5** left it open | **FAIL** |
| **2** convergence | median 28 (range 16), 4/5 converged | median 33, 5/5 converged | no detectable difference |
| **3** concessions | median 6 (range 4) | median 6 | PASS |
| **4** brief not crowded out | 3.00 curated/turn | 2.41 = **80%** | PASS |

**Per §9, failing (1) means the feature does not do its job.** 1 needs both halves and 1b fails: the
authorities reached every research conversation and the legal question was still left open in every
one. That is the registered verdict and it is not softened by anything below.

What did NOT happen is also worth stating, because §9 names it as the reason to revert: research did
not make positions harder or convergence rarer. Concessions held, the brief kept 80% of its share of
the prompt, and every research run converged against four of five controls. The feature is, on this
evidence, **harmless and not yet useful**.

#### Exploratory — not part of the verdict

Two observations from reading the output, recorded as hypotheses for a NEW pre-registered run rather
than as a rescue of this one.

**The open question changed shape.** Control runs leave open whether any authority *exists*:
*"Whether any actual board action/case/complaint exists"*, *"Actual text of CA §4826(b)"*,
*"Whether other states have similar statutory carve-out, unsurveyed"*. Research runs leave open what
an authority they *have* means: *"Whether CA section 417 text matches Riley's paraphrase or the
636(f)/(g) statute"*, *"Whether practice act's 'professional opinion rendered' language reaches this
fact pattern"*, *"Legal disagreement: is plan renewal a continuation or a new treatment
plan"*. That reads like moving from "nobody has the text" to "we have the text and disagree about
it" — which may be the honest ceiling of what research can do for a question the law has not
settled. The binary 1b rule cannot see that difference, and it was not designed to.

**The documented negative never fired — not once in 159 retrieval turns — and this is a design
gap, not bad luck.** Every scope found at least one controlling authority (a practice act, a board
policy), and `documented_negative` writes only when a corpus finds NONE. But the room's recurring
question is narrower: *"Whether any board case/enforcement action exists on plan renewal"*
was still open in research runs 3 and 5. Research found the statutes and no enforcement action on
this fact pattern, and nothing in the corpus ever said *"we searched for one and there is none"*.

§4 promised exactly that artefact. It is computed at the wrong grain — per CORPUS, when the question
is per QUERY. A persona's `evidence_that_shifts` query ("a board case on this fact pattern") that
returns no controlling authority should produce its own negative, even when the same corpus found a
statute answering a different query. That is a specific, testable change, and the obvious candidate
for a second run.


### 9.3 Run 2 — pre-registered 2026-09-25, BEFORE the per-query negative is written

§9.2's lead hypothesis: the documented negative never fired (0 of 159 turns) because it is computed
per CORPUS — written only when a whole corpus finds no controlling authority — while the room's
recurring question is per QUERY: *has any board acted on plan renewal?* Every scope found
some statute, so nothing ever said "we searched for an enforcement action and there is none".

**The change under test.** When a corpus found SOME controlling authority, each query that found
none gets its own short negative document, titled `No controlling authority found — {the query}`,
so a turn asking the question in those words can retrieve it. A corpus that found no controlling
authority at all keeps the existing corpus-level negative, unchanged. Nothing else changes.

**Both arms run fresh.** Run 1's control output was read in full in §9.2, so reusing it would score a
rule designed after seeing it. Same two definitions as run 1; the research arm binds a NEW clone of
the seven curated documents, because run 1's research-arm clones now hold run 1's batch.

| # | measured as | verdict |
|---|---|---|
| **R1** mechanism | a per-query negative is produced, AND some turn's `document.retrieved` carries a passage titled `No controlling authority found…` | ≥ 3 of 5 research runs, or the run is **VOID** — the fix was inert, and nothing is concluded about whether it works |
| **R2** primary | runs with ≥ 1 open EXISTENCE question (rule below) | **PASS** if research ≤ control − 2 · **no detectable difference** if within 1 · **FAIL** if research > control |
| **2 · 3 · 4** | exactly as §9.1 | guardrails, same thresholds |
| *secondary* | the original 1b | reported, not judged — §9.2 already showed it cannot separate "does it exist" from "what does it mean" |

**The existence rule.** An open question counts when it matches the amended 1b rule AND, word-bounded,
contains one of `any`, `exist(s)`, `existence`, `unsurveyed`, `other states`, `sourced`. Calibrated on
`renewal-cells` and `renewal-ens` ONLY — not on §9 run 1, whose texts I have read. There it separates
*"Whether any state defines a specialty plan as a regulated product"* (existence) from *"Whether
the PCR positions would survive a board test"* (interpretation). Known borderline:
*"…has been interpreted to reach plan renewal"* counts, via "other states" in its wording;
*"Whether a specialty plan carries a federal legend trigger"* does not.

**A difference of 2 runs, because n = 5.** One run's difference is within what §3.3 says replicates
of this brief produce on their own.

**What each outcome means, decided now.** R1 void: fix the mechanism and re-run; nothing learned. R1
passes and R2 fails: the negative reached the room and did not settle the existence question, so
§4's claim that it is "the single most reusable artefact the feature can produce" is unsupported for
this brief, and research is recorded as not useful here. R2 passes with guardrails intact: the
feature does its job on the question it can answer, and stays.

**Criterion 4 is at more risk than in run 1.** Up to one negative per query now competes for prompt
slots alongside everything else.

### 9.4 Run 2 result — **VOID**. The negatives were produced and never retrieved.

Control `1c135dc0`, research `962ba446`. $9.92: $7.79 conversations, $1.86 reports, $0.27 research.

    R1  mechanism   9 per-query negative(s) produced; reached a prompt in 0 of 5 research runs
                    (needs >= 3)  VOID — the fix was inert; nothing is concluded

As pre-registered, R2 and the guardrails are **not scored**, and nothing is concluded about whether
per-query negatives help. The script stops at the gate by design.

**The zero was checked rather than trusted**, because a detection bug had already produced a false
number once in this comparison. Matched by document id against the KB rows — not by title — the
research arm retrieved a negative **15 times, in all 5 runs, and every one was Quinn's
corpus-level negative** (her corpus found no controlling authority at all). Of the 9 per-query
negatives, none was retrieved once in 474 passages. The detection was right.

**Why they lose — measured, not reasoned.** Casey's corpus holds a negative for exactly the authority
she keeps demanding: *`state statute "specialty plan" defined as regulated product`*. Replaying
her 23 recorded retrieval queries against her own collection, that negative ranked **#5 at best**,
usually below #50, and beyond #100 on 8 turns. Queried with her own stated demand instead —
*"A state statute defining specialty plans as regulated-only would change my sign-off"* — it
ranks **#3**.

So the negative is relevant to the question, and the turns never ask the question. A turn's retrieval
query is a term bag from the last few messages; it is conversation-shaped, and the negative is written
in the vocabulary of the SEARCH that produced it. It is §3's statute-versus-commentary problem again:
material whose wording mirrors the conversation wins by construction, and being right about relevance
is exactly how it hides.

Also observed and not changed mid-run: each negative splits into **2 chunks**, not the 1 its length
cap aimed at, because the chunker's window is smaller than 1,500 characters.

**The mechanism has to change, and the choice changes what a persona sees.** Three options, recorded
for a decision rather than taken:

- **A standing query per persona.** Each turn, retrieve once more with a query built from the
  persona's own `evidence_that_shifts` — their declared "what would change my mind" — and merge.
  Replayed above, that query puts the negative at #3. Stays inside retrieval and its budget.
- **A reserved slot for negatives**, like the authority floor. Rejected in advance on the floor's own
  argument: at typical ranks of #50+ it would force a low-relevance passage into most prompts, and an
  irrelevant reservation reads as the room citing at random.
- **Render the persona's own negatives directly** in their prompt, outside retrieval — the way
  convictions are rendered. Deterministic, a few short lines per persona, and guaranteed to be seen;
  but it stops being retrieval, and it puts "nothing was found for your condition" in front of a
  persona on every turn, which may entrench rather than settle.
---

## 10. Risks

**Fetching arbitrary URLs from a Lambda.** Egress, provider terms of service, and SSRF if a URL
ever originates from user input rather than from a search provider. A provider that returns
extracted text (Tavily, Exa) avoids writing a fetcher at all and is the safer default.

**The researcher frames everything.** Whatever it treats as background shapes every persona's
reasoning invisibly. Mitigated by §3's visible tiering and by §4's requirement that a negative
carry its method — not eliminated.

**Tiering will be wrong sometimes.** A model decides what is controlling. Recorded per chunk and
shown in the KB view so it is arguable.

**Provider non-determinism.** Search results change between runs. §6's snapshot-once is what keeps
ensembles interpretable; a single run's research is not reproducible and the corpus should carry
its search date so a later reader knows what it was.

**Writing into a collection somebody curated.** Reuse is what the operator asked for and it is
right, but it means a research pass mutates hand-assembled data. Ownership is checked (§5.1), origin
is recorded so curation and research stay separable, and a re-run replaces rather than accumulates
— three guardrails for one convenience, and all three are load-bearing.

**No provider existed in the repo.** Decided 2026-09-23 — see §12.

---

## 11. Build order

1. **The corpus builder, offline.** A script that takes a definition and produces the KBs, so the
   researcher's prompt and the tiering can be judged by reading the output before any of it is
   wired to a run. Cheap to iterate; this is where the quality is won or lost.
2. **`origin` and `authority` on a document**, plus the replace-a-previous-batch behaviour. Before
   the floor, because the floor reads `authority`, and before anything writes into a curated
   collection, because undoing a research pass depends on `origin`.
3. **The authority tier and the retrieval floor**, measured against an existing renewal run — does
   a controlling authority actually survive into the prompt?
   **DONE, and the metadata path is verified rather than assumed.** A probe against the live index
   put a vector carrying `authority` — a key the index was not created with — and a k-NN query
   returned it: `['authority', 'document_id', 'ordinal', 'owner_sub', 'text']`. No index recreation
   needed, and there was already precedent in the codebase: `title` was added the same way, with a
   comment explaining that only the filterable/non-filterable split is immutable.

   Verified first on purpose. Three features in this project have shipped inert because a value
   never arrived where it was read — cognition v0.2, the decline streak, the closing round — and a
   floor reading an absent field fails SILENTLY while the corpus displays an authority no turn ever
   saw.

   `authority` now travels from the document row through `kb_chunks_missing_vectors` into vector
   metadata and back out of the k-NN query. Carried with the vector rather than looked up at query
   time for the same reason `title` is: a KB document row lives under the KB OWNER's partition and a
   grantee physically cannot read it, so a lookup would work for the owner and silently return
   nothing for everyone a collection is shared with.

   `vectors.apply_floors` composes the two floors, and the composition is the whole difficulty.
4. **The Research state** in the machine, reusing a bound KB where there is one, pre-allocating an
   id where there is not, and failing additively (§5.1, §5.2). **DONE.**

   `research_state.py` holds the three decisions the searcher must not make for itself:
   `allocate_targets` (creation time, in the API), `run_research` / `research_definition` (state
   time), and the record. The state machine gains a `Research` state ahead of `Prepare`, and
   **its catch points at `Prepare`, not at `MarkFailed`** — §5.2 as one line of wiring, asserted
   by a template test, because a Brave rate limit must not kill a run that would have held itself
   perfectly well with no research at all.

   Four things turned out to be load-bearing and none was in the design:

   - **The corpus has to be EMBEDDED, and nothing else would have done it.** `add_kb_document`
     chunks and stores; a chunk with no vector is invisible to a k-NN query; and the only other
     caller of `embed_pending_kb_chunks` is the upload ROUTE. So a research pass would have
     stored twenty documents that no turn could ever retrieve — and the symptom would have read
     as "the researcher found nothing useful", a judgement about quality, rather than as a
     missing step. The fourth instance of this project's signature failure, caught before it
     shipped rather than after.
   - **The vector index must be created by the API, not by the state.** The tenant role holds
     `s3vectors:GetIndex` and deliberately not `CreateIndex`, so the worker running the Research
     state physically cannot create one. `allocate_targets` takes the unscoped store for exactly
     this, and does it for a REUSED target too — a KB created before indexes were made eagerly
     would otherwise be permanently unwritable.
   - **Ownership is checked twice.** `allocate_targets` resolves targets at creation, but they
     live in `config_json`, which is built from a request body — so between the two there is a
     blob the caller controls. Without the second check, naming another user's KB in
     `config.research.targets` would write research into their collection.
   - **The status cannot be derived from rows written.** Every pass writes at least one row,
     because the documented negative is itself a document. Counting writes would report
     `researched` for a pass that found no source at all, and the one distinction §5.2 asks for
     — "we looked and there is nothing" versus "research failed" — would be the one the status
     could not make. It is counted from sources found.

   Also found and fixed: **the authority floor from step 3 was inert.** `vector_search_kbs`
   accepted `authority_floor` and no caller passed it, and `retrieve_for_turn` applied the source
   floor at two later trims that would each have discarded the reservation. There are **three**
   trims between a k-NN query and a prompt, and a floor missing from any one is undone by the
   next. All three now call `apply_floors`, `RetrievalConfig.authority_floor` reaches the engine,
   and `tests/test_kb_retrieval_switch.py` asserts end to end that the statute reaches the prompt
   *and* that the persona's only passage is not what pays for it.

   The search keys are in **Secrets Manager**, fetched at cold start by
   `websearch.load_keys_from_secret` and never a Lambda environment variable — those are readable
   with `lambda:GetFunctionConfiguration` and appear in `cdk diff` and CloudFormation events.
   `search_secret_arn` names a secret the operator created; the stack creates none, because a
   CDK-generated placeholder would look like a configured key while every search failed
   authentication. Unset means research reports itself `unavailable` and the conversation runs.
5. **The UI toggle** (§7). **DONE** — see §7.1, which also corrects §7's own table. The toggle,
   both tiers, sources per query, the collection count on the button, and a `ResearchPanel` on the
   run view so a pass nobody watched can still be reviewed (§5.3).

   **First live end-to-end run: `602ddffe`, and the chain holds.** Tavily, 94 sources, $0.2581,
   313s against the 900s ceiling. All seven scopes ingested AND embedded — 3,951 passages, 18
   controlling authorities, and the two personas who found none got a documented negative.
   Researched passages reached all 8 turns.

   Two things the doc had listed as open are closed by it. **Tavily retrieved the ASSOC Model
   Licensed Practice Act**, which §12.5 said would have to be curated by hand because ASSOC
   blocks both the fetcher and Tavily's extractor — it reached turns 1–5. And Quinn's turn
   retrieved *"No controlling authority found — Quinn"*, so §4's negative is readable by the
   conversation rather than merely filed.

   The floor was then A/B'd on those live vectors, because the run alone could not distinguish it
   from the source floor: on Casey it was correctly a **no-op** (her selection already held a
   controlling passage), and on Jordan and Riley it **promoted** one that global rank had placed
   6th. At k=6 the controlling row arrives naturally, so at k=3 the floor is doing real work.

   **One open question from that run.** On Jordan the promoted slot was paid for by his own
   run-scoped passage rather than by one of three surplus `persuasive` chunks from the same
   collection. `apply_floors` should let only a *surplus* row pay, and a lone `kb_id: None` row is
   not surplus — so either the eviction happened at the earlier merge trim, where the pool is
   larger, or the implementation does not match its docstring. Live probes cannot show the
   intermediate selections; this needs a unit test reproducing that exact shape (one run-scoped
   row, three same-collection persuasive rows, one controlling at rank 6). It is the same class as
   the bug already fixed once here, so it is worth pinning rather than leaving.
6. **The ensemble path** — research once, bind to all members (§6). **DONE.**

   The flow: `POST /api/ensembles` allocates the targets against the **base config** (fast — it
   creates collections and searches nothing), re-plans the members so every one inherits the
   resolved bindings, writes the parent with every member id and `status: "researching"`, and
   dispatches. The research worker researches once, records on the parent, and **then** creates
   the members. The route returns immediately, because a pass is minutes against API Gateway's
   29-second ceiling.

   Parent-then-members is the same property the fan-out already had: an interrupted pass leaves a
   parent that knows what is missing, rather than orphan runs with nothing to aggregate them.

   Four things this forced, none of them in the design:

   - **The parent had to become self-sufficient.** Research and the fan-out both run in a worker
     holding only an ensemble id, so the row now carries `cast_json` — without it there is no
     per-persona corpus to build and no member to create — and `groups_json`, because members are
     created minutes later where no JWT exists and a KB granted to a group would otherwise be
     unsearchable for the entire fan-out.
   - **`ensemble_spec.plan` has to run twice.** The members were planned before `knowledge_bases`
     and `research.targets` existed on the base config, so reusing that plan would have given
     every member a config with no bindings: research would run and no turn would query it.
   - **A dependency cycle in the infrastructure.** Granting the worker `states:StartExecution`
     with `grant_start_execution` makes it depend on the state machine, which already depends on
     it (Research is its first state). CloudFormation refuses: *"Template is undeployable, these
     resources have a dependency cycle."* The ARN is composed from the same explicit
     `state_machine_name` instead, and a test pins that name so it cannot silently become
     generated.
   - **The local path bypassed the member guard**, and a test caught it. `run_research` refuses on
     `ensemble_id`, but the local path calls `research_definition` directly — so a two-member
     fan-out researched **three times**. The same guard now exists in both places, because one of
     them is not a guard.

   The fan-out happens **whatever the research did** (§5.2). An ensemble stuck at `researching`
   with no members is the worst available failure: the parent lists run ids that do not exist and
   nothing is working on them.

   **Verified on the deployed path**, ensemble `7c3448ee`: one pass (28 sources, 13 controlling,
   $0.0935, 174 s), `2 started, 0 failed`, both members bound identical collections and recorded
   their own research as `skipped`, and every retrieved passage carried `origin` and `authority`
   with `researched_passages=3 of 3`. The intermediate state was observed as designed — parent
   `researching`, two member ids listed, zero runs existing.
7. **The §9 comparison**, 5 replicates each way.

Step 1 is deliberately separable and deliberately first: if the researcher's output does not look
useful when a human reads it, none of the wiring is worth building.

---

## 12. The provider: Brave Search, decided 2026-09-23

Chosen over the AWS-native options after checking what they actually offer in this account.

**AgentCore has no web-search primitive.** `bedrock-agentcore-control` offers
`create-browser`, `create-code-interpreter`, `create-gateway`, `create-memory` — a managed headless
browser, not a search engine. Using it would mean writing the search-and-read loop ourselves:
driving a browser at a search engine, scraping its results page, navigating, extracting. That is
page-structure brittleness, a session lifecycle per query, and the terms-of-service question of
scraping a search engine, in exchange for staying inside one vendor. It is built for agents that
*operate* a web app, not for bulk retrieval. `list-browsers` is empty; nothing is provisioned.

**Bedrock Knowledge Bases' `WEB` data source is a closer fit and still not enough.**
`create-data-source` accepts `S3 | WEB | CONFLUENCE | SALESFORCE | SHAREPOINT`, and
`webConfiguration` crawls URLs into a KB with chunking and embedding managed. That matches this
design's *output* well — the artefact we want is a knowledge base. But **it crawls seeds; it does
not search.** It cannot answer "what do state practice acts say about continuation of treatment",
so it solves the ingest half and leaves discovery open. Its help text also says "Crawling web URLs
as your data source is in preview release", which is not where a feature's ingest path belongs
without deciding that deliberately.

**A model naming URLs is not an option for this.** The discovery step could be a model call, using
only AWS. But a model naming sources from training data produces URLs that do not exist or do not
say what it claims — and for legal authorities specifically that is the worst available failure
mode. It is precisely what §4's documented-negative requirement exists to prevent.

**"We are already on AWS" cuts the other way.** That is an argument for keeping *state* on AWS, and
this adds none: the search call is stateless, its output lands in a KB we already own, and swapping
provider later touches one adapter. Avoiding one API key by building a crawler or trusting a
model's URLs costs more than it saves.

### 12.1 Verified

`GET api.search.brave.com/res/v1/web/search` with `X-Subscription-Token`, HTTP 200. The first result
for *"california licensed practice act specialty plan"* was
`vmb.ca.gov/applicants/practice_act.shtml` — the California Licensed Medicine Practice Act, whose
snippet names "Article 4. Requirements for Regulated items".

That is a **controlling authority for the exact question five replicate runs could not answer**.
Recorded because it is the strongest evidence so far that §9's primary criterion is reachable.

A second live query, through the provider abstraction rather than curl, returned three more primary
sources: the ASSOC Model Licensed Practice Act, and the Arizona and Pennsylvania practice acts. The
Arizona snippet contains the operative language verbatim — *"the provider has arranged for either
of the following: … (ii) Continu[ation]"* — which is the continuation-of-treatment question the
conversations kept asking about. Search is finding the right KIND of thing, not merely something.

### 12.2 Brave returns snippets, so we DO own a fetcher

Correcting §10 as it was first written. Tavily and Exa return extracted page text, which is why they
were described as avoiding a fetcher; **Brave returns titles, URLs and short descriptions**.
Measured across two live queries, those descriptions run **roughly 100–500 characters** — the first
query's were about 100, a later one's were 214–495. Enough to rank a result and to see that a page is
promising; nowhere near enough to cite a statute.

So choosing Brave means writing the fetch-and-extract step, and re-accepting the surface §10 said it
avoided: egress from a Lambda, per-site terms of service, and SSRF if a URL ever reaches the fetcher
from anywhere but a search response. Those are now requirements rather than avoided risks:

- fetch only URLs that came from a search response, never from user input
- an allowlist of schemes (`https` only) and a denylist of private address ranges, checked after DNS
  resolution rather than on the hostname
- a byte ceiling and a timeout per page, because a statute site can serve a 40 MB PDF
- extraction that fails to text rather than raising, so one unreadable page does not lose a corpus

**PDFs are not an edge case here, they are the primary sources.** A live query for "state licensed
practice act continuation of treatment" returned the ASSOC Model Licensed Practice Act as a PDF in
the top results, alongside Arizona and Pennsylvania statutes as HTML. So the fetcher must handle
PDF, and it does not need a new extractor: `documents.py` already extracts PDFs for the knowledge-base
upload flow, and `/api/documents/formats` already reports what this deployment can read. The fetcher
hands bytes to the extractor that exists.

This is a real cost of the choice and worth carrying openly rather than discovering during
implementation. It is not a reason to reverse the decision — the authority ranking Brave gave in
§12.1 is worth a fetcher — but step 1 of §11 is now larger than it looked.

### 12.4 Measured while building the fetcher

Three things came out of fetching real pages, and each changed the code:

**A bot block wears a success code.** The ASSOC model practice act PDF — a primary source this feature
exists to find — returns HTTP 200 with 954 bytes of `text/html` reading "Request unsuccessful.
Incapsula incident ID: …". It extracted to 83 characters and passed an emptiness check, so it would
have entered a corpus as a found source: retrieval could rank it, a persona could cite it, and the run
would have counted a citation it never had. Hence `MIN_TEXT_CHARS = 200`, with the rejected text
logged, because that text is how the next kind of block gets recognised.

**Some primary sources refuse us outright.** `vetboard.az.gov/statutes-and-rules` answers 403 to a
non-browser user agent. That is the site's right and the fetcher identifies itself rather than
impersonating a browser, so this is a permanent limitation rather than a bug: **a state licensed
board's own statute page can be unreachable.** It argues for §4's documented negative recording *what
could not be read*, not only what was not found.

**What does work, works well.** `azleg.gov/ars/32/02201.htm` — Arizona Revised Statutes §32-2201 —
extracts to 6,795 characters containing the PCR definition verbatim: "the provider has
sufficient knowledge of the member to initiate at least a general or preliminary diagnosis" and "has
arranged for either continuation of treatment or emergency coverage". That is the operative language
for Riley's "is a technician form enough" objection and Casey's classification question, retrieved
by one query. Three of four results fetched cleanly at 6.8–10.8k characters each.

### 12.5 The sources this feature most needs are the ones that block fetching

Measured on the first full pipeline run over the shared corpus: **8 of 12 fetches failed, and they
were the authoritative ones** — `fda.gov` three times, `assoc.org` three times, plus `cacvt.org` and
`vetcation.com`. Diagnosed individually, and **none was a bug**. Three distinct mechanisms:

| site | what it does | handled by |
|---|---|---|
| `assoc.org` | HTTP 200 with 961 bytes of Incapsula block page | `MIN_TEXT_CHARS` |
| `cacvt.org`, `vetboard.az.gov` | plain HTTP 403 | status check |
| `fda.gov` | 302 to `/apology_objects/abuse-detection-apology.html`, then 404 | redirect following |

**This is the most important finding about the feature so far, and it is uncomfortable.** The corpus
that came back was 1 controlling and 3 commentary — not because commentary is what exists, but
because the FDA, the ASSOC and a state technician association all refuse automated readers while
vendor sites, association summaries and policy trackers do not. The authority distribution is partly
an artifact of the web's defences.

Three consequences:

**The tier distribution must not be read as a survey of what exists.** A corpus carrying "1
controlling, 3 commentary" invites exactly that reading. The unreadable list has to travel with it.

**The documented negative becomes MORE valuable, not less.** "We could not read fda.gov, assoc.org or
cacvt.org" tells a human precisely where to look by hand. That is a better artefact than a silent
corpus of whatever happened to be scrapeable.

**It is a concrete argument for Tavily or Exa** that §12 did not have. Their value was framed as
avoiding a fetcher; the stronger reason is that they operate fetching infrastructure at a scale that
negotiates with these defences, and we do not and should not. Choosing Brave means accepting a corpus
systematically biased away from primary sources. That is worth re-examining before step 3 — with one
of those keys set, the same run is a direct comparison, and `websearch.select` already prefers them.

The fetcher stays either way: it identifies itself rather than impersonating a browser, and a site
entitled to refuse it should be able to. Working around these blocks is not on the table.

### 12.6 Tavily settles it, against the judgement in §12

Same definition, same shared corpus, same six queries, run twice — once through Brave with our
fetcher, once through Tavily.

| | Brave + our fetcher | Tavily |
|---|---|---|
| documents | 4 | **20** |
| controlling | 1 | **3** |
| persuasive | 0 | **7** |
| commentary | 3 | 10 |
| model cost | $0.0103 | $0.0435 |

Five times the documents and three times the controlling authorities. And they are the real thing:
**eCFR 21 CFR Part 514**, **Texas Occupations Code Chapter 801**, AMDUCA, and the **ASSOC model
licensed practice act PDF** — the last of which Brave-plus-fetcher could not read at all, because
`assoc.org` answers an Incapsula block page with HTTP 200.

Checked source by source, Tavily gets past two of the three block mechanisms of §12.5:

| source | our fetcher | Tavily |
|---|---|---|
| `fda.gov/media/83998/download` | 302 to an abuse-detection page | 18,146 chars |
| `vetboard.az.gov/statutes-and-rules` | HTTP 403 | 13,354 chars |
| `assoc.org/KB/…/PCR.aspx` | Incapsula block | 156 chars — still blocked |

**§12's reasoning was sound and its conclusion was wrong.** "Already on AWS argues for keeping state
on AWS, and this adds none" still holds; so does the note that swapping providers touches one
adapter. What that reasoning missed is that a search provider is not only a ranking service — it is
fetching infrastructure, and the difference between *its* fetching and ours is the difference between
a corpus of primary law and a corpus of law-firm blogs. §12.2 framed owning a fetcher as an
acceptable cost. On measurement it is not: it costs the feature its point.

`websearch.select` already prefers a text-supplying provider, so setting `TAVILY_API_KEY` is the
whole change. The Brave adapter stays — it is verified, it is the fallback, and having two providers
is what made this comparison possible at all.

The fetcher also stays, and is still needed: Brave remains a valid configuration, and a
text-supplying provider can return a summary instead of a page (below).

#### The bug this comparison exposed

Tavily returns its own ~150-character summary when its extraction fails, and `TavilySearch`
deliberately falls back to that summary because `supplies_text` promises the caller no fetcher is
needed. So a blocked page arrived as a 156-character "document" and entered the corpus — the same
failure as an Incapsula block page, on the path `webfetch.MIN_TEXT_CHARS` does not cover.

`research.MIN_DOCUMENT_CHARS` now floors provider-supplied text at the same 200, and a thin result is
recorded as **unreadable** rather than dropped, so the negative can say a source was seen and not
obtained. On the Tavily run it caught nine, including `assoc.org`, `law.cornell.edu` and
`texas.public.law` — all sources whose absence would otherwise have looked like absence of law.

### 12.3 The key

`BRAVE_API_KEY`, following the `openai_api_key` / `anthropic_api_key` pattern in `settings.py`.

Locally it lives in `.env`, which is gitignored (`.gitignore:36`).

**Deployed it must go to Secrets Manager, not a Lambda environment variable.** There is no
Secrets Manager usage in the stack today, so this is a new pattern and worth stating why: a Lambda
env var is readable by anyone holding `lambda:GetFunctionConfiguration`, which is a much wider set
than the people who should hold a search key, and this project's posture is that the API's own role
holds no ambient rights. The Research function gets `secretsmanager:GetSecretValue` on one secret
ARN and nothing else.

**Built, step 4.** `websearch.load_keys_from_secret` reads `SEARCH_SECRET_ARN` at cold start and
populates the provider variables in-process. Four decisions in it are deliberate:

- **The secret is a JSON object keyed by the provider variable names**, so one secret configures
  whichever provider the deployment has: `{"TAVILY_API_KEY": "…", "BRAVE_API_KEY": "…"}`.
- **A name this code does not know is ignored rather than exported.** Otherwise the secret would be
  a way to set arbitrary environment variables in the runtime, which is a much larger thing than
  configuring a search provider.
- **An exported key wins over the secret**, so a developer who set one locally has said which to
  use and `scripts/research_definition.py` works unchanged.
- **Only the variable NAMES are ever logged.** Not a length and not a prefix: a fingerprint in
  CloudWatch is the thing the secret exists to avoid.

It never raises. An unreadable secret means research reports itself `unavailable`, which §5.2
already handles, and raising would turn a missing key into a failed conversation.

The stack creates **no** secret — `search_secret_arn` names one the operator made. A CDK-generated
placeholder would sit there looking like a configured key while every search failed authentication.
An infra test asserts no provider key is an environment variable on any function.

---

## Related

- `docs/ENSEMBLE-CONVERSATIONS.md` — §2 for why research must be snapshotted per ensemble, §3.2 for
  the traffic-control premise research breaks, §5.2 for the axis it resembles
- `docs/PHASE6-KB-DESIGN.md` — the two binding scopes, grants, and the query-time re-check
- `docs/PHASE5-RETRIEVAL-DESIGN.md`, `tests/test_kb_source_floor.py` — the floor this extends
- `matrix_studio/documents.py` `ingest_text` — the seam a found page enters through
- `matrix_studio/personas.py` — `evidence_that_shifts`, rendered with `firmness`
