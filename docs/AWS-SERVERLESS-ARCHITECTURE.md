# Running TheMatrix Studio serverless on AWS with Bedrock

Status: **proposed architecture**, 2026-09-09. Nothing here is built.

## 1. What actually constrains the design

Measured from this repository and from the 38 runs in `data/matrix_studio.db`, not
assumed:

| Constraint | Evidence | Consequence |
|---|---|---|
| A turn takes 6–13 s; a 30-turn run 3–6 min | `agent.response` event timestamps across five real runs | A whole run *just* fits a 15-min Lambda. A 40-turn run with validation retries does not. The loop cannot live in one invocation. |
| The engine is event-sourced with a per-turn snapshot, and `branching.reconstruct_at_turn()` rebuilds exact engine state from the log | `matrix_studio/branching.py:94` | A turn is already a pure function of (log up to N) → (new events, new snapshot). That is precisely a state-machine iteration. This is the single biggest reason a serverless port is tractable. |
| SQLite appears in exactly **one** file — `storage/database.py`, 53 methods, imported by 7 modules | `grep -rln aiosqlite matrix_studio/` | The storage port is invasive in volume but confined in surface. One interface, two implementations. |
| 41 references to FTS5 / `bm25()` / `vec0` | `storage/database.py` | Retrieval is the part that does *not* port cleanly. See §4. |
| litellm 91 MB; site-packages 343 MB | `du -sh .venv/.../site-packages` | Over Lambda's 250 MB unzipped limit. Container image Lambda (10 GB) or drop litellm. |
| Four pieces of in-process state: `_brokers`, `_tasks`, `_stop_requested`, `_subscribers` | `api/manager.py` | All four must be externalised. The stop flag and the live-stream fan-out are the two that carry behaviour. |

## 2. Topology

```
                    ┌─────────────────┐
   browser ────────▶│   CloudFront    │──▶ S3 (built SPA)
                    └────────┬────────┘
                             │  /api/*
                    ┌────────▼────────┐
                    │ API GW HTTP API │
                    └────────┬────────┘
                             │
                    ┌────────▼────────────────┐
                    │ Lambda: API             │  container image
                    │ FastAPI via Mangum      │  (existing app, unchanged routes)
                    └───┬────────────┬────────┘
        StartExecution  │            │  reads/writes
                        │            │
        ┌───────────────▼──┐   ┌─────▼──────────────────────────┐
        │ Step Functions   │   │ DynamoDB                       │
        │ one exec per run │   │  runs / events / snapshots-ptr  │
        │                  │   │  summaries / threads /          │
        │  ┌────────────┐  │   │  connections                    │
        │  │ PrepareTurn│  │   └─────┬───────────────────────────┘
        │  └─────┬──────┘  │         │ Streams (events)
        │  ┌─────▼──────┐  │   ┌─────▼──────────┐
        │  │GenerateTurn│──┼──▶│ Lambda: fanout │──▶ API GW WebSocket
        │  └─────┬──────┘  │   └────────────────┘
        │  ┌─────▼──────┐  │
        │  │CheckContinue│ │   ┌────────────────────────────────┐
        │  └─────┬──────┘  │   │ S3                             │
        │    loop or end   │   │  snapshots/ (large state)      │
        └──────────────────┘   │  fts/{run_id}.db (per-run BM25)│
                 │             └────────────────────────────────┘
                 ▼
          Bedrock (IAM role, no keys)
```

## 3. The turn loop: Step Functions, one execution per run

A **Standard** workflow, one execution per run, looping until the budget is reached
or the run is told to stop:

1. **IngestDocuments** (once, before turn 1) — extracts and chunks cast documents,
   builds the per-run FTS index, uploads it to S3. Must be before turn 1 because the
   engine already ingests cast documents ahead of the first turn.
2. **PrepareTurn** — `reconstruct_at_turn(N)`, select the next speaker.
3. **GenerateTurn** — one Bedrock call, the Phase 4a validation gate, append events,
   write the snapshot. Retries live here.
4. **CheckContinue** — a `Choice` state on three conditions, in this order:
   `stop_requested` → `stopped`; accumulated cost ≥ cap → `capped`; turn ≥ budget →
   `complete`; otherwise loop to 2.

Why Standard workflows rather than one long Lambda, or Express:

- **No duration ceiling.** Standard runs up to a year, so a 200-turn run, or a run
  resumed repeatedly, is not a special case.
- **The stop semantics survive unchanged.** The feature already works by polling a
  predicate *between* turns so the in-flight turn finishes and is persisted. In
  Step Functions that predicate is a DynamoDB read in `CheckContinue` — the same
  place, the same guarantee, no redesign.
- **The cost cap becomes a state**, so it is enforced by the orchestrator rather
  than by engine code that a future caller could bypass. Note the cap is currently
  enforced in `_run_turns`, shared by both entry points; the Choice state replaces it.
- **Validation-gate regeneration maps onto `Retry`** with backoff instead of a
  hand-rolled retry budget.
- **Per-turn observability for free**, which matters for a tool whose whole point is
  introspection.

Cost: Standard charges per state transition. Three states × 30 turns ≈ 90
transitions ≈ **fractions of a cent per run** — irrelevant beside Bedrock tokens.
(Express is cheaper per transition but caps at 5 minutes, which is exactly the
ceiling being escaped.)

**Resume and branch need no new machinery.** Both already reconstruct state at a turn
and generate forward; each becomes a new execution seeded with a different
`from_turn`. `resume_run_in_place` and `resume_simulation` already share `_run_turns`,
so both paths get this at once.

## 4. Documents and BM25 — the part that does not port

This is the decision that most affects fidelity, and it is a trap. The retrieval
scoring was **just fixed** (v0.6.0) so that BM25 statistics come from the run's own
slice rather than the whole database: scores had been moving when unrelated runs were
added (−0.0000 alone vs −1.8331 with a neighbour present). Any port that reintroduces
a shared corpus reintroduces that bug.

### Recommended: a per-run immutable SQLite FTS index in S3

Documents are ingested once at run start and then never change. So:

- The ingest step builds the FTS5 index for that run and uploads `fts/{run_id}.db`
  to S3.
- The retrieval path downloads it to `/tmp` (configurable to 10 GB) on cold start and
  queries it; warm invocations reuse it.

Why this and not a search service:

- **It preserves the run-scoped BM25 property exactly**, because the index physically
  contains only that run's chunks. No filter, no shard statistics to reason about.
- The scoring code needs **no change at all** — it is the same FTS5, the same
  tokenizer, the same `bm25()`.
- Cost is S3 storage plus one GET per cold start.
- `reindex_documents()` already exists for the case where documents are added after
  the run starts: rebuild and re-upload.

Trade-off: a document added mid-run means rebuilding that run's index. Given documents
are attached at setup time, that is rare and already has a code path.

### Rejected: OpenSearch Serverless

Native BM25 and it would also replace `sqlite-vec` for vector mode. But **it
reintroduces the bug just fixed**: OpenSearch computes IDF per shard across the whole
index, and a `run_id` filter does not scope corpus statistics. Avoiding that needs one
index per run, which hits collection limits. On top of that its floor cost (an OCU
minimum billed hourly, whether or not anything is querying) is heavy for a tool that
may see a handful of runs a day. Revisit only if this becomes multi-tenant at volume.

### Rejected: Aurora Serverless v2 Postgres

Postgres full-text ranking (`ts_rank`, `ts_rank_cd`) is **not BM25** — it does not use
corpus IDF the same way. Retrieval quality would change and every existing measurement
in `docs/` would become incomparable. That is a large, silent behavioural change to
buy nothing this application needs.

## 5. Storage: DynamoDB

The event-sourced model maps onto DynamoDB almost too neatly.

| Table | PK | SK | Notes |
|---|---|---|---|
| `runs` | `run_id` | — | Plus `stop_requested`. GSI on `name` (`get_run_by_ref` accepts id *or* name), GSI on `created_at` for the list view. |
| `events` | `run_id` | `seq` | Append-only. `get_events_after(seq)` is a Query with `SK > seq` — the existing access pattern, natively. |
| `snapshots` | `run_id` | `turn` | **See the size warning below.** |
| `summaries` | `run_id` | `kind` | |
| `threads` / `thread_messages` | `run_id` / `thread_id` | | |
| `connections` | `run_id` | `connection_id` | WebSocket fan-out only. |

**Snapshot size is a real risk.** A snapshot carries the full transcript plus every
agent's state; with cognition on that includes memory streams. DynamoDB's item limit
is 400 KB, and a 40-turn run with memories will exceed it. So snapshots go to S3 with
a DynamoDB pointer, not inline. This must be decided up front — discovering it at turn
30 of a real run is how you lose a run.

**Cost accounting stays exact.** Costs are already accumulated per agent from
LiteLLM-reported values and summed; nothing about that changes.

## 6. Live streaming: poll first, WebSocket later

The current implementation is an in-process broker with an asyncio queue per
subscriber — it cannot survive Lambda.

**v1: poll.** The REST client *already* supports `getEvents(ref, afterSeq)`. Polling
every 2 s removes an entire subsystem from the first deployment and costs almost
nothing at this volume. A 2 s lag on a 6–13 s turn is barely perceptible.

**v2: API Gateway WebSocket**, if the lag annoys:
- `$connect` → write `connection_id` into the `connections` table keyed by run
- DynamoDB Streams on `events` → Lambda → `PostToConnection` per subscriber
- `$disconnect` → delete

The existing replay-then-tail logic carries over: on connect the client sends
`after_seq`, the Lambda posts the backlog, and the stream tail takes it from there.

## 7. Frontend

S3 + CloudFront, with **two cache behaviours that must be right**: `index.html`
`no-cache, must-revalidate` and `/assets/*` `immutable`. This is not a detail — the
exact absence of it caused a blank-page hang fixed in this release (a cached shell
pointing at deleted asset hashes). CloudFront must reproduce the policy that
`_ImmutableStatic` currently enforces in-process.

## 8. Bedrock and credentials

The Lambda execution role gets `bedrock:InvokeModel` scoped to specific model ARNs.
This is **strictly better than today**: no bearer token, no `.env`, no credential
anywhere in the system, which satisfies the project's "keys stay server-side" rule
absolutely rather than by convention. LiteLLM already honours the boto3 credential
chain, so the LLM seam needs no code change.

**Packaging:** use a **container image Lambda**. litellm alone is 91 MB and the full
environment 343 MB, over the 250 MB unzipped limit. The alternative — calling
`bedrock-runtime.Converse` via boto3 and dropping litellm — is smaller but abandons
provider-agnosticism, which is a stated project value (`PROJECT-SPEC.md` §7) and the
reason the same code runs against Ollama locally. Container image keeps both.

## 9. Guardrails on spend

Given the whole reason the stop button exists:

- **Reserved concurrency** on `GenerateTurn` caps how much Bedrock can be driven in
  parallel, whatever the API is asked to do.
- **The cost cap as a Choice state**, enforced by the orchestrator.
- **`stop_requested` in DynamoDB**, so a stop works across processes — which the
  current in-memory set cannot do.
- **AWS Budgets** with an alarm, as the backstop the application cannot bypass.

## 10. Migration path

Ordered so each phase is independently useful and testable.

**Phase 1 — storage interface (the big one).** Extract the 53 methods of `Database`
into a protocol; add a DynamoDB implementation alongside the SQLite one. The bar: the
existing **709 Python tests pass against both backends**. Keep SQLite as the local-dev
and test backend permanently — it is faster, hermetic, and it is how the retrieval
scoring is specified.

**Phase 2 — containerise and deploy, loop still in one Lambda.** API Lambda + DynamoDB
+ S3/CloudFront + polling. Runs up to ~30 turns work. Usable end to end, and it proves
the storage port under real conditions before adding an orchestrator.

**Phase 3 — move the loop to Step Functions.** Removes the duration ceiling and
restores the stop and cost-cap semantics as orchestrator states. Branch and resume
become seeded executions.

**Phase 4 — WebSocket**, only if polling proves annoying.

Deliberately out of scope for v1: vector/hybrid retrieval (this project's own
measurements found lexical adequate, and vector mode needs an embedding call per
chunk), and avatars (optional already).

## 11. Open decisions

1. **Who can use it?** There is no authentication today. Public on the internet with
   Bedrock behind it is a spend risk, not just a data one. Cognito, or IAM-signed
   requests, or CloudFront + WAF with an allowlist?
2. **One AWS account or per-environment?** Affects whether run data ever mixes.
3. **Is multi-user a requirement?** If runs need to be per-user, `runs` needs an owner
   attribute and every query needs scoping — much cheaper to decide now than later.
4. **Retention.** Events and snapshots grow without bound. TTL on DynamoDB, lifecycle
   on S3?
5. **Region.** Bedrock model availability differs by region, and the avatar model
   (`stability.sd3-5-large-v1:0`) is already pinned to `us-west-2` separately from the
   text model. That split has to survive the port.
6. **Does local SQLite stay supported?** Recommended yes: it keeps the tests fast and
   keeps the quickstart honest. It does mean maintaining two backends.

## 12. Rough cost shape

At low volume the infrastructure is close to free and **Bedrock dominates** — the runs
measured here cost $0.05–0.06 for 16–30 turns. Lambda, DynamoDB on-demand, Step
Functions transitions, API Gateway and CloudFront at this volume are single-digit
dollars a month combined. The one line item that would change that picture by orders
of magnitude is a managed search service with an hourly capacity floor, which is the
practical argument for §4's per-run S3 index on top of the correctness one.

Verify current per-unit prices before committing; the numbers above are shapes, not
quotes.
