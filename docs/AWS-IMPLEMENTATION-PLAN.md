# Implementation plan: TheMatrix Studio on AWS

Companion to `AWS-SERVERLESS-ARCHITECTURE.md`, which is the *what* and *why*. This is the
*in what order*, and what proves each step worked.

Status: **plan**, 2026-09-09. Nothing built.

## Principles for sequencing

1. **Every phase ends in something demonstrable**, not "the layer is done". A phase whose
   only evidence is a green unit test can pass while the thing does not work.
2. **Do the local-testable work first.** Two of the hardest changes — tenancy in the domain
   model, and getting avatars out of the event log — need no AWS at all and are covered by
   the existing 709 tests. Doing them first shrinks what has to be debugged through a
   deployment.
3. **Port storage before orchestration.** A run inside one Lambda works up to ~30 turns
   (measured 6–13 s/turn), which is enough to prove tenancy and storage against real
   conditions before adding a state machine.
4. **The 709 existing tests are the specification.** They were written against behaviour,
   not SQL, so they are the contract the new backend must satisfy. Where one only passes
   because of a SQLite detail, that is a finding about the test.

### A note on "no local run"

Dropping local run was a **product** decision: the deployed thing is AWS-only, and there is
no supported laptop mode. It is not a decision to stop testing in-process. During the port
the tests need a fast backend, and the recommendation is **`moto`** (in-process AWS mocking,
no container) for the unit suite, plus **DynamoDB Local** and a real vector index for the
integration and quality suites. SQLite is deleted, not kept as a second implementation.

---

## Phase 0 — De-risking pre-work (no AWS, current backend)

Everything here is testable today with `pytest`, and each item removes a migration blocker.

**0.1 Avatars out of the event payload.** `avatar.ready` embeds the image as base64 —
2.2 MB in the one real instance, against a 1.2 KB mean for every other event type. Over
DynamoDB's 400 KB item limit, so it is a hard write failure later. Write the image to a blob
location and put the key in the event.
*Done when:* no event payload exceeds ~10 KB in a fresh run with avatars on, and the UI
still renders them.
*Effort:* small. Already logged in `BACKLOG.md`.

**0.2 Tenancy in the domain model.** The largest and most under-estimated item.
- Add `owner_sub` to runs; thread it through every query.
- Make run-name uniqueness **per user** (today `runs_name_unique ON runs(name)` is global,
  so two people could not both have `trusted-robot`), and scope the name-generation
  collision check accordingly.
- `get_run_by_ref` accepts an id *or* a name: scope name lookups to the caller and verify
  ownership on id lookups.
- Add an authorisation check to every route that takes a `ref`.
*Done when:* a test suite proves user A cannot read, branch, resume, stop, summarise,
delete-documents-from, or export the setup of user B's run — one negative test per route,
because a single missed route is the whole vulnerability.
*Effort:* the biggest item in Phase 0. Touches most of `api/app.py`.

**0.3 Make vector retrieval a first-class path.** `RetrievalConfig.mode` defaults to `fts`
and `vector` needs the optional `sqlite-vec` extra plus an embedding provider, so the mode
the AWS design depends on has never been the default. Flip the default, make the embedding
provider a hard dependency, and confirm the existing vector-mode tests still pass.
*Done when:* a run with documents retrieves via embeddings with no extra configuration.

**0.4 Decide the embedding dimension, with evidence.** Titan Text Embeddings v2 offers
256/512/1024, and an S3 Vectors index's dimension is **immutable after creation**. Re-run the
Phase 5f recall measurement at 256 and 1024 on the existing ground truth.
*Done when:* a recorded recall@1/@5/MRR comparison and a chosen dimension. If 256 holds
recall it is a 4× reduction in vector storage and query cost.
*Effort:* small — the harness, labelled queries and diluted/paraphrased arms already exist.

---

## Phase 1 — Infrastructure skeleton (CDK, nothing wired)

**CDK in Python**, matching the codebase language, so the Step Functions definition and the
Lambda packaging live in the same language as the engine.

Stand up, with no application logic:
- **Cognito** user pool + app client, Hosted UI domain, self-signup disabled, one admin-created
  test user, one group.
- **DynamoDB** tables per §4 with their GSIs.
- **S3**: one bucket for snapshots/docs/uploads/avatars with per-user prefixes; **one vector
  bucket** with one index, to validate the `s3vectors` API and IAM shape early.
- **API Gateway HTTP API** + JWT authorizer + a container-image Lambda serving a health route.
- **CloudFront + S3** for the SPA, with the two cache behaviours (`index.html` no-cache,
  `/assets/*` immutable) — the policy whose absence caused a real blank-page hang.

*Done when:* you log in through the Hosted UI, the SPA loads from CloudFront, and an
authenticated `GET /api/health` returns 200 while an unauthenticated one returns 401.

**Do this before the storage port.** It surfaces the boring blockers — container image size
(litellm is 91 MB, the environment 343 MB), IAM policy shape, the `s3vectors:GetVectors`
requirement that returns 403 the moment you filter or request metadata — while there is
nothing else to blame.

---

## Phase 2 — Storage port

Replace the SQLite implementation with DynamoDB + S3. One implementation, no abstraction.

- 53 storage methods, grouped as §4a describes: runs, events, snapshots, analysis, documents.
- Snapshot bodies to S3 with a DynamoDB pointer (measured: mean 45 KB, max 2.2 MB, 1 of 619
  already over the 400 KB item limit).
- Per-request `sts:AssumeRole` scoped with `dynamodb:LeadingKeys` (§3), so a missing tenant
  filter in application code cannot leak data.

*Done when:* the **709 existing tests pass against the new backend** under `moto`, plus the
Phase 0.2 cross-tenant negative tests, plus a test asserting the scoped role actually refuses
a cross-partition read — that last one is what proves §3 rather than assuming it.

*Risk:* the tests are the contract, and any that only passed because of a SQLite behaviour
will surface here. Treat each as a question about the test, not an obstacle.

---

## Phase 3 — Retrieval port

- Ingest: extract → chunk (unchanged) → full text to S3 → embed → `PutVectors` (batch up to
  500 per call) with `text` as **non-filterable metadata declared at index creation**.
- Query: embed the turn's query → `QueryVectors` **per bound KB index** (one call each,
  parallel — a single query cannot span indexes) → merge by distance → `apply_budget`.
- Keep the lexical arm for the operator-facing `/documents/search` only, computed in-process
  over document text from S3.

*Done when:* the **Phase 5f recall numbers are reproduced on the new stack** — not just
"search returns something". This is the phase where a green plumbing suite would happily hide
a retrieval-quality regression, so the measurement is the acceptance test.

---

## Phase 4 — Run execution, still inside one Lambda

Wire run creation to execute the whole conversation in the API Lambda's background task, as
it does today.

*Done when:* a real ~20-turn conversation completes end to end in AWS, streams to the UI by
polling, produces a summary, and the dossier shows retrieved passages.

**Why stop here before Step Functions:** this proves tenancy, storage, retrieval and Bedrock
together, with one moving part instead of five. Measured turn latency (6–13 s) means ~30
turns fits a 15-minute Lambda, so this is genuinely usable, not a throwaway.

---

## Phase 5 — Step Functions

Move the loop to a Standard workflow per §5.2: `IngestDocuments` → `PrepareTurn` →
`GenerateTurn` → `CheckContinue`.

- `stop_requested` and the cost caps become `Choice` states — restoring semantics that
  currently depend on in-memory state and so only work if the request lands on the process
  running the turn.
- Bedrock throttling and validation-gate regeneration become `Retry` with backoff.
- Branch and resume become the same machine with a different `from_turn`.

*Done when:* a 40+ turn run completes (impossible in one Lambda), a stop lands **after the
turn in flight is persisted**, and a cost cap terminates a run as `capped`.

---

## Phase 6 — Knowledge bases and sharing

First-class KBs, grants, run-level and persona-level bindings (§8b). Deferred to here
because Phases 0–5 can carry per-conversation documents as an implicit run-scoped KB, and
doing it later means designing it against a working system.

*Done when:* one document, uploaded once, is searchable by two personas in two different
conversations; a revoked grant stops working **at query time**, not just at binding time.

---

## Phase 7 — Operational polish

Per-user spend caps from Cognito groups · model invocation logging · WebSocket if polling
latency or cost justifies it · admin views · retention/TTL · the deletion path across all
three stores.

---

## Order-of-magnitude effort

Deliberately coarse, because a precise estimate here would be invented:

| Phase | Relative size | Main risk |
|---|---|---|
| 0 | medium | 0.2 touches most routes and is easy to under-scope |
| 1 | small–medium | container image size; IAM shape |
| 2 | **large** | 53 methods; tests surfacing SQLite assumptions |
| 3 | medium | quality regression hiding behind green plumbing tests |
| 4 | small | first real integration; expect Bedrock/IAM friction |
| 5 | medium | state machine plumbing, not logic |
| 6 | medium | authorisation correctness, negative cases |
| 7 | ongoing | — |

Phase 2 dominates. Phases 0 and 1 are worth doing carefully because they make Phase 2
debuggable.

## What I would build first, concretely

Phase 0.1 (avatars) and 0.4 (dimension measurement) in either order — both are small,
independently valuable, and testable today. Then 0.2 (tenancy), which is the real work and
wants a dedicated stretch.

Nothing in Phase 0 depends on an AWS account, so it can start immediately.
