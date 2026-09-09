# TheMatrix Studio on AWS: multi-tenant, serverless, Bedrock

Status: **proposed architecture**, revised 2026-09-09. Nothing here is built.

**Verification.** The S3 Vectors, Bedrock prompt-caching and DynamoDB claims below were
checked against AWS service documentation and the AWS Well-Architected **Generative AI
Lens** (Nov 2025), not written from memory. That pass corrected one outright error (a
single query cannot span vector indexes), inverted one constraint (index count is not the
limiting factor — fan-out is), and surfaced one gap the design had missed entirely (choosing
the embedding dimension deliberately — §8). Claims still resting on measurement rather than
documentation are the ones drawn from this repository's own `docs/PHASE5-RETRIEVAL-MEASUREMENT.md`
and `data/matrix_studio.db`, and are labelled as measured where they appear.

**Premise (revised).** AWS-only — local-run capability is explicitly dropped. The target
is something a large company installs: individuals sign in with Cognito and each has
their own private conversations. Serverless throughout.

Dropping local run is a genuine simplification, and it changes one decision: there is
no longer any reason for a storage abstraction with two adapters. One implementation,
targeted at the platform. (The previous revision's ports-and-adapters section is
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
  run's own slice, to make experimental arms comparable across a database that grew
  between runs. This turned out to be the wrong axis to argue on at all: §8 settles that
  engine retrieval must be **vector**, and cosine similarity is pairwise — there are no
  corpus statistics to scope, contaminate or reproduce. See §8's note on why that
  dissolves rather than resolves the question.
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
   `dynamodb:LeadingKeys` to that user's partition, and restricts S3 object ARNs to
   `…/{sub}/*` (with `s3:prefix` on the `ListBucket` action, which is where that
   condition key applies).
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

## 4. Data model (DynamoDB tables)

> What lives in DynamoDB versus S3 versus S3 Vectors, and why, is §4a. This section is
> the DynamoDB key design only.

Single-table would work, but separate tables keep the access patterns legible and let
each carry its own capacity and TTL policy.

| Table | PK | SK | Notes |
|---|---|---|---|
| `runs` | `USER#{sub}` | `RUN#{run_id}` | The run row plus `stop_requested`, `owner_sub`, `total_cost_usd`. |
| `events` | `USER#{sub}` | `RUN#{run_id}#{seq}` | Append-only. `get_events_after(seq)` is a Query with `SK > RUN#{id}#{seq}` — the existing access pattern, natively. User-prefixed so `LeadingKeys` reaches it; see below. |
| `snapshots` | `USER#{sub}` | `RUN#{run_id}#{turn}` | **Pointer only** — body in S3, see below. |
| `summaries` | `RUN#{run_id}` | `{kind}` | |
| `threads` / `thread_messages` | `RUN#{run_id}` / `THREAD#{id}` | | |
| `connections` | `RUN#{run_id}` | `{connection_id}` | Plus `owner_sub`. TTL for cleanup. |
| `knowledge_bases` | `KB#{kb_id}` | `META` | Name, owner. **Not** under a user partition — a shared KB is read by principals who do not own it (§8b). |
| `kb_grants` | `KB#{kb_id}` | `PRINCIPAL#{sub\|group}` | Permission. Checked at **query** time, not only at binding time. |
| `documents` | `KB#{kb_id}` | `DOC#{doc_id}` | **Metadata only** — title, `char_count`, `chunk_count`, media type. Text is in S3, passages in S3 Vectors (§4a). |

GSIs, driven by the screens that exist: `runs` by `owner_sub` + `created_at` (the
history list), and by `owner_sub` + `status` (finding interrupted runs).

**Why `events` and `snapshots` are user-prefixed, not run-prefixed.** Keying them
`RUN#{run_id}` would be the more obvious shape, and a run id is an opaque UUID whose own
row carries the owner — so scoping *could* rest on the API checking run ownership first.
That was the earlier draft and it is the weaker option: it makes isolation a code path
rather than a key prefix, and it is the one table pair `LeadingKeys` would not reach. For
a company install, take the wider partition and keep every table enforceable by
credentials. A single run's events stay contiguous within the user's partition because the
sort key leads with `RUN#{run_id}`.

**Snapshots must go to S3, not inline.** A snapshot holds the full transcript plus
every agent's state, including memory streams when cognition is on. DynamoDB's item
limit is 400 KB and a 40-turn cognition run will exceed it. Discovering that at turn 30
of a real conversation is how a user loses work.

---

## 4a. Division of labour: DynamoDB vs S3 vs S3 Vectors

Three stores needs a rule, not a list. The rule is **access shape**, not data type:

| Access shape | Store |
|---|---|
| Mutated in place, needs conditional/atomic updates | DynamoDB |
| Ordered append + range read, with read-after-write consistency | DynamoDB |
| Point lookup of a small item by key | DynamoDB |
| Large, immutable, written once and read rarely | S3 |
| Similarity search | S3 Vectors |

Applied to this application, with sizes **measured from the 38 real runs** in
`data/matrix_studio.db` rather than estimated:

| Data | Measured size | Store | Why there |
|---|---|---|---|
| `runs` | small | DynamoDB | Mutated constantly — status, accumulated cost, `stop_requested`. Needs conditional writes and GSIs for list-by-owner. |
| `events` | **80/run, mean 1.2 KB, max 2.4 KB** | DynamoDB | Atomic append; `get_events_after(seq)` is `Query SK > n`; polling needs read-after-write. See below. |
| **avatar images** | **2.2 MB, base64 inside an `avatar.ready` event** | **S3** | Over DynamoDB's 400 KB item limit. Must be extracted — see below. |
| `snapshots` | **mean 45 KB, max 2.2 MB; 1 of 619 over 400 KB** | S3 body + DynamoDB pointer | Large, immutable, write-once-read-rarely. The one oversized row proves the limit is reachable in normal use. |
| document **full text** (normalised) | mean 11 KB | S3 | One object per document. Serves whole-document reads and the lexical arm. |
| chunk **embedding + text** | ~4 KB vector, 749 B text | S3 Vectors | Text rides as vector metadata, so one k-NN call returns ids, scores *and* passages. |
| document/KB **metadata** | small | DynamoDB | `title`, `char_count`, `chunk_count`, `kb_id`, owner — for listing. No content. |
| uploaded originals | MBs | S3 | Write once, read for audit ("which document did this come from"). |
| KBs, grants, summaries, threads, connections | small | DynamoDB | Mutable, key-addressed. |

### Chunk text does not belong in DynamoDB

An earlier revision put chunk text there. That was inertia from the SQLite schema, where
`doc_chunks.content` was the only place text could live — not a reasoned placement.

Only three things ever read document text, and none of them wants a DynamoDB item:

| Reader | Needs | Best source |
|---|---|---|
| A turn (hot path) | the *k* retrieved passages | **S3 Vectors metadata** — the k-NN call already returns them, so there is no second lookup at all |
| The lexical arm of `hybrid`, for `/documents/search` | all of a KB's chunks | **S3** — a handful of document objects (mean 11 KB each), re-chunked in memory. `chunk_text()` is deterministic, so re-chunking the same normalised text reproduces the same chunks and the same `ordinal`s the vectors were built from. |
| Setup export (`document_text`) | one whole document | **S3** — a single GET |

Two consequences worth having:

- **The hot path drops from two round trips to one.** Vector search returns ids, scores
  and passage text together; DynamoDB leaves the retrieval path entirely.
- **`join_chunks` becomes unnecessary.** That function — and the overlap-deduplication it
  performs — exists *only* because chunks were the sole store of text, so reassembling a
  document meant stitching overlapping chunks back together (measured: naive joining added
  4,253 and 4,859 duplicated characters to two real documents). Store the normalised full
  text as one S3 object and `document_text()` is a GET. The machinery was correct for the
  constraint it was written under; the constraint disappears here.

Provenance is unaffected: `document.retrieved` already records only `chunk_id`,
`document_id`, `ordinal`, `score`, `title` and `chars` — no content — so the trace needs
no text store at all.

**To verify before committing:** S3 Vectors per-vector metadata limits, and the
filterable-versus-non-filterable distinction — passage text should be *non-filterable*
metadata, since only `owner_sub` and `kb_id` need to be filtered on and filterable
metadata is the constrained kind. If the text will not fit, the fallback is not DynamoDB
but the S3 document object plus per-chunk offsets carried in the vector metadata.

### What the DynamoDB document row is actually for

Not "describing the document" — S3 could do most of that, since `LIST` gives keys and
sizes and object metadata could carry a title. It earns its place for four things that
S3 cannot do, traced from the real consumers (`list_documents` has six callers).

1. **Strongly consistent listing.** An operator uploads a document and expects to see it
   in the persona's list immediately. S3 `LIST` is not strongly consistent for that, so
   the document would intermittently appear missing right after upload — the worst moment
   for it to look broken.
2. **The authorisation join.** Which KB a document belongs to, and therefore who may read
   it, is a relationship, not a property of a byte range. The delete path already relies on
   this shape today (`app.py:1567` builds the set of documents owned by the run before
   permitting a delete), and on AWS every read needs it to check a grant (§8b).
3. **The cleanup manifest across three stores.** Deleting a document means removing the S3
   object *and* its vectors from S3 Vectors. To delete the right vectors you need to know
   how many chunks there were and their ordinals — which is exactly what `chunk_count`
   is for. Without a row, deletion becomes "query the vector store by filter and hope",
   and user-level deletion (§12.4) becomes unbounded. This is the least obvious job and
   the hardest to retrofit.
4. **Facts that do not survive the trip to S3.** `chunk_count` is a property of the
   chunking, not of the object. Original `media_type` — that this was a PDF rather than a
   `.docx` — is lost the moment only extracted text is stored, and it is what the operator
   needs to recognise their own file. Ingest status and any failure reason likewise.

**One honest near-redundancy:** `char_count` is approximately the S3 object's size, and
keeping it only saves a `HEAD` per object on the list endpoint. That is a fair trade for a
list view, but it *is* denormalisation and a denormalised count can drift from the object
it describes. Treat the S3 object as authoritative if they ever disagree.

**One consumer that disappears:** `retrieval.py:452` reads `chunk_count` to compute a
document-frequency ratio for lexical term selection. That feature is default-off and was
measured harmful, and the vector path has no use for it, so this is not a reason to keep
the row.

**One that gets cheaper:** `copy_documents_to_run` copies a parent's documents into a
branch today. Under the KB model a branch inherits *bindings* instead, so nothing is
copied at all.

### Why the event log specifically cannot live in S3

This is the least negotiable placement in the design, and worth stating because "it is
just an append-only log, put it in S3" is the obvious cheaper idea.

1. **S3 has no atomic append.** One object per event means hundreds of tiny objects per
   run and a `LIST` to enumerate them — and `LIST` is not strongly consistent, so the
   polling path would intermittently see *gaps* in the log. One growing object means
   read-modify-write, so two concurrent writers silently lose events. The event log is
   the source of truth from which all state is reconstructed; losing one is
   unrecoverable.
2. **The access pattern is a range read.** `get_events_after(seq)` — used by polling,
   replay and `reconstruct_at_turn` — is `Query` with `SK > n`, natively. In S3 it is a
   `LIST` plus client-side sort.
3. **Read-after-write matters.** The UI polls immediately after a turn lands. A strongly
   consistent DynamoDB read returns exactly what was written.

By contrast, snapshots *are* safe in S3 precisely because they are written once, never
mutated, and addressed by a deterministic key.

### One thing this analysis found: avatars must move to S3

`avatar.ready` carries the generated image as base64 **inside the event payload** — 2.2 MB
in the one real instance here. On DynamoDB that is a hard failure, not a slow path, since
it exceeds the 400 KB item limit. It is also wrong on the current SQLite backend for a
softer reason: it puts a megabyte of image data into the append-only log that every
replay and every `reconstruct_at_turn` reads.

The fix is the same either way: write the image to S3 and put the key in the event. Worth
doing **before** the port rather than during it, since it is a small change that is
testable locally and it removes a hard blocker from the migration.

## 5. Topology

Reflects every revision in this document: **no SQLite anywhere**, DynamoDB for
application data and metadata only, **S3 Vectors** for embeddings and passage text
(retrieval is vector —
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
 │  FastAPI via Mangum            │      (LeadingKeys + S3 ARN prefix)
 │  container image (litellm 91MB) │   3. all storage access uses those creds
 └───┬────────────────┬───────────┘
     │                │ scoped creds
     │ StartExecution │
     ▼                ▼
 ┌───────────────┐  ┌──────────────────────────────────────────────┐
 │ Step Functions│  │  DynamoDB — small, mutable, key-addressed     │
 │ (§5.2)        │─▶│   runs · events · snapshots (pointers)        │
 └───────┬───────┘  │   summaries · threads · connections           │
         │          │   knowledge_bases · kb_grants                 │
         │          │   documents (METADATA only — no text)         │
         │          └──────────────────────────────────────────────┘
         │          ┌──────────────────────────────────────────────┐
         │          │  S3 — large, immutable (per-user prefixes)    │
         │─────────▶│   snapshots/{sub}/{run}/{turn}.json          │
         │          │   docs/{sub}/{kb}/{doc}.txt   (full text)     │
         │          │   uploads/{sub}/{kb}/{doc}    (originals)     │
         │          │   avatars/{sub}/{run}/{name}.png              │
         │          └──────────────────────────────────────────────┘
         │          ┌──────────────────────────────────────────────┐
         │─────────▶│  S3 Vectors — similarity search              │
         │          │   one index per KB                           │
         │          │   vector + metadata {owner_sub, kb_id,       │
         │          │   doc_id, ordinal, PASSAGE TEXT}             │
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
 │                   │   → full text + original to S3
 │                   │   → embed chunks → S3 Vectors, text as metadata
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
 │  validation fail  │     (passages return WITH the vectors — no second
 │                   │      lookup) → top-k under max_chars
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
  document ───────────▶ knowledge_base ◀────── kb_grant ──▶ user | Cognito group
  (chunked once,        (unit of binding                    (may read)
   never re-indexed)     AND sharing)
       │                       ▲
       │                       │ bound by id
       ▼                       │
  ┌─────────────────────┐  ┌───┴──────────────────────────────┐
  │ S3                  │  │  run                             │
  │  docs/…/{doc}.txt   │  │   knowledge_bases: [...]  ← whole│
  │  (normalised text)  │  │   cast:                    cast  │
  ├─────────────────────┤  │     Priya  knowledge_bases: [...]│
  │ S3 Vectors          │  │     Dan    knowledge_bases: [...]│
  │  one index per KB   │  └───┬──────────────────────────────┘
  │  vector + metadata: │      │
  │   owner_sub, kb_id, │      │  scope = run.knowledge_bases
  │   doc_id, ordinal,  │◀─────┘        ∪ persona.knowledge_bases
  │   PASSAGE TEXT      │
  └─────────────────────┘  k-NN over those KB indexes
                           → passages returned inline, no second lookup

  DynamoDB holds only document/KB METADATA (title, counts, owner) for listing.
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

### Recommended: S3 Vectors for embeddings and passage text

Given retrieval must be vector, a vector store is needed either way, and **S3 Vectors is
the right one for this workload's shape** — bursty, low QPS, small-to-medium corpora,
priced as storage plus per-query rather than provisioned capacity. It is the option that
makes the earlier cost objection moot without reintroducing an hourly floor.

| Holds | Where | Why there |
|---|---|---|
| document **full text** | S3 | One object per document. Serves whole-document reads and the lexical arm of `hybrid`. |
| chunk **embedding + passage text** | S3 Vectors | Text as vector metadata, so one k-NN call returns everything a turn needs. See §4a. |

Per turn: embed the query (one Bedrock call) → query S3 Vectors filtered to the speaker's
bound KBs → passages come back with the vectors. **One** round trip to the store, not two;
DynamoDB is not in the retrieval path at all.

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
operator-facing `/documents/search`, so BM25 stays — computed in-process over the
document text fetched from S3 (§4a), not over DynamoDB. It is ~50 lines, needs no
service, and now serves the one caller the measurement says it helps.

### Choose the embedding dimension deliberately, not by default

The AWS Well-Architected **Generative AI Lens** (Nov 2025) raises this twice, and the design
had not addressed it at all: **GENCOST04-BP01 "Reduce vector length on embedded tokens"** and
**GENPERF04-BP02 "Optimize vector sizes for your use case"**. Vector length drives storage
cost, query cost and latency, and an index's dimension is **immutable after creation**
(§8b) — so this is a decision to make once, with evidence, before the first index exists.

Titan Text Embeddings v2 — the model the Phase 5f measurement used, so the model whose
numbers transfer — supports **256, 512 and 1024** output dimensions. The design should not
simply take 1024 because it is the default. S3 Vectors accepts 1 to 4096.

What to do, and it is cheap because the ground truth already exists: re-run the Phase 5f
recall measurement at 256 and 1024 on the same corpus and queries. That is the lens's
**GENPERF04-BP01 "Test vector embeddings for latency and relevant performance"**, and this
repository is unusually well placed to do it — the labelled queries, the diluted/paraphrased
arms and the recall@1/recall@5/MRR harness are all already built. If 256 holds recall, it is
a 4× reduction in vector storage and query cost for free; if it does not, the measurement
says so before the choice is locked in.

**Everything else previously flagged as "verify before committing" is now verified** against
the service documentation and resolved inline: metadata size limits (§8a), filter
expressiveness (§8a), per-index and per-bucket limits (§8b), and whether one query can span
indexes (§8b — it cannot). The one remaining open item is the dimension choice above, and it
is a measurement rather than a lookup.

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
bringing it to roughly $0.13 per run. The cache TTL is **five minutes and resets on each
successful hit** (confirmed in the Generative AI Lens, GENCOST03-BP03), and a checkpoint
needs a **minimum prefix length** — 1,024 tokens for Claude 3.7 Sonnet. Both work here: a
persona's 3k–14k-token document block clears the minimum comfortably, and in an 8-person
cast a persona speaks every ~8 turns at 6–13 s each, so ~50–100 s between its own turns —
inside the TTL, and each hit extends it.

What stuffing buys: **perfect recall** — no retrieval miss is possible, because the model
sees everything — and the deletion of retrieval as a subsystem.

What it costs: **the provenance trace**, the `max_chars` per-turn budget that
`PROJECT-SPEC` calls "the feature, not a safety valve", and any path to a corpus larger
than the context window.

So the choice is: retrieval buys provenance and cost control; stuffing buys recall and
simplicity. At this corpus size recall is a non-issue either way and cost is manageable
either way, which leaves **provenance as the deciding factor** — and for a tool whose
purpose is introspection, that is decisive — retrieval stays.

(This section was written when the recommendation was in-process BM25. The conclusion
survives the correction above: retrieval wins over stuffing on provenance either way, and
vector retrieval makes the case stronger, since it is the mode measured to actually find
the right passage.)

---

### Rejected: OpenSearch Serverless

Kept for the reasoning, not as a fallback. It has an **hourly capacity floor** billed
whether or not anything queries, and S3 Vectors provides the k-NN and metadata filtering
this design needs without one. Everything else about it — index-per-KB granularity,
application-enforced document filtering — is the same trade, so the floor decides it.

Had it been chosen, it would have been used **directly rather than via Bedrock Knowledge
Bases**, for the reason in §8: Knowledge Bases takes over chunking, and this codebase's
sentence-aligned overlap exists because a naive cut produced a chunk beginning
`". This is a correctness requirement, not hardening."` — a persona quoted it and inferred
the **opposite** of the source, with 90% of chunks starting mid-sentence before the fix.
Knowledge Bases also runs on OpenSearch Serverless underneath, so it inherits the floor.

*(Guidance that used to live in this section — the search chokepoint, the outbound
assertion, per-KB index granularity — applies to S3 Vectors and has moved to §8a and §8b,
where it will actually be read.)*

### What this costs the test suite

Retrieval tests currently run against in-process SQLite: fast, hermetic, no services. On
S3 Vectors they cannot, and the honest options are both imperfect:

- **A fake vector store** — a few dozen lines doing exact cosine in memory. Adequate for
  the *plumbing* (is the filter applied, is the scope asserted, is the ranking preserved)
  and worthless for *quality*, since it fakes the thing being relied on.
- **A real vector index in CI**, one per test run, torn down after. Slower and needs
  credentials, but it is the only way a retrieval-quality test means anything.

Recommended split: the fake for the ~55 existing retrieval tests, which are about plumbing
and scoping; plus one **quality suite** against a real index, run deliberately rather than
on every commit, using the ground truth already built for `PHASE5-RETRIEVAL-MEASUREMENT.md`.
That preserves the fast inner loop and keeps the measured recall numbers honest — which
matters, because the switch from lexical to vector is exactly the kind of change a green
plumbing suite would pass while retrieval quality silently regressed.

## 8a. How a persona's knowledge base is scoped

The mechanism is **vector metadata plus a filter on the k-NN query**. Two things about it
need stating precisely: where the enforcement boundary really is, and one consequence that
turns out to remove a problem rather than create one.

### The mechanical answer

Every chunk today carries `run_id` and a nullable `persona_name` (`doc_chunks`), and
search scopes with `persona_name = ? OR persona_name IS NULL` — "this persona's own
documents plus anything cast-wide". Under the KB model (§8b) that becomes a `kb_id`, and
each vector is stored with metadata:

```jsonc
{
  "owner_sub": "…",        // tenant — filterable
  "kb_id":     "kb-legal", // filterable; the binding unit
  "scope":     "run",      // run | org — filterable
  "doc_id":    "…",
  "ordinal":   7,
  "text":      "…"         // the passage itself: NON-filterable metadata
}
```

and the query filters on the KBs the speaker is bound to:

```jsonc
// QueryVectors — one call per bound KB index (see §8b on fan-out)
{
  "indexName": "kb-legal",
  "queryVector": { "float32": [ /* the embedded query */ ] },
  "topK": 6,
  "returnMetadata": true,          // brings the passage text back with the hit
  "returnDistance": true,
  "filter": {
    "owner_sub": "<from the verified JWT>",
    "scope": { "$in": ["run", "org"] }
  }
}
```

`$in`, `$eq`, `$and`/`$or` and `$exists` are all supported filter operators, so the scoping
predicates this design needs are expressible directly.

**One IAM detail that is easy to miss and would fail closed at runtime:** requesting
metadata *or* using a metadata filter requires **both** `s3vectors:QueryVectors` **and**
`s3vectors:GetVectors`. With only `QueryVectors` a caller can retrieve keys and distances
but the request returns **403** the moment it filters or asks for metadata — and this design
does both on every turn. Note also that S3 Vectors uses its own `s3vectors` IAM namespace,
separate from `s3`, so the vector policy is written independently of the object-store one
in §3.

Three details that matter, **verified against the S3 Vectors documentation**:

- **Passage text as non-filterable metadata is the documented intended use.** The service
  guide states non-filterable metadata is "ideal for storing large text chunks" and gives
  "full document text" as the example. It is retrieved with query results via
  `returnMetadata`, which is exactly the one-round-trip property §4a relies on.
- **The size budget is ample.** Per vector: **40 KB total metadata**, of which **2 KB may be
  filterable**, across up to 50 keys. A 749-byte passage plus five short filter fields sits
  far inside both. The earlier "verify whether the text will fit" caveat is resolved — it
  fits with roughly 50× headroom.
- **Non-filterable keys are declared at index creation and are immutable** (max 10 per
  index). `text` must therefore be declared non-filterable when each KB index is created;
  it cannot be reclassified later. See §8b.
- **Cast-wide is a binding, not a null field.** The earlier draft expressed "visible to
  the whole cast" as an absent `persona_name`, which required an awkward
  `must_not exists` predicate. Under §8b it is simply a KB bound at run level, so the
  filter is the same `kb_id` `$in` clause with one more id in it. Simpler, and no
  null-versus-absent trap.

### The consequence worth noticing: the corpus-statistics problem dissolves

This document has argued about corpus statistics four times — v0.6.0's per-run BM25 fix,
whether a shared index reintroduces it, whether per-KB indexes put IDF on the right unit.
**Choosing vector retrieval ends the argument rather than settling it.** Cosine similarity
is computed between the query vector and each chunk vector; it involves no corpus at all.
So:

- A score is a property of the query and the passage, nothing else. It cannot be moved by
  another tenant's data, another conversation's data, or the deployment growing.
- The v0.6.0 property — a score being reproducible and a property of the run — is
  preserved **by construction**, not by scoping.
- There is nothing to re-baseline on the engine path, and no need for a fixed corpus
  snapshot to make a future measurement meaningful.

The one place corpus statistics survive is the **lexical arm** of `hybrid`, used only by
the operator-facing `/documents/search`. That is computed in-process over the documents of
the KBs being searched (§4a), so its corpus is exactly the set asked about — scoped by
construction there too.

### Where the enforcement boundary is

`dynamodb:LeadingKeys` (§3) does not reach a vector store: the tenant predicate is a query
filter the application supplies, so it is application-enforced rather than
credential-enforced. Two mitigations, worth having together because this is the one
boundary IAM does not hold by itself:

1. **Make the scope impossible to omit at the call site.** One value object, constructible
   only from an authenticated principal plus the resolved KB bindings, and a single search
   function that accepts nothing else — no raw-query path, no overload without a scope.
   Backed by a test that fails if any other module imports the vector client.
2. **Assert on the way out.** After a query returns, verify every hit's `owner_sub` and
   `kb_id` are in the requested scope; drop mismatches and alarm. It costs a loop over *k*
   hits and turns a silent cross-tenant disclosure into a detectable error.

**And then take the boundary back** where it matters: the service documentation confirms
policies can grant access to **individual vector indexes**, all indexes in a bucket, or all
buckets. So with **one vector index per KB** (§8b) a principal's policy names exactly the KB
indexes they hold a grant for, and the per-document filter reduces to a per-index
permission — credential-enforced again. The filter still applies as defence in depth, but it
stops being the only thing standing between two tenants.

**One thing that turned out better than assumed:** writes to S3 Vectors are **strongly
consistent**, so a document is searchable immediately after ingest. There is no
read-your-writes gap to design around — which is what makes the "operator uploads and
expects to see it work" case in §4a hold on the retrieval side too, not just the listing
side.

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
collapsed its BM25 score to zero; the first is fixed by binding, and the second cannot
happen under vector retrieval at all.

**One document shared to multiple personas across conversations.** Put it in a KB, grant
the KB to whoever needs it, bind it wherever it is wanted. Sharing and binding are
separate operations: a grant says *may* read, a binding says *does* read in this
conversation. Both are needed, which is what stops a shared corpus leaking into
conversations nobody intended.

### Index granularity: one vector index per KB

Verified against the S3 Vectors service documentation rather than assumed. Two earlier
claims here were wrong, and the correction changes what the limiting factor is.

**Index count is not the constraint.** The service allows **10,000 vector indexes per
vector bucket** and **2 billion vectors per index**, so index-per-KB is viable at real
company scale — not merely "the low hundreds" as an earlier draft cautioned.

**A multi-KB search is NOT one query.** `QueryVectors` takes a single `indexName` (or
`indexArn`), so a persona bound to three KBs is **three queries, fanned out and merged
client-side** — the opposite of what an earlier draft claimed. This is the real cost of
index-per-KB, and it is modest: the queries are independent so they parallelise, each is
sub-second, and a persona typically binds one or two KBs. Against a 6–13 s turn it does
not register.

**Merging is trivially correct, which is the part that would not have been true with
BM25.** Cosine distance is pairwise between the query and each vector, so results from
`kb-legal` and `kb-migration` are directly comparable and a merge is just a sort. Under
BM25 statistics are per index, scores would not share a scale, and OpenSearch's
`dfs_query_then_fetch` exists precisely to paper over that. So vector retrieval makes
fan-out cheap where lexical would have made it wrong.

So the trade is now clear and it is not the one stated before:

| | index per KB | single index, `kb_id` filter |
|---|---|---|
| Isolation | **IAM-enforceable** — policies can grant on individual vector indexes | Application-enforced only (§8a's mitigations become the whole boundary) |
| Queries per turn | one **per bound KB**, parallel | **one** |
| Ceiling | 10,000 indexes per bucket | 2 billion vectors per index |

**Recommended: index per KB**, because the IAM boundary is worth more than saving one or
two parallel sub-second calls — and because the third reason an earlier draft gave,
"corpus statistics land on the right unit", is **void**: with vector retrieval there are
no corpus statistics (§8a).

**Two immutability constraints that must be decided before the first index is created**,
since neither can be changed afterwards:

- **Non-filterable metadata keys are fixed at index creation** — maximum 10 per index, and
  a key designated non-filterable can never become filterable. So `text` must be declared
  non-filterable up front (§8a).
- **Dimension, distance metric and encryption are also fixed at creation.** Changing any of
  them means creating a new index and re-populating it. This is what makes the
  embedding-model choice (§12) genuinely expensive to reverse: not just re-embedding every
  document, but rebuilding every KB index.

**The documented escalation path**, better than the hand-waved one in an earlier draft: an
S3 vector index snapshot can be **exported to Amazon OpenSearch Serverless** for high-QPS,
low-latency search. So outgrowing S3 Vectors is a migration AWS supports, not a rewrite —
which removes most of the risk from choosing the cheaper store first.

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

## 8c. Walkthrough: a document, from upload to a persona quoting it

Marked ✅ where the step exists today, ⟳ where it changes on AWS, ✚ where it is new.

### Why there are two moments, not one

The confusing part of the current design is that **extraction and ingestion are separate
events, minutes apart**. That is deliberate:

- The engine indexes a persona's documents **before turn 1**, because a document attached
  after generation starts is too late to influence the conversation.
- The browser cannot hand the server a file path, and the upload-to-a-run endpoint only
  exists once a run exists — by which point turn 1 has been generated.

So a file is turned into *text* while the operator is still filling in the form, that text
travels inside the create-run request, and *indexing* happens when the run starts.

### Stage 1 — Upload, while the form is still open  ✅

1. Operator picks `migration-plan.pdf` in Priya's documents section.
2. `POST /api/documents/extract` (multipart). The handler takes the **base name only**
   (a client filename may contain path separators), checks the extension against the
   allowlist, and checks the extractor is actually installed on this server.
3. The body is streamed to a temp file with a running byte total, so an oversized upload is
   refused *before* it is buffered (`MAX_UPLOAD_BYTES`, 10 MB).
4. `ingest_file()` → `pypdf` / `python-docx` / plain read → `normalise_text()` (de-hyphenate
   line breaks, preserve paragraph breaks, collapse the rest) → `chunk_text()`
   (paragraph-packed, ~900 chars, overlap snapped to a sentence boundary).
5. Refused here if the text exceeds `MAX_DOCUMENT_CHARS` (400 k) or is empty — the
   scanned-PDF case, reported by the operator's filename, not the temp file's.
6. Returns `{title, media_type, text, char_count, chunk_count}` and **deletes the temp
   file. Nothing is stored.**
7. The browser drops the returned text into Priya's `document_texts` draft, where the
   operator can read and edit it. That review step is the point of returning text rather
   than storing the file: PDF extraction quality varies.

⟳ **On AWS**: unchanged, except the original file is also written to
`uploads/{sub}/{kb}/{doc}` for audit ("which document did that come from"), which the
current code deliberately does not keep.

### Stage 2 — Run creation  ✅

`POST /api/runs` carries the whole setup, including
`cast[i].document_texts = [{title, text}]`. The text rides in the request body; there is
no separate upload call.

✚ **On AWS**, this is also where the KB decision is made: an ad-hoc upload becomes an
**implicit KB scoped to this run**, and a named reusable KB is referenced by id in
`knowledge_bases` instead (§8b). Both end up as the same thing downstream — a `kb_id`.

### Stage 3 — Ingest, at run start, before turn 1  ✅ ⟳

`_ingest_cast_documents()` runs once, per cast member, inline documents first (they cannot
fail on a missing path, so one bad path elsewhere in the cast cannot block a
browser-authored run).

Today:
- `ingest_text()` re-chunks the text, then `db.add_document()` writes one `documents` row
  and N `doc_chunks` rows scoped `(run_id, persona_name)`. The FTS5 index updates
  automatically, being an external-content table over `doc_chunks`.
- Emits `document.ingested` per document — `document_id`, `persona_name`, `title`,
  `media_type`, `char_count`, `chunk_count` — or `document.failed` with the reason. Never
  raises: a bad document degrades that persona's background, it does not fail the run.

⟳ On AWS this becomes the `IngestDocuments` state (§5.2), and the three writes change:

| | goes to |
|---|---|
| normalised full text, one object | S3 `docs/{sub}/{kb}/{doc}.txt` |
| each chunk: **embedding + the passage text as metadata**, tagged `owner_sub`, `kb_id`, `doc_id`, `ordinal` | S3 Vectors, index per KB |
| title, counts, media type | DynamoDB `documents` (metadata only) |

✚ The new step is **embedding**: one Bedrock embedding call per batch of chunks. Measured
cost for a 387-chunk corpus: **$0.0014**. The `document.ingested` event stays as the
operator-visible record.

### Stage 4 — Retrieval, on every turn  ✅ ⟳

This is where the earlier revisions of this document went wrong, and the shape of the query
is why.

1. `retrieve_for_turn()` builds the query from **the last 3 turns of conversation plus the
   topic** — not from a search box. That is what makes it *diluted*: conversational prose
   with the relevant terms buried in it.
2. ⟳ Today those terms become an FTS5 `OR` query. On AWS the assembled text is **embedded**
   (~$0.0000001) and used for k-NN against the KB indexes the speaker is bound to
   (`run.knowledge_bases ∪ persona.knowledge_bases`), filtered on `owner_sub` + `kb_id`.
   This is the step the 5f measurement settles: on exactly this query shape, lexical put
   the right passage first **1.7%** of the time against vector's **36.7%**.
3. Passages come back **with** the vectors — no second lookup.
4. `apply_budget()` trims to `max_chars` (default 1200), which is what keeps a 40-page
   attachment out of every prompt.
5. The passages go into that turn's prompt, and a **`document.retrieved`** event records
   the query and each passage's `chunk_id`, `document_id`, `title`, `ordinal`, `score`,
   `chars` — metadata only, no content. That event is the provenance trail the dossier
   reads and `citation_integrity` gates.

### The whole path, compressed

```
 browser        POST /api/documents/extract     → text (nothing stored)
   │            (extract · normalise · chunk · caps)
   ▼
 form          POST /api/runs { cast[].document_texts }
   │
   ▼
 IngestDocuments (once, before turn 1)
   ├─▶ S3           full normalised text
   ├─▶ S3 Vectors   embedding + passage text, tagged owner_sub/kb_id/doc_id/ordinal
   ├─▶ DynamoDB     title, counts (metadata only)
   └─▶ event        document.ingested
   ▼
 per turn:  last 3 turns + topic ──embed──▶ k-NN over bound KBs ──▶ passages
                                              │
                                              ├─▶ prompt (trimmed to max_chars)
                                              └─▶ event  document.retrieved (metadata)
```

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
7. **Avatars exceed the DynamoDB item limit.** `avatar.ready` carries the generated image
   as base64 *inside the event payload* — 2.2 MB in the one real instance here, against a
   1.2 KB mean for every other event type. On DynamoDB that is a hard write failure, so it
   is a blocker rather than a slow path. Fix it before the port (write the image to S3, put
   the key in the event): it is small, testable against the current backend, and it removes
   a migration blocker for free. See §4a.
8. **Retrieval defaults to the mode that does not work here.** `RetrievalConfig.mode`
   defaults to `fts`, and `vector` currently requires the optional `sqlite-vec` extra plus
   an embedding provider — so vector mode has never been the default path. On AWS it must
   be, per §8's measurement, which also makes the embedding provider a hard dependency
   rather than an extra.
9. **CloudFront must reproduce the SPA cache policy** — `index.html` no-cache, hashed
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
specification; run them against DynamoDB Local in CI. Retrieval moves to S3 Vectors in
the same phase (§8) — the largest single behavioural change in the port, and the one that
needs a before/after quality comparison on a real corpus rather than a green test suite.
Note this phase also switches the engine's retrieval mode from lexical to `vector`, which
the Phase 5f measurement supports but which has never run against a live multi-user
corpus.

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
   CMK is a strong isolation story for a security review and cheap at this scale. Confirmed
   available on S3 Vectors (SSE-S3 or SSE-KMS, per bucket or overridden per index) — but
   **fixed at creation**, like dimension, so it is another decide-once item. Note KMS adds a
   `kms:Decrypt` requirement to every principal that queries.
6. **Analytics.** DynamoDB serves the access patterns the UI has, but not "show me every
   run about X across the org". If that is wanted, add a DynamoDB-Streams-to-S3 path and
   query with Athena rather than distorting the operational key design.
7. **Which embedding model, and at which dimension.** A core decision, and verified to be
   the most expensive one to reverse: an S3 Vectors index's **dimension, distance metric and
   non-filterable metadata keys cannot be changed after creation**, so a different model or
   width means creating new indexes and re-populating every one. Titan Text Embeddings v2 is
   what Phase 5f measured, so its numbers are the ones that transfer, and it offers 256 /
   512 / 1024 — pick with the measurement in §8, not the default. Store the model id and
   dimension alongside each vector so a future migration can tell what needs redoing.
8. **Cost ownership.** Is Bedrock spend charged back to teams? If so, tag or attribute
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

**The two stores added since the first revision are both small.** S3 Vectors is priced as
storage plus per-query rather than provisioned capacity, which is the property that got it
chosen over OpenSearch Serverless (§8) — at a few dozen chunks per persona the storage is
negligible and the queries are one per turn. Embedding is measured, not estimated:
**$0.0014** to embed a 387-chunk corpus once, and **~$0.0000001** per per-turn query
embedding. Neither is a line item worth managing; both are worth knowing are not free.

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
