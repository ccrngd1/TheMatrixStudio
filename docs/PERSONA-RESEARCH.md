# Pre-conversation research: what the room could not find out for itself

Opened 2026-09-23. **Nothing here is implemented.** A design, written before any code so the
reasoning is on the record rather than reconstructed afterwards.

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
and hidden it. `tests/test_kb_source_floor.py` is the existing hook; extending it to an authority
tier is part of this feature, not a follow-up.

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
| Queries per viewpoint | 2 | one for the position, one for `evidence_that_shifts` |
| Recency limit | none | a settled case from 2011 is not stale; a blog from 2011 is. Tiering handles it better than a date filter |

The button should name the multiplier, as the ensemble one does: research is 1 + N corpora, and an
operator should see "researching 7 collections" before agreeing to it.

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
4. **The Research state** in the machine, reusing a bound KB where there is one, pre-allocating an
   id where there is not, and failing additively (§5.1, §5.2).
5. **The UI toggle** (§7).
6. **The ensemble path** — research once, bind to all members (§6).
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

### 12.3 The key

`BRAVE_API_KEY`, following the `openai_api_key` / `anthropic_api_key` pattern in `settings.py`.

Locally it lives in `.env`, which is gitignored (`.gitignore:36`).

**Deployed it must go to Secrets Manager, not a Lambda environment variable.** There is no
Secrets Manager usage in the stack today, so this is a new pattern and worth stating why: a Lambda
env var is readable by anyone holding `lambda:GetFunctionConfiguration`, which is a much wider set
than the people who should hold a search key, and this project's posture is that the API's own role
holds no ambient rights. The Research function gets `secretsmanager:GetSecretValue` on one secret
ARN and nothing else.

---

## Related

- `docs/ENSEMBLE-CONVERSATIONS.md` — §2 for why research must be snapshotted per ensemble, §3.2 for
  the traffic-control premise research breaks, §5.2 for the axis it resembles
- `docs/PHASE6-KB-DESIGN.md` — the two binding scopes, grants, and the query-time re-check
- `docs/PHASE5-RETRIEVAL-DESIGN.md`, `tests/test_kb_source_floor.py` — the floor this extends
- `matrix_studio/documents.py` `ingest_text` — the seam a found page enters through
- `matrix_studio/personas.py` — `evidence_that_shifts`, rendered with `firmness`
