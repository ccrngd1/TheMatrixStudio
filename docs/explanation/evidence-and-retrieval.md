# Evidence: retrieval, knowledge collections, research and consultants

This page explains how documents reach a persona, why retrieval works the way it does, why
research now writes only into collections of its own, what consultants are for, and what the
citation check can and cannot tell you. For how to attach documents or turn research on, see
[`../how-to/`](../how-to/). For the settings, see [`../reference/`](../reference/).

## Why retrieval, rather than putting the documents in the prompt

There are three ways to give a persona its background material:

1. **Put every document in the prompt on every turn.** Perfect recall: the model sees everything.
   Expensive: five documents across 30 turns was estimated at about $1.28 a run against the
   $0.05–0.06 measured for real runs at the time, or about $0.13 with prompt caching.
2. **Have a model read the documents and pass on what it extracts.** Rejected outright. It destroys
   the trace from a claim back to a passage, it has to guess what a conversation will need before
   the conversation happens, and it generalises a failure already seen: a chunk cut mid-sentence
   once led a persona to quote it and infer the opposite of the source.
3. **Retrieve a few passages per turn, under a character budget.** What ships.

The deciding reason was **provenance**. Every turn that retrieves anything records a
`document.retrieved` event with the query and the exact passages used. That is what lets the
dossier answer "what was this persona looking at?", and what the citation check is built on. At the
corpus sizes involved, recall and cost were manageable either way, "which leaves provenance as the
deciding factor — and for a tool whose purpose is introspection, that is decisive"
([`../AWS-SERVERLESS-ARCHITECTURE.md`](../AWS-SERVERLESS-ARCHITECTURE.md) §8).

The budget matters as much as the retrieval. `max_chars` (1,200 characters by default) is "the
feature, not a safety valve": it keeps a forty-page attachment out of every call
([`../../matrix_studio/state.py`](../../matrix_studio/state.py), `RetrievalConfig`). On one
17,771-character document, 913 characters reached the prompt (README, Phase 5).

## Why vector retrieval

A turn's search query is not a search box. It is the last three messages of the conversation plus
the topic, which buries the relevant words in conversational prose. The project calls this a
*diluted* query, and it is the case that decides which method to use.

Re-measured on the deployed stack against this repository's own documentation (22 files, 666
chunks, 40 sampled queries), on diluted queries
([`../project/PHASE3-RECALL-MEASUREMENT.md`](../project/PHASE3-RECALL-MEASUREMENT.md)):

| | lexical (BM25) | vector | hybrid |
|---|---|---|---|
| right passage ranked first (recall@1) | 0.125 | **0.650** | 0.450 |
| right passage in the top five (recall@5) | 0.650 | 0.825 | **0.875** |

Recall@1 is the figure that matters, because a turn injects only a few passages and the first one
dominates. Lexical search put the right passage first one turn in eight. Hybrid wins on recall@5 but
loses the top slot, because equal-weight fusion lets a confident lexical wrong answer outrank a
correct semantic one. The same ordering held in the earlier local measurement, so it is reproduced
rather than a one-off. Vector is the default; the query embedding costs about $0.0000001 a turn.

Three supporting decisions:

- **1,024-dimension embeddings, measured.** On 128 queries, narrower vectors were as good on
  well-formed queries but lost on paraphrased ones: 256 dimensions scored 0.211 recall@1 against
  0.328 at 1,024. Paraphrase robustness is the reason for using vectors at all
  ([`../studies/EMBEDDING-DIMENSION-MEASUREMENT.md`](../studies/EMBEDDING-DIMENSION-MEASUREMENT.md)).
- **An off-topic guard, not a relevance filter.** Matches below a cosine of 0.15 are rejected. Over
  180 calibration retrievals, right and wrong passages overlapped almost completely (right
  0.228–0.870, wrong 0.166–0.699), so no threshold can tell them apart. What a threshold can catch
  is a query with nothing to do with the corpus, which scores about 0.0–0.07. At 0.15 the guard cost
  0 of 137 genuine hits ([`../project/PHASE5-RETRIEVAL-MEASUREMENT.md`](../project/PHASE5-RETRIEVAL-MEASUREMENT.md), 5h).
- **Degrade, do not fail.** If the embedding call fails, or a run's chunks were never embedded, the
  turn falls back to lexical search and carries on.

Two plausible query-tuning knobs, picking "discriminative" terms and dropping weak matches, were
tried first and made recall worse on every arm. On diluted queries recall@5 fell from 0.509 to 0.339.
Rarity is not relevance: conversational filler often supplies the rarest words. Both knobs remain,
off, so the measurement can be repeated
([`../project/PHASE5-RETRIEVAL-MEASUREMENT.md`](../project/PHASE5-RETRIEVAL-MEASUREMENT.md), "The cheap fixes were
tried first").

## Knowledge collections: documents, grants and bindings

A knowledge collection (a "knowledge base" in the code) is a named set of documents. Three separate
things govern who sees it ([`../project/PHASE6-KB-DESIGN.md`](../project/PHASE6-KB-DESIGN.md)):

- A **document** belongs to exactly one collection and is chunked and embedded once.
- A **grant** says who *may* read a collection: a user or a group. The owner needs none.
- A **binding** says which collections a persona *does* search in this conversation. A run-level
  binding is shared by the whole cast; a persona-level binding is that persona's alone.

Grants and bindings are kept apart deliberately. A shared corpus should not leak into a
conversation nobody intended, so both are needed. A binding to a collection you cannot read is
refused when the run is created, and re-checked on every turn, so a revoked grant stops working on
the next turn. A grant check that errors returns no collections at all. That is the opposite of the
retrieval rule above, on purpose: "a *retrieval* failure degrades to lexical, but an
*authorisation* failure denies".

Sharing is the one documented exception to the project's tenancy model. Everything else is
partitioned by user and protected by scoped credentials. A shared collection cannot be, because
someone else has to read it, so access becomes an authorisation decision with its own tests.

### The per-collection floor

Retrieval reserves one slot for each bound collection before the remaining slots are filled by rank
(`merge_with_source_floor` in [`../../matrix_studio/storage/vectors.py`](../../matrix_studio/storage/vectors.py)).

The reason is a measured failure. In one run, six personas each had a private collection and the
whole cast shared one more, holding the proposal under discussion. All 24 turns retrieved from the
shared collection and none from any persona's own. The ranking was not wrong. In a conversation
*about* the proposal, every turn's query is proposal-shaped, so the collection whose wording mirrors
the topic wins every time. The symptom was personas saying "I don't have a citation in front of me",
which read as caution rather than as a retrieval failure. "A binding that can never win a slot is
indistinguishable from no binding at all."

Two refinements follow from the same reasoning:

- **The speaker's own collections are reserved first.** Otherwise the topic-shaped cast-wide
  collection keeps winning, one level up.
- **An authority floor**, for research. A statute and thirty commentary chunks in the same
  collection compete for that collection's single reserved slot, and the commentary wins because
  it is written in the conversation's vocabulary. So when research is on, one slot can be bought for
  a passage tiered as controlling authority, but only by evicting a surplus passage from a collection
  that already has more than one. A collection contributing nothing is invisible; a missing statute
  is at least visible in the corpus as one no turn cited. "Given a choice of failures, take the one
  somebody notices" (`apply_floors`).

The floor gives something up, and says so: plain top-k across collections is exact, and the floor
breaks that on purpose.

## Research

With research on, before turn 1 the system searches the open web and builds collections: a shared
one, one per persona with convictions, and one per consultant. A persona's search covers its
position **and** the evidence it said would change its mind. That second half is not separately
switchable, because "searching only for support builds a confirmation-bias engine"
([`../PERSONA-RESEARCH.md`](../PERSONA-RESEARCH.md) §2.2). Found sources are tiered as controlling,
persuasive or commentary. When the search finds no controlling authority it records a *documented
negative*, with its queries and date, so "nobody looked" and "we looked and found nothing" become
different facts (§3–§4).

A few design points that follow from the purpose:

- **The researcher is not in the cast.** It builds a corpus and leaves. A researcher that took part
  would become the authority whose citations outrank everyone's (§2.1).
- **An ensemble researches once**, before any member exists, and every member reads the same
  collections. Independent searches would give replicates different inputs and make their
  differences unattributable (§6).
- **Research never fails a run.** It is additive; a failed search leaves an ordinary conversation.

### Why research now writes only into fresh collections

The first design wrote research into the collection already bound at each scope, when you owned
it. The reasons were real: a persona with a curated collection and a research collection would have
two places to look, and retrieval would split its slots between them for no gain (§5.1).

The cost showed up on the first live run. Because the floor reserves one slot per *collection*,
curated and found material in the same collection competed for that one slot. On one turn a persona's
hand-picked source material was displaced entirely by researched passages. That was accepted and
made visible, with "found" and "yours" badges in the dossier, rather than prevented.

Then the guardrails failed together, on 30 September and 1 October 2026. A run created from an
earlier run's setup, with research on, inherited that run's bindings and wrote its research into
seven hand-curated collections. A later run from the same setup then *replaced* the earlier run's
research in those collections, because a new research pass replaces its predecessor's batch. The
measured consequences (§5.1, "Reversed 2026-10-01"):

- curated passages were crowded out, compounding: one persona's fell from 37 to 8 across five runs;
- every other run bound to those collections, including no-research baselines in other ensembles,
  now retrieved web material it never asked for;
- an earlier run's citations now pointed at documents that had been removed.

Each rule had been correct for one run. The operator owned all seven collections, so the ownership
check passed. No curated document was deleted, so the origin check passed. The replaced batch was
the previous pass, so the replace rule did what it said. "Nothing in the design said a collection
*belonged to a pass*."

Now every pass creates its own collections, marked with the run or ensemble they belong to, and
binds them beside whatever was already bound. The Research state refuses to write anywhere not
marked for its own pass or holding anything a person uploaded. A copied setup leaves the earlier
run's research collections behind, because they are an output of that run, like its transcript
([`../../matrix_studio/research_state.py`](../../matrix_studio/research_state.py)).

**The trade-off it brings back.** The budget split the first design avoided is back. A persona with
its own curated collection now has two collections of its own, and with a run-level curated
collection and the shared research one there are four collections competing for the default three
slots. The speaker's own two are reserved first, the run-level two compete for the third by rank, and
rank decides less than it did. This was accepted because the alternative's cost was "different in
kind": a budget split is predictable, visible in every `document.retrieved` event, and adjustable
by raising `k`; silently rewriting collections that other runs and other users' baselines read is
none of those. Runs created before the change keep their bindings, and their curated collections
still hold the research written into them until a separate clean-up is run.

### What research was measured to do

Research was tested against a pre-registered criterion in three successive comparisons of five runs
per arm on one brief, about $29 in all ([`../PERSONA-RESEARCH.md`](../PERSONA-RESEARCH.md) §9):

- **It reached the room.** Every research run retrieved controlling authorities.
- **It did not settle the question the room kept asking.** In the first comparison the central
  question was left open in five of five research runs, as in five of five controls. After two
  changes to make documented negatives reachable, the third comparison still failed its primary
  criterion. The recorded verdict: research is **not useful for this brief**.
- **It did no harm.** Concessions held and the guardrails passed. Curated material kept 80% of its
  share of the prompt in the first comparison and 53% in the third, against a threshold of 50%.
- **Every research conversation converged**, 15 of 15, against 9 of 15 controls. That was never a
  criterion and was computed after seeing the data, so it is recorded as a hypothesis. It is equally
  consistent with research making the room stop arguing without having resolved anything.

The settled-or-folded study then read the converged endings. Research runs folded less often, not
more (3 of 15 against 3 of 9), with one rater and small numbers
([`../studies/CAPITULATION-STUDY.md`](../studies/CAPITULATION-STUDY.md)).

One more finding shapes how to read any researched corpus. On the first full pipeline run, 8 of 12
fetches failed, and they were the authoritative sites: they refuse automated readers, while vendor
pages and summaries do not. "The authority distribution is partly an artifact of the web's
defences", so a corpus of one controlling source and three commentaries is not a survey of what
exists (§12.5). A text-supplying search provider recovered more primary sources (§12.6), but tiering
is a model's judgement and is shown per passage so that a wrong tier can be argued with.

Research is off by default. See [Why the defaults are what they are](why-the-defaults.md).

## Consultants

Many runs were full of "someone needs to find out": a persona demanding a fact nobody in the room
could supply. A consultant is the answer to that. It is a named expert with its own documents and
collections. Personas ask it a specific question by ending a message with a line
`ASK <name>: <question>`. The engine removes that line, retrieves from the consultant's own sources,
and puts the answer into the transcript under "<name> (consultant)"
([`../../matrix_studio/experts.py`](../../matrix_studio/experts.py)).

What a consultant is not matters as much: it never takes a turn, is never in the speaker pool, has
no position and no goals, and answers only what was asked. When its sources do not answer the
question it says exactly "That isn't in my sources.", a fixed phrase so the miss can be counted.

Because a consultant answers only from documents, consultants require retrieval to be on, and the
API refuses them otherwise. Each consultation is a model call, so a run allows six by default. The
list of a consultant's document titles is shown to the personas because, on the first live run
without it, all four questions asked for things the sources did not contain.

## Inline citations, and what the citation check can and cannot check

### Why personas are asked to cite

Personas were always shown passages labelled `title #n` and told to cite them. Across 25 stored
runs, 816 of 824 messages had passages in their prompt, and none cited one by its label. Quoting a
passage word for word recovered the source for about 6% of messages; nothing recovered it for
paraphrase ([`../studies/CITE-INLINE.md`](../studies/CITE-INLINE.md)).

`retrieval.cite_inline` asks a persona to end a sentence that relies on a passage with its label in
square brackets, and to cite only passages it actually used. On a short two-persona probe it took
citing from 0.21 to 1.00 of messages. On a realistic six-persona, 40-turn cast it rose only from 0.00
to 0.26, against a target gap of 0.40. Both comparisons missed their primary criterion. It is on by
default by the operator's decision, not because it passed. The reasons and the open recommendation
are on [Why the defaults are what they are](why-the-defaults.md).

### What the check does

A citation is legitimate when it is either ([`../../matrix_studio/citations.py`](../../matrix_studio/citations.py)):

- **first-hand**: the speaker retrieved that passage, on this turn or an earlier one; or
- **second-hand**: the speaker credits another participant who really did cite it first-hand
  ("Tobias cited the incident review as saying…").

The second case is allowed on purpose. In a real meeting evidence travels through people, and
forbidding second-hand use "would destroy information the discussion needs". What the check
protects is the relationship. An attributive citation that is neither is *unverified*, and the
validation gate rejects the turn, regenerates it once, then flags it. Only attributive use is
judged: "I haven't seen that document you're referencing" is honest and passes.

It costs no model call: it is a lookup against passages the engine already recorded. The "earlier
turn" part was added after the check rejected a persona for citing a passage it had retrieved on an
earlier turn but not on the current one ([`../studies/CITE-INLINE.md`](../studies/CITE-INLINE.md), comparison 2).

### What it cannot check

- **Whether the passage says what the persona claims.** The check knows a label was in front of the
  speaker. It does not read the passage against the sentence.
- **A claim with no citation.** An uncited claim is not judged at all. An unhedged turn does not
  mean the claim is supported.
- **Paraphrase of something the speaker never retrieved**, if no label or title is used.
- **Whether the document itself is right.** A researched page is as reliable as its source and its
  tier.

There is a separate, optional disclosure for the opposite case
(`retrieval.disclose_unsupported`). When retrieval runs and finds nothing, the persona is asked to
say in its own voice that it has nothing in front of it. The wording is about provenance, not
support, because at the measured recall the passage often exists and was simply missed; "no
documentation supports this" would be false about one time in five
([`../project/PHASE5-RETRIEVAL-MEASUREMENT.md`](../project/PHASE5-RETRIEVAL-MEASUREMENT.md), 5g).

## Related

- [How a run works](how-a-run-works.md)
- [Reading the results](reading-the-results.md)
- The record: [`../PERSONA-RESEARCH.md`](../PERSONA-RESEARCH.md),
  [`../project/PHASE6-KB-DESIGN.md`](../project/PHASE6-KB-DESIGN.md),
  [`../project/PHASE5-RETRIEVAL-MEASUREMENT.md`](../project/PHASE5-RETRIEVAL-MEASUREMENT.md),
  [`../project/PHASE3-RECALL-MEASUREMENT.md`](../project/PHASE3-RECALL-MEASUREMENT.md),
  [`../studies/CITE-INLINE.md`](../studies/CITE-INLINE.md)
