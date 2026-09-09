# TheMatrix Studio on AWS: multi-tenant, serverless, Bedrock

Status: **proposed architecture**, revised 2026-09-09. Nothing here is built.

**Premise (revised).** AWS-only — local-run capability is explicitly dropped. The target
is something a large company installs: individuals sign in with Cognito and each has
their own private conversations. Serverless throughout.

Dropping local run is a genuine simplification, and it changes one decision: there is
no longer any reason for a storage abstraction with two adapters. One implementation,
targeted at the platform. (The previous revision's §13 on ports-and-adapters is
superseded and removed.)

Multi-tenancy is now the dominant constraint — larger than the serverless one. A
tenancy mistake leaks one person's conversations to another, and conversations here
contain whatever documents someone uploaded. So isolation is treated below as a
structural property to be enforced by IAM, not a filter to be remembered in code.

---

## 1. On SQLite in Lambda: the concern is right about the write path

Splitting this precisely, because the answer differs by path and the difference is
load-bearing.

### As the primary datastore: not viable. Agreed, and for more reasons than concurrency.

- Lambda's filesystem is per-execution-environment, so there is no shared write target.
- EFS makes the file shared but gives SQLite exactly one safe writer. Multi-user means
  concurrent writes, so this is not a tuning problem, it is the wrong storage model.
- SQLite has no primitive for tenant isolation. Every isolation guarantee would be an
  application-level `WHERE`, which is the weakest possible place to put a security
  boundary.

So runs, events, snapshots, summaries and threads all move to DynamoDB (§4).

### As a per-run retrieval index: also rejected, on a claim that turned out to be false

An earlier revision of this document argued for keeping SQLite as a per-run,
**read-only** FTS index in S3, on the grounds that it is immutable after ingest and
therefore has no write concurrency to reason about.

**That premise is wrong.** `attach_document` (`api/app.py`) performs no status check —
it 404s on a missing run and otherwise accepts the attachment — so a document can be
added to a run that is actively generating turns, and `retrieve_for_turn` re-reads the
index on **every** turn (`engine/simulator.py:1101`). A per-run S3 file would therefore
need download → rebuild → re-upload on each mid-run attachment, and two concurrent
attachments to the same run would silently lose one. That is the same write-concurrency
problem as the primary datastore, in the one place the argument claimed it was absent.

The other arguments for it do not survive the change of premise either:

- **Cost floor.** A managed search service bills an hourly capacity minimum whether or
  not anyone queries. For a single-operator tool that dominated the bill. For a
  thousand-employee install it is immaterial next to Bedrock spend, so the strongest
  objection to a central service disappears.
- **BM25 corpus scoping.** v0.6.0 corrected scoring so corpus statistics come from the
  run's own slice. That fix was motivated by **measurement reproducibility** — making
  experimental arms comparable across a database that grew between runs. It is not
  obviously the right choice for a product: deployment-wide IDF is defensible, arguably
  better, since a term being common across the whole corpus is genuine information about
  how discriminative it is. See §8 for what changes and what must be re-baselined.
- **Operational ownership.** A hand-rolled index lifecycle with rebuild-on-write is
  precisely the kind of component you do not want to hand a company to operate.
- **An org-wide corpus is likely, not hypothetical.** "Search our documents" is the
  obvious next request for a company install. Building a bespoke per-run mechanism first
  means running two retrieval systems later.

So retrieval goes to a central service too — see §8. SQLite leaves the architecture
entirely.

## 2. Identity: Cognito federated to the company IdP

A large company will not accept managing employee passwords in a new user pool, and
will expect SSO.

- **Cognito user pool** as the token issuer, with an **external identity provider**
  (SAML 2.0 or OIDC) federated in — Entra ID, Okta, Ping. Employees sign in with
  their existing corporate credentials; the pool issues the JWTs this application
  trusts.
- **API Gateway JWT authorizer** validates the token at the edge, so no unauthenticated
  request reaches a Lambda. `sub` is the stable user identifier.
- **SPA uses Authorization Code with PKCE**, not implicit. Tokens held in memory,
  refresh handled by the Cognito SDK.
- **Cognito groups** carry entitlement — tier, per-user spend cap (§7), and admin.
  Groups arrive as a JWT claim, so authorisation decisions need no extra lookup.

Provider credentials still never reach the browser: the app calls Bedrock with its
execution role. The only thing in the browser is the user's own OIDC token, which is
what it is for.

---

## 3. Tenant isolation: enforce it in IAM, not in application code

This is the most important recommendation in the document.

Use the **pool model** — shared tables, tenant in the partition key — because a table
per user does not scale to a large company. But do not rely on application code to
scope every query. Instead:

1. Partition keys are tenant-prefixed: `USER#{sub}` (see §4).
2. On each request, the API Lambda extracts `sub` from the validated JWT and calls
   `sts:AssumeRole` for a **session-scoped role** whose policy pins
   `dynamodb:LeadingKeys` to that user's partition, and `s3:prefix` to their prefix.
3. All storage access in that request uses those scoped credentials.

The consequence: **a missing tenant filter in application code cannot leak data.**
DynamoDB itself refuses the read. The security boundary moves from "every developer
remembers the predicate forever" to "the credentials cannot express the wrong query".
Cost is one STS call per session, cacheable for its duration.

The turn-execution Lambdas are not request-scoped, so they receive the owning `sub` in
the Step Functions execution input and assume the same scoped role. A run's execution
can therefore only touch its own owner's partition, which also contains any blast
radius from a bug in the engine.

**WebSocket needs the same care.** `$connect` must validate the JWT and store the
`sub` alongside the connection id, and fan-out must filter by owner. Otherwise the
live-event push is a cross-tenant leak with extra steps.

---

## 4. Data model (DynamoDB)

Single-table would work, but separate tables keep the access patterns legible and let
each carry its own capacity and TTL policy.

| Table | PK | SK | Notes |
|---|---|---|---|
| `runs` | `USER#{sub}` | `RUN#{run_id}` | The run row plus `stop_requested`, `owner_sub`, `total_cost_usd`. |
| `events` | `RUN#{run_id}` | `{seq}` | Append-only. `get_events_after(seq)` is a Query with `SK > seq` — the existing access pattern, natively. Carries `owner_sub` for fan-out filtering. |
| `snapshots` | `RUN#{run_id}` | `{turn}` | **Pointer only** — body in S3, see below. |
| `summaries` | `RUN#{run_id}` | `{kind}` | |
| `threads` / `thread_messages` | `RUN#{run_id}` / `THREAD#{id}` | | |
| `connections` | `RUN#{run_id}` | `{connection_id}` | Plus `owner_sub`. TTL for cleanup. |

GSIs, driven by the screens that exist: `runs` by `owner_sub` + `created_at` (the
history list), and by `owner_sub` + `status` (finding interrupted runs).

Note `events` and `snapshots` are keyed by run rather than user, since a run id is an
opaque UUID and the run's own row carries the owner. Their IAM scoping therefore comes
from the run-level check the API does first. If that indirection feels thin — and it is
the one place where isolation is a code path rather than a key prefix — key them
`USER#{sub}` / `RUN#{run_id}#{seq}` instead and pay a slightly wider partition. I would
take the stronger option for a company install.

**Snapshots must go to S3, not inline.** A snapshot holds the full transcript plus
every agent's state, including memory streams when cognition is on. DynamoDB's item
limit is 400 KB and a 40-turn cognition run will exceed it. Discovering that at turn 30
of a real conversation is how a user loses work.

---

## 5. Topology

Reflects every revision in this document: **no SQLite anywhere**, DynamoDB for
application data and chunk text, **S3 Vectors** for embeddings (retrieval is vector —
measured 22× better recall@1 than lexical on engine queries), one Step Functions
execution per run, polling for live updates. Nothing here has an hourly capacity floor.

### 5.1 System

```
 ┌────────────┐        OIDC / PKCE          ┌──────────────┐      SAML / OIDC
 │  employee  │◀───────────────────────────▶│   Cognito    │◀────────────────── company IdP
 │  (browser) │                             │  user pool   │                    (Entra/Okta)
 └──┬───┬─────┘                             └──────────────┘
    │   │  static
    │   └──────────────▶ CloudFront ──────▶ S3: built SPA
    │                    (index.html no-cache · /assets/* immutable)
    │
    │  JWT on every /api call
    ▼
 ┌──────────────────────────┐
 │  API Gateway (HTTP API)  │  JWT authorizer — unauthenticated requests
 │                          │  never reach a Lambda
 └────────────┬─────────────┘
              │
              ▼
 ┌────────────────────────────────┐   1. read `sub` from the verified JWT
 │  Lambda: API                   │   2. sts:AssumeRole scoped to that user
 │  FastAPI via Mangum            │      (dynamodb:LeadingKeys + s3:prefix)
 │  container image (litellm 91MB) │   3. all storage access uses those creds
 └───┬────────────────┬───────────┘
     │                │ scoped creds
     │ StartExecution │
     ▼                ▼
 ┌───────────────┐  ┌──────────────────────────────────────────────┐
 │ Step Functions│  │  DynamoDB                                    │
 │ (§5.2)        │─▶│   runs · events · snapshots(ptr)              │
         │  │  chunks (TEXT + metadata, no vectors)       │
 └───────┬───────┘  │   summaries · threads · knowledge_bases       │
         │          │   kb_grants · connections                    │
         │          └──────────────────────────────────────────────┘
         │          ┌──────────────────────────────────────────────┐
         │          │  S3  (per-user prefixes)                     │
         │─────────▶│   snapshots/{sub}/{run}/{turn}.json          │
         │          │   uploads/{sub}/{kb}/{doc}     (originals)   │
         │          └──────────────────────────────────────────────┘
         │          ┌──────────────────────────────────────────────┐
         │─────────▶│  S3 Vectors                                  │
         │          │   one index per KB · chunk embeddings +      │
         │          │   {owner_sub, kb_id, chunk_id} metadata      │
         │          └──────────────────────────────────────────────┘
         │
         ▼
 ┌──────────────────┐
 │  Bedrock         │  execution role, no keys anywhere
 │  text + Stability│  (Stability pinned us-west-2, separate from text)
 └──────────────────┘

 Live updates (v1): the browser polls GET /api/runs/{id}/events?after_seq=N
                    — already supported by the client, no new subsystem.
       (v2, only if needed): DynamoDB Streams on `events` → fan-out Lambda
                    → API Gateway WebSocket, filtered on owner_sub.
```

### 5.2 The turn loop (one Step Functions Standard execution per run)

```
 StartExecution { run_id, owner_sub, from_turn, budget }
        │
        ▼
 ┌───────────────────┐   only when bindings or uploads changed:
 │ IngestDocuments   │   extract → chunk (sentence-aligned overlap)
 │                   │   → text to DynamoDB · original to S3
 │                   │   → embed chunks → S3 Vectors  ($0.0014/387 chunks)
 └─────────┬─────────┘
           ▼
 ┌───────────────────┐   reconstruct_at_turn(N) from the event log
 │ PrepareTurn       │   → agents, transcript, thread ledger
 │                   │   → select next speaker
 └─────────┬─────────┘
           ▼
 ┌───────────────────┐   embed the query (Bedrock, ~$1e-7)
 │ GenerateTurn      │   → S3 Vectors k-NN, filtered to bound KBs
 │                   │     (vector mode: measured 22x recall@1 vs lexical
 │  Retry: throttle, │      on the diluted queries a turn produces)
 │  validation fail  │   → BatchGetItem chunk text → top-k under max_chars
 │                   │   → Bedrock call → Phase 4a validation gate
 │                   │   → append events (incl. document.retrieved)
 └─────────┬─────────┘   → snapshot: DynamoDB pointer + S3 body
           ▼
 ┌───────────────────┐
 │ CheckContinue     │──── stop_requested (DynamoDB) ────▶  Stopped
 │  (Choice)         │──── cost ≥ run cap or user cap ──▶  Capped
 │                   │──── turn ≥ budget ───────────────▶  Complete
 └─────────┬─────────┘
           └──── else ──▶ back to PrepareTurn
```

Every terminal state writes the run's status and a final snapshot. Branch and resume are
the same machine started with a different `from_turn`; a stop is honoured *between*
turns, so the turn in flight is always finished and persisted.

### 5.3 Documents, knowledge bases and what a persona may search

```
  document ──────────▶ knowledge_base ◀────── kb_grant ──▶ user | Cognito group
  (chunked once,       (the unit of                        (may read)
   never re-indexed)    binding AND sharing)
       │                      ▲
       │ chunks               │ bound by id
       ▼                      │
  DynamoDB: text       ┌──────┴────────────────────────────┐
  S3 Vectors: embeds   │  run                              │
  { kb_id, doc_id,     │   knowledge_bases: [...]  ← whole │
       ▲               │   cast:                     cast  │
       │               │     Priya  knowledge_bases: [...] │
       │               │     Dan    knowledge_bases: [...] │
       │               └──────┬────────────────────────────┘
       │                      │
       └──────────────────────┘
         a speaker's scope = run.knowledge_bases ∪ persona.knowledge_bases
         → S3 Vectors k-NN over those KB indexes → chunk text from DynamoDB
```

Reuse is binding, not copying: a document is chunked and stored once and any conversation
that binds its KB can search it. Cast-wide is the run-level binding. Grants must be
re-checked at **query** time, not only when a binding is created, or a revoked grant
keeps working.

## 6. The turn loop: Step Functions Standard, one execution per run

Measured from the 38 real runs here: a turn takes **6–13 s**, a 30-turn run **3–6 min**.
That fits a 15-minute Lambda — but a 40-turn run with validation-gate retries does not,
and the budget is user-controlled. So the loop belongs in an orchestrator.

The states are drawn in §5.2; this section is the reasoning behind the choice.

This is tractable because `branching.reconstruct_at_turn()` **already exists** — the
engine can rebuild exact state from the event log, so a turn is already a pure function
of (log up to N) → (new events, snapshot). That is precisely a state-machine iteration.

Why Standard workflows:

- **No duration ceiling** (up to a year), so a long or repeatedly-resumed conversation
  is not a special case.
- **The stop semantics survive unchanged.** Stop already works by polling a predicate
  *between* turns so the in-flight turn finishes and is persisted; in Step Functions
  that predicate is a DynamoDB read in `CheckContinue`. Same place, same guarantee. It
  also fixes something multi-user makes urgent: the flag is currently an in-memory set,
  so today a stop only works if the request lands on the process running the turn.
- **The cost cap becomes an orchestrator state** rather than engine code a future
  caller could bypass — and per-user caps (§7) plug in at the same point.
- **Validation regeneration and Bedrock throttling both map onto `Retry`** with
  exponential backoff, which matters much more with many concurrent users.
- **Concurrency is free**: one execution per run, no shared in-process state.

Cost is ~90 state transitions per 30-turn run — fractions of a cent, irrelevant beside
Bedrock tokens.

Branch and resume need no new machinery: both already reconstruct-and-generate-forward,
so each is a new execution with a different `from_turn`.

---

## 7. Bedrock at company scale

- **Auth by execution role**, `bedrock:InvokeModel` scoped to specific model ARNs. No
  keys anywhere — strictly better than today's bearer token in `.env`.
- **Throttling is the real operational risk.** Bedrock quotas are per-account,
  per-model requests- and tokens-per-minute. Many employees running conversations
  concurrently will hit them. Mitigations, in order: `Retry` with backoff on the
  `GenerateTurn` state (free, and Step Functions does it natively); **cross-region
  inference profiles** to spread load; request a quota increase; **provisioned
  throughput** only if guaranteed capacity is genuinely required, since it is a
  standing cost.
- **Per-user spend control** — a company will require this before rollout. The
  per-run cap already exists; add a per-user monthly cap keyed off the Cognito group,
  checked in `CheckContinue` against a rolling total on the user's row. Refuse to
  *start* a run when a user is over budget, which is cheaper and clearer than stopping
  one mid-way.
- **Model invocation logging** to S3/CloudWatch, which security review will ask for.
- **Region pinning survives**: the avatar model (`stability.sd3-5-large-v1:0`) is
  already configured separately in `us-west-2` from the text model. That split must be
  preserved, not flattened.

---

## 8. Retrieval: vector search via S3 Vectors

**Revised again.** Told that a persona will hold roughly 1–5 documents, the earlier
recommendation of OpenSearch Serverless is wrong — it is a distributed search cluster
being asked to rank a few dozen short passages.

Measured against the real documents in this repository (8 documents, mean 11,383
characters):

| Persona's corpus | Chars | ~Tokens | Chunks at 900 chars |
|---|---|---|---|
| 1 document | 11k | 2,800 | 13 |
| 5 documents | 57k | 14,200 | 63 |
| 10 documents | 114k | 28,500 | 126 |

Sixty-three chunks is small — small enough that the earlier revision concluded no search
infrastructure was needed at all. That conclusion was right about *scale* and wrong about
*method*, for the reason below.

### Correction: retrieval must be VECTOR, and this project already measured it

The previous revision recommended in-process BM25 and was wrong. It contradicted
`docs/PHASE5-RETRIEVAL-MEASUREMENT.md` §5f, which measured all three modes against the
same ground truth. On **diluted** queries — the shape the engine actually produces, since
a turn's query is built from conversational text rather than a well-formed search string:

| metric | fts | vector | hybrid |
|---|---|---|---|
| recall@1 | **0.017** | **0.367** | 0.233 |
| recall@5 | 0.400 | 0.817 | 0.700 |
| MRR | 0.127 | 0.549 | 0.409 |

`recall@1` is the figure that matters, because a turn injects only `k = 1..3` passages.
Lexical put the right passage first **1.7% of the time**. That is not a subtlety to trade
against infrastructure simplicity; it means lexical-only retrieval on engine turns
mostly does not work.

The measurement also settles the mode per caller: **`vector` for engine turns**, and
**`hybrid` for the operator-facing `/documents/search`**, where queries are well-formed
and fusion helps. Hybrid *loses* to pure vector on diluted queries, because equal-weight
RRF lets a noise-grade lexical ranking drag down a good semantic one.

Cost is not the gate it was assumed to be: embedding a 231,744-character corpus (387
chunks) measured **$0.0014**, and a per-turn query embedding ~**$0.0000001**.

### Recommended: S3 Vectors for embeddings, DynamoDB for chunk text

Given retrieval must be vector, a vector store is needed either way, and **S3 Vectors is
the right one for this workload's shape** — bursty, low QPS, small-to-medium corpora,
priced as storage plus per-query rather than provisioned capacity. It is the option that
makes the earlier cost objection moot without reintroducing an hourly floor.

| Holds | Where | Why there |
|---|---|---|
| chunk **text** + `kb_id`, `doc_id`, `ordinal` | DynamoDB | Needed to return passage content, and for the lexical arm of `hybrid`. One Query or BatchGetItem. |
| chunk **embedding** + filter metadata | S3 Vectors | Purpose-built. Keeps 4 KB float32 vectors out of DynamoDB items and off the per-turn fetch. |

Per turn: embed the query (one Bedrock call) → query S3 Vectors filtered to the speaker's
bound KBs → `BatchGetItem` the winning chunks' text from DynamoDB. Two round trips,
sub-second plus single-digit milliseconds, against a 6–13 s turn.

Why S3 Vectors over the alternatives now that a vector store is required:

- **Versus OpenSearch Serverless** — no hourly capacity floor, which was the whole
  objection. Same metadata filtering, same k-NN capability for this use.
- **Versus vectors in DynamoDB with in-process cosine** — workable at 63 chunks (258 KB
  of float32 per turn) but it is a key-value store used as a vector store: bulky items,
  bulky fetches, and a hand-rolled brute-force scan that has to be replaced the moment a
  corpus outgrows it. S3 Vectors removes the escalation cliff instead of deferring it.
- **Versus Bedrock Knowledge Bases** (which can sit on S3 Vectors) — Knowledge Bases
  takes over chunking, and this codebase's chunking has a measured justification: overlap
  is snapped to a sentence boundary because a naive cut produced a chunk starting
  `". This is a correctness requirement, not hardening."` and a persona quoted it and
  inferred the **opposite** of the source. Calling S3 Vectors directly keeps our chunker
  and our embedding choice.

**Isolation.** Filtering is by metadata, so the §8a caveat applies rather than
`LeadingKeys`: make it one chokepoint that requires the authenticated subject, and assert
on the way out. Where a stronger boundary is wanted, use **one vector index per KB** —
the same reasoning as §8b, and IAM can then grant per index rather than per document.

**Keep lexical, but as the second arm.** `hybrid` is the measured best mode for the
operator's search, so in-process BM25 over the same DynamoDB chunks stays — it is ~50
lines, needs no service, and now serves the caller it actually helps.

**Before committing**, verify against current AWS capability: supported dimensions for
the chosen embedding model, metadata-filter expressiveness (it must express
`owner_sub` + `kb_id IN [...]`), per-index vector limits, and whether index-per-KB is
practical at the expected KB count. S3 Vectors is newer than the rest of this design, so
these are checks rather than assumptions.

### Why not feed the documents to an LLM and pass forward what it extracts

This is the tempting alternative at small scale, and it is worse than both retrieval and
plain stuffing. Three reasons, in order of weight:

1. **It destroys the provenance trace, which is this product's differentiator.** Every
   turn currently emits a `document.retrieved` event carrying the query and the exact
   passages used — `chunk_id`, `document_id`, `title`, `ordinal`. That is what lets the
   dossier answer "which passages did it use", and `citation_integrity` is part of the
   Phase 4a validation gate. An LLM digest cannot be traced back to a passage, so the
   tool stops being able to show its work.
2. **"Relevant" has no fixed referent.** Extract at ingest and you must guess what the
   conversation will need — but a multi-turn debate goes places nobody planned, and a
   fixed digest cannot answer an unanticipated question. Extract at turn time and you
   have to read the whole document to do it, which is stuffing (below) *plus* an extra
   model call and its latency. Turn-time extraction is strictly dominated.
3. **It generalises a failure this project has already measured.** Chunk overlap is
   sentence-aligned because a naive cut produced a chunk beginning `". This is a
   correctness requirement, not hardening."` — a persona quoted it verbatim and inferred
   the **opposite** of the source. An LLM digest is that risk everywhere, and harder to
   detect, because there is no verbatim passage left to compare against.

The **good version of the instinct** is additive rather than substitutive: a one-time,
ingest-time document abstract stored alongside the chunks, used to help choose *which*
document or KB to search and to show the operator what they attached. Cheap, computed
once, and it does not replace passage-level retrieval.

### The honest alternative: stuff the documents and use prompt caching

At 3k–14k tokens per persona this is genuinely viable, and it deserves stating fairly
rather than dismissing.

Cost, at Sonnet-class input pricing: 5 documents ≈ 14,200 tokens × 30 turns ≈ 427k input
tokens ≈ **$1.28 per run**, against the $0.05–0.06 measured for real runs — roughly 20×.
With **Bedrock prompt caching** it becomes defensible: put the document block before the
transcript so it is a stable prefix, and cache reads cost a fraction of fresh input,
bringing it to roughly $0.13 per run. Turns are 6–13 s apart and a persona speaks every
~8 turns in an 8-person cast, comfortably inside the cache TTL.

What stuffing buys: **perfect recall** — no retrieval miss is possible, because the model
sees everything — and the deletion of retrieval as a subsystem.

What it costs: **the provenance trace**, the `max_chars` per-turn budget that
`PROJECT-SPEC` calls "the feature, not a safety valve", and any path to a corpus larger
than the context window.

So the choice is: retrieval buys provenance and cost control; stuffing buys recall and
simplicity. At this corpus size recall is a non-issue either way and cost is manageable
either way, which leaves **provenance as the deciding factor** — and for a tool whose
purpose is introspection, that is decisive. Hence in-process BM25, which keeps the trace
at essentially zero infrastructure.

---

### Rejected: OpenSearch Serverless

Retained here for the reasoning. Its hourly capacity floor was the objection, and S3
Vectors provides the same capability for this use without one. Used directly rather than
through Bedrock Knowledge Bases, had it been chosen.

**Why not Knowledge Bases**, despite being Bedrock-native and more managed: it takes
over chunking, and this codebase's chunker has measured behaviour worth keeping. Chunk
overlap is snapped to a sentence boundary because a naive tail cut produced a real
failure — a chunk beginning `". This is a correctness requirement, not hardening."` had
lost the antecedent of "This", and a persona quoted it verbatim and inferred the
**opposite** of what the source meant. On one real document 90% of chunks began
mid-sentence before the fix. Handing chunking to a managed service discards that, and
the failure it prevents is silent. Knowledge Bases also runs on OpenSearch Serverless
underneath, so it does not avoid the cost floor.

**Isolation is the part to get right, and here it is a query filter rather than an IAM
boundary.** OpenSearch Serverless data access policies operate at collection and index
granularity, not per document, so per-user scoping cannot be enforced by credentials the
way DynamoDB's `LeadingKeys` allows (§3). An index per user would restore that, but
thousands of tiny indexes is a known anti-pattern — shard overhead, and hard limits.

So accept a code-level boundary and make it **one auditable chokepoint** rather than a
predicate repeated at call sites:

- Exactly one function may talk to the search client, and it takes the authenticated
  subject as a required argument — there is no overload without it.
- Every query it issues carries the `owner_sub` and `run_id` filters.
- Enforce it with a test that greps for any other import of the search client, in the
  same spirit as the other structural rules here. A convention nobody checks is not a
  control.

For a multi-company deployment, add an index per tenant: that granularity *is*
IAM-enforceable via data access policies, and it caps the index count at the number of
customers rather than users or conversations.

**What changes behaviourally, stated so it is not discovered later.** Corpus statistics
become deployment-wide rather than per-run, which is the opposite of the v0.6.0 fix.
Consequences:

- Retrieval scores are no longer comparable to any number recorded in `docs/`. Every
  absolute retrieval measurement in this repository must be re-baselined against the new
  backend before it is cited again.
- Scores now move as the deployment's corpus grows. That is acceptable for a product and
  unacceptable for an experiment, so any future retrieval measurement needs a fixed
  corpus snapshot to be meaningful.
- Ranking quality is likely *better* for the product case and is worth measuring rather
  than assumed — the inspection endpoint (`/documents/search`) already exists for
  exactly this and should be used to compare before and after on a real corpus.

**What comes free**, and did not before: native k-NN, so vector and hybrid retrieval no
longer need `sqlite-vec` in a Lambda temp file; and an org-wide shared corpus can live
in the same collection under a different index with a `scope` field, queried alongside a
user's own documents.

**The cost this adds**, honestly: the test suite currently runs retrieval against
in-process SQLite — fast and hermetic. Against OpenSearch it needs a container in CI for
the retrieval tests (~55 of them in `test_retrieval*.py`), or a fake, which is a fake of
the exact scoring semantics being relied on and therefore not worth much. Budget for the
container.

## 8a. How a persona's knowledge base is scoped

> With in-process scoring (§8) the filter below is not a query sent to a search
> service — it is which chunks are fetched from DynamoDB, so `LeadingKeys` enforces it
> and the client-side-filter caveat does not apply. The OpenSearch form is retained for
> the escalation tier.

The mechanism is **indexed metadata fields plus filter context** — but two things about
that deserve stating precisely, because one is a real limitation and the other is a
design question the current model does not answer well.

### The mechanical answer

Every chunk today already carries `run_id` and a nullable `persona_name`
(`doc_chunks`), and search scopes with `persona_name = ? OR persona_name IS NULL` —
"this persona's own documents plus anything cast-wide". Indexed as fields, the
document becomes:

```json
{
  "owner_sub":   "…",          // tenant
  "run_id":      "…",
  "persona_name":"Priya",      // absent = visible to the whole cast
  "kb_id":       "…",          // see below
  "scope":       "run",        // run | org
  "document_id": "…", "title": "…", "ordinal": 7,
  "content":     "…"           // the only analysed field
}
```

and the query puts every scoping predicate in **`filter` context**, not `must`:

```json
{"query": {"bool": {
  "must":   [ {"match": {"content": "<terms>"}} ],
  "filter": [
    {"term": {"owner_sub": "<from the JWT>"}},
    {"term": {"run_id": "<run>"}},
    {"bool": {"should": [
      {"term": {"persona_name": "Priya"}},
      {"bool": {"must_not": {"exists": {"field": "persona_name"}}}}
    ]}}
  ]
}}}
```

`filter` rather than `must` matters for two reasons: filter clauses **do not contribute
to the score**, so scoping cannot perturb ranking, and they are cacheable. A scoping
predicate in `must` would silently make a persona's own documents rank differently from
the same documents in another persona's slice.

That `should` block is the literal translation of `persona_name = ? OR persona_name IS
NULL`. Note `must_not exists` is the correct way to express "cast-wide" — a missing
field, not a `null` value, since a JSON null and an absent field index differently.

### The limitation: on Serverless, this filter is client-side

OpenSearch's security plugin supports **document-level security** — a role carries a
query filter and the *cluster* enforces it, so a client cannot see documents outside its
scope even if it forgets the predicate. That is the enforcement model you would want
here.

**OpenSearch Serverless does not offer it.** Its data access policies are collection-
and index-granular; there is no per-document rule. (Worth re-checking against current
AWS capability before building — this is the kind of gap AWS closes — but assume it is
absent.) So the filter is the application's responsibility, which is weaker than the
`dynamodb:LeadingKeys` enforcement §3 uses for the primary datastore.

Two mitigations, both cheap, and worth having together because this is the one boundary
IAM cannot hold:

1. **Make the scope impossible to omit at the call site.** One value object,
   constructible only from an authenticated principal plus a run plus a persona, and a
   single search function that accepts nothing else. No raw-query path, no overload
   without a scope. Backed by a test that fails if anything other than that module
   imports the search client.
2. **Assert on the way out.** After a search returns, verify every hit's `owner_sub`
   and `run_id` match the requested scope; drop mismatches and raise an alarm. It costs
   a loop over *k* hits and converts a silent cross-tenant leak into a detectable
   error. Cheap insurance for a boundary enforced by a predicate.

### The design question: knowledge bases should probably be first-class

`persona_name` is a **string inside one run**, and documents are scoped
`run_id + persona_name` (`doc_chunks`, and `documents.run_id` is a foreign key to the
run). That means a knowledge base is not a thing — it is an attachment to one
conversation. Two consequences that will bite a company install immediately:

- **No reuse.** Giving the same three PDFs to a persona in a new conversation means
  uploading them again, re-chunking, re-indexing and re-storing. A company with a
  standing "Legal" or "Security" corpus will expect to define it once.
- **Duplication multiplies within a run too.** Attaching one document to eight personas
  stores it eight times — which is exactly the effect measured in the cast-wide
  investigation, where duplication collapsed the document's BM25 score to zero because
  the term appeared in 8 of 16 chunks.

So model knowledge bases as their own objects and bind them to personas:

| Concept | Where it lives |
|---|---|
| `knowledge_base` | Owned by a user or a team. Has a name, and documents. |
| `kb_documents` / chunks | Indexed **once**, tagged `kb_id` (+ `owner_sub`, `scope`). No `run_id`. |
| persona → KB binding | On the run's cast: `knowledge_bases: ["kb-legal", "kb-migration"]` |

Retrieval then filters `kb_id` against the bindings for the speaking persona:

```json
{"filter": [
  {"term":  {"owner_sub": "<from the JWT>"}},
  {"terms": {"kb_id": ["kb-legal", "kb-migration"]}}
]}
```

This is a better answer to the original question than per-run tagging, because "which
knowledge base may this agent search" becomes an explicit, inspectable binding on the
cast rather than a string match on a persona's display name. It also fixes three things
at once: reuse across conversations, the duplication that distorted scoring, and the
cast-wide case (a KB bound to every persona, stored once) that was shelved as
`WILL NOT IMPLEMENT` partly *because* the per-run model made it costly.

It does mean per-conversation ad-hoc uploads become "an implicit KB scoped to this
run" — still one code path, with `scope: "run"` and a `kb_id` derived from the run.

**One caveat to size before committing:** binding a persona to a shared KB means its
BM25 corpus statistics are those of the whole KB, so a persona's retrieval quality now
depends on a corpus it does not own and that other conversations also use. That is the
right trade for a company (a shared corpus should have shared statistics) but it must be
measured on a real KB with the inspection endpoint, not assumed.

## 8b. Sharing and reuse: documents, collections, bindings

Today one row conflates three different things: the document's **content**, the
**collection** it belongs to, and the **grant** that says who may read it. `documents`
has a `run_id` foreign key and a `persona_name`, so a document *is* its own grant, scoped
to one conversation. That is why none of sharing, reuse or cast-wide visibility works
without duplication.

Separating the three makes all of it fall out:

| Entity | Holds | Cardinality |
|---|---|---|
| `document` | The extracted text and its chunks. Indexed **once, ever**. | 1 |
| `knowledge_base` | A named collection of documents. The unit of binding and of sharing. | 1 doc → 1 KB |
| `kb_grant` | Which principals may read a KB (user, or Cognito group for a team). | 1 KB → N grants |
| `binding` | On a run: which KBs a persona may search. | N personas → N KBs |

**A document belongs to exactly one KB.** Not many-to-many, deliberately. Multi-KB
membership would mean either duplicating chunks per KB — the duplication this whole
thread has been avoiding — or carrying a `kb_ids` array and rewriting it with
`update_by_query` on every membership change, which is a non-transactional write
proportional to chunk count. One KB per document keeps chunks immutable after indexing.
"This document belongs in two collections" is served by a KB of one document, bound
twice.

### The three cases, answered

**Can documents be reused by personas?** Yes — reuse is binding, not copying. The
document is indexed once; a persona in any conversation that binds its KB can search it.
Nothing is re-chunked, re-embedded, re-stored or re-indexed.

**One document shared to a whole conversation.** Two levels of binding, which generalise
today's `persona_name = ? OR persona_name IS NULL` exactly:

```jsonc
{
  "knowledge_bases": ["kb-migration-policy"],      // run level: every persona
  "cast": [
    {"name": "Priya", "knowledge_bases": ["kb-sre-runbooks"]},   // hers alone
    {"name": "Dan",   "knowledge_bases": ["kb-board-materials"]}
  ]
}
```

The effective scope for a speaker is `run.knowledge_bases ∪ persona.knowledge_bases`.
Run-level binding is the cast-wide case — and note it is now **cheap**, stored once,
which removes the reason cast-wide documents were shelved as `WILL NOT IMPLEMENT`. That
decision was made because the per-run model made a shared document cost 8× storage and
collapsed its BM25 score to zero; neither is true here.

**One document shared to multiple personas across conversations.** Put it in a KB, grant
the KB to whoever needs it, bind it wherever it is wanted. Sharing and binding are
separate operations: a grant says *may* read, a binding says *does* read in this
conversation. Both are needed, which is what stops a shared corpus leaking into
conversations nobody intended.

### Index granularity: one index per KB (escalation tier only)

This is the decision that also repairs the isolation weakness §8a had to concede.

**Recommended: one OpenSearch index per knowledge base**, when KB count stays in the low
hundreds. Three things fall out at once:

1. **Isolation becomes IAM-enforceable again.** Data access policies are index-granular,
   so a principal's policy can name the KB indexes they hold a grant for. That is the
   `LeadingKeys`-grade enforcement §8a said was unavailable — it was unavailable at
   *document* granularity, but a KB is a coarser and much more natural boundary. The
   per-document filter reduces to a per-index permission.
2. **Corpus statistics land on the right unit.** A term's discriminativeness within the
   Legal corpus is a property of the Legal corpus, not of the whole deployment and not
   of one conversation. This is a better answer than either extreme discussed earlier.
3. **A multi-KB search is a native multi-index search** (`/kb-a,kb-b/_search`), so a
   persona bound to three KBs is one query.

The catch, and its fix: with several indexes, BM25 statistics are computed per index, so
scores from different KBs are not strictly comparable — the same class of problem as the
cross-run contamination fixed in v0.6.0, one level up. OpenSearch's
`search_type=dfs_query_then_fetch` computes global term statistics across the searched
indices first, which is precisely the remedy. It costs an extra round trip, which against
a 6–13 s turn is irrelevant. **Use it, and note that a query spanning KBs and one
scoped to a single KB will then score consistently.**

**Fall back to one shared index with a `kb_id` term filter** if KB count becomes
unbounded — thousands of small indexes is a shard-overhead anti-pattern and there are
collection limits to check. That trade is: simpler operationally, but isolation returns
to being a filter (§8a's mitigations apply) and IDF becomes deployment-wide.

### What sharing costs the tenancy model — stated, not hidden

§3's isolation rests on every partition key being prefixed `USER#{sub}`, so credentials
can be pinned with `dynamodb:LeadingKeys`. **A shared KB breaks that**, necessarily: a
user must read a KB owned by someone else, so KB access cannot be scoped by "your own
partition". It needs a grant lookup, which is an authorisation *decision* rather than a
partition constraint.

This is inherent to sharing, not a flaw in the design, but it means:

- KB metadata lives in its own table keyed by `KB#{kb_id}` with a grants collection, not
  under a user partition.
- The grant check is a real authorisation code path — so it is the second place after
  §8a's search chokepoint that deserves a dedicated test suite, including the negative
  cases (revoked grant, grant to a group the user has left, binding to a KB the user
  never had a grant for).
- Revocation must be checked at **query time**, not only at binding time. Otherwise a
  binding created while a grant existed keeps working after it is revoked — a stale
  binding is the obvious way this design leaks.

## 9. Live updates

**Start with polling.** The client already supports `getEvents(ref, after_seq)`, so a
2-second poll needs almost no new code and is barely perceptible against a 6–13 s turn.
At company scale, watch the cost: N concurrent viewers × 0.5 rps against DynamoDB is
cheap but not zero, and it is a per-viewer cost that WebSocket amortises.

**Then WebSocket** when the polling cost or the latency justifies it: `$connect`
validates the JWT and records `(run_id, connection_id, owner_sub)`; DynamoDB Streams on
`events` triggers a fan-out Lambda that posts to that run's connections **after
filtering on owner**. The existing replay-then-tail logic carries over via `after_seq`.

---

## 10. What in the current code blocks multi-tenancy

Concrete, from the code rather than in principle:

1. **There is no notion of a user anywhere.** No owner on a run, no auth on any route.
   Every table, every route and the WebSocket need an owner concept. This is the bulk
   of the application work.
2. **Run names are globally unique** — `runs_name_unique ON runs(name)`
   (`storage/database.py:172`). Two employees could not both have a run codenamed
   `trusted-robot`. Uniqueness must become per-user, and the name-generation retry loop
   must scope its collision check accordingly.
3. **`get_run_by_ref` accepts an id *or* a name.** Name lookup must be scoped to the
   caller; id lookup must verify ownership. This is precisely the kind of route where
   the IAM scoping of §3 earns its cost, because the check is easy to forget.
4. **The orphaned-run sweep runs at process startup** (`api/app.py:482`) — there is no
   startup in Lambda. It becomes either an EventBridge scheduled sweep, or better,
   Step Functions' own failure path writing the terminal status, which is more accurate
   because the orchestrator knows the execution died.
5. **Four pieces of in-process state** — `_tasks`, `_stop_requested`, `_brokers`,
   `_subscribers` (`api/manager.py`). The first two become DynamoDB attributes; the
   last two become the WebSocket connections table.
6. **Package size**: litellm alone is 91 MB, the full environment 343 MB, over Lambda's
   250 MB unzipped limit. Use a **container image Lambda** (10 GB). Dropping litellm for
   direct `bedrock-runtime` calls would be smaller but abandons provider-agnosticism
   for no benefit here, since the target is Bedrock-only.
7. **CloudFront must reproduce the SPA cache policy** — `index.html` no-cache, hashed
   assets immutable. Its absence caused a blank-page hang fixed in v0.6.0; the same bug
   is one misconfigured cache behaviour away.

---

## 11. Migration path

**Phase 1 — tenancy in the domain model.** Add an owner to runs and thread it through
every route and query; make name uniqueness per-user; add authorisation checks. Do this
*before* the platform port, against SQLite, where the 709 existing tests give fast
feedback. This is the phase most likely to be under-estimated.

**Phase 2 — storage port.** Replace the SQLite implementation with DynamoDB + S3. One
implementation, no abstraction (local run is out of scope). The existing tests are the
specification; run them against DynamoDB Local in CI. Retrieval moves to OpenSearch
Serverless in the same phase (§8), which is the largest single behavioural change in the
port and the one that needs a before/after quality comparison rather than a green
test suite.

**Phase 3 — deploy.** Cognito + IdP federation, API Gateway with the JWT authorizer,
scoped-role assumption, container Lambda, CloudFront/S3, polling for live updates. The
turn loop still runs inside one Lambda here — fine up to ~30 turns, and it proves the
tenancy and storage work under real conditions before adding an orchestrator.

**Phase 4 — Step Functions.** Removes the duration ceiling; restores stop and cost caps
as orchestrator states; adds per-user spend caps.

**Phase 5 — WebSocket**, if polling cost or latency justifies it.

---

## 12. Open decisions

1. **Sharing.** "Their own conversations" implies private by default, but a company
   install will want "show this to a colleague" almost immediately. Read-only share
   links, or team-scoped visibility via Cognito groups? This changes the key design, so
   it is much cheaper to decide now than to retrofit.
2. **One tenant or many?** "Installed for a large company" reads as one company per
   deployment, in which case `USER#{sub}` suffices. If one deployment must serve several
   companies, keys need `TENANT#{org}#USER#{sub}` and the IdP federation becomes
   per-tenant.
3. **Admin visibility.** Who can see all runs, and can they read conversation content or
   only spend and volume? A company will want cost attribution; whether that includes
   reading employees' conversations is a policy question with privacy implications, and
   it should be answered explicitly rather than fall out of who holds an IAM role.
4. **Retention and deletion.** Employees leave; GDPR-style deletion requests happen.
   Deleting a user's data must reach DynamoDB items, S3 snapshots, S3 indexes and
   uploads. Worth designing while the key structure is still malleable — per-user
   prefixes everywhere make it a prefix delete rather than a scan.
5. **Encryption.** Default AWS-managed keys, or customer-managed KMS keys? A per-tenant
   CMK is a strong isolation story for a security review and cheap at this scale.
6. **Analytics.** DynamoDB serves the access patterns the UI has, but not "show me every
   run about X across the org". If that is wanted, add a DynamoDB-Streams-to-S3 path and
   query with Athena rather than distorting the operational key design.
7. **Cost ownership.** Is Bedrock spend charged back to teams? If so, tag or attribute
   invocations per user from the start, since it is nearly impossible to reconstruct
   later.

---

## 13. Cost shape

Idle cost is dominated by whatever has an hourly floor. In this design nothing does —
Lambda, DynamoDB on-demand, Step Functions, API Gateway, S3 and CloudFront are all
per-request, so an idle deployment costs cents. Cognito charges per monthly active
user, and federated users are priced differently from pool-native ones; check current
pricing for the expected headcount, as this is the one line item that scales with
employees rather than usage.

**Bedrock dominates variable cost.** Measured here: $0.05–0.06 per 16–30 turn
conversation. For a thousand employees running one conversation a week that is roughly
$200–250/month in tokens — which is why the per-user cap in §7 is a product
requirement, not a nicety.

The design deliberately avoids services with a standing capacity floor (OpenSearch
Serverless, Aurora Serverless v2 minimum ACUs, provisioned Bedrock throughput). Adding
any of them changes idle cost from cents to hundreds of dollars a month, so each should
be a conscious decision with a reason attached.

Verify current per-unit prices before committing; the numbers above are shapes, not
quotes.
