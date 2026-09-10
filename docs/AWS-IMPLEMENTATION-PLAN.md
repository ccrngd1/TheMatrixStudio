# Implementation plan: TheMatrix Studio on AWS

Companion to `AWS-SERVERLESS-ARCHITECTURE.md`, which is the *what* and *why*. This is the
*in what order*, and what proves each step worked.

Status: **in progress**, updated 2026-09-10. **Phase 0 complete; Phase 1 written and
verified locally, not yet deployed** — see Progress at the end.

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

**0.2 Tenancy in the domain model.** ✅ **DONE 2026-09-10.** The largest item, as
predicted — 28 routes and every read in the storage layer.
- `owner_sub` on runs, threaded through every query. Required and **keyword-only** on
  each read, so a caller that forgets it raises `TypeError` rather than serving another
  tenant's data; defaulted only on `create_run`, where a forgotten owner fails closed.
- Run-name uniqueness is now **per user**. The migration has to DROP the old global
  index, not merely add the new one — `CREATE INDEX IF NOT EXISTS` is satisfied either
  way, and a surviving global index would go on rejecting cross-user duplicates while
  every "the new index exists" assertion passed.
- `get_run_by_ref(ref, *, owner_sub)` is the single authorisation choke point for all 23
  `ref`-taking routes. "Not yours" and "not found" are the same 404: run refs can be
  memorable names from a small generated vocabulary, so a 403 would be a guessable
  oracle. The two `{thread_id}` routes resolve their run through the same path, since a
  thread id does not authorise anything by itself.
- `AUTH_MODE` decides where the identity comes from (`single-user` | `jwt`), and
  verified claims win in **both** modes so an authorizer added later takes effect even
  if the setting is forgotten. Authentication itself is Phase 1.

*Done:* 47 tests. The central one is **table-driven over every route** that takes a
`ref` or `thread_id`, paired with a guard that walks the app's own route table and fails
if a route has no case — a hand-written list is complete the day it is written and
silently incomplete after the next route is added. Each case asserts B gets 404 **and A
gets non-404**, because otherwise a path typo would 404 for both and pass while proving
nothing. Mutation-tested: removing the owner filter from `get_run_by_ref` fails 31 of
47; the AND/OR precedence slip in the search filter fails its own test; keeping the old
global index fails two.

**0.3 Make vector retrieval a first-class path.** ⏩ **Moved to Phase 3.** Flipping
`RetrievalConfig.mode` today would change the behaviour of a working product for a
benefit that only materialises after the port — and Phase 3 deletes FTS anyway, so the
default becomes moot rather than needing to be flipped twice.

Investigating it was still worth it: it surfaced a live bug. `mode="vector"` returned
**zero passages** both when `sqlite-vec` was absent and when chunks were never embedded,
while `mode="fts"` over the same corpus returned matches. The lexical fallback that was
supposed to prevent exactly this was nested inside the `vec_available` guard, so it was
unreachable in both cases. Fixed, with the fallback now logging which cause fired. Had
0.3 been done as written, flipping the default would have exposed every install without
the optional extra to it.

**0.4 Decide the embedding dimension, with evidence.** ✅ **DONE 2026-09-10 — 1024.**
See `docs/EMBEDDING-DIMENSION-MEASUREMENT.md`. Width is irrelevant for natural queries and
decisive for paraphrased ones (256 → 1024 is +0.117 recall@1, 15 of 128 queries, monotonic
across four metrics), so the hoped-for 4× saving would have been paid for in paraphrase
robustness. Two instrument defects were found and fixed on the way: query generation had been
failing on *every* sample because of a hardcoded `temperature=0` the configured model rejects
— reported as a table of zeros rather than an error — and comparisons were silently
incomparable because generation is non-deterministic, now fixed with a reusable query cache.

*Why it had to be decided first:* an S3 Vectors index's dimension is immutable after
creation, so a later change means rebuilding every index.

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

### Status: written and verified locally, awaiting a deploy

`infra/` holds the CDK app (Python), and `infra/README.md` is the runbook. What has
actually been verified, as distinct from written:

- ✅ **`cdk synth` succeeds** — real CLI, no credentials needed. 23 resource types.
- ✅ **39 template assertions pass** (`infra/tests/`), each covering a property whose
  absence is a *working deployment with a real defect*. Four mutation-tested: enabling
  self-signup, dropping the default authorizer, caching `index.html`, leaving passage
  text filterable — all four caught.
- ✅ **The Lambda image builds**, from the exact context CDK stages: **791 MB**. Under
  the 10 GB container limit, ~3× over the 250 MB zip limit. The container decision is
  now measured rather than inferred.
- ✅ **The container answers correctly under the Lambda Runtime Interface Emulator**:
  `/api/health` 200, `/api/runs` 200, and `/api/runs` **401 with no authorizer context**
  — so `AUTH_MODE=jwt` is real defence in depth behind the gateway, not just a setting.
- ⬜ **Not verified:** anything requiring an account — the Hosted UI login, CloudFront
  serving the SPA, the gateway's own 401, `s3vectors` IAM. Credentials were expired.

**Three defects this phase surfaced, which is what it is for:**

1. **`Mangum(lifespan="off")` broke every storage route.** Reasoning that the startup
   sweep should not fire per cold start, the first version disabled the lifespan — which
   is also where `db.connect()` happens. Result: `AttributeError: 'NoneType' object has
   no attribute 'execute'` and a bare 500 on every route touching storage, while
   `/api/health` passed throughout. **The phase's own acceptance check would not have
   caught it.** Found by invoking the image through the RIE. Fixed by running the
   lifespan and gating the sweep on a new `STARTUP_SWEEP` setting.
2. **The sweep is a correctness problem under concurrency, not a performance one.** Its
   premise is "this is the only process"; with several Lambda sandboxes, two cold starts
   each conclude the other's in-flight run was orphaned and mark it `interrupted` — one
   request terminating another user's live conversation. Observed directly: two cold
   starts logged the sweep decision in a single RIE session. Now off on Lambda, and
   Phase 7 owns the replacement.
3. **`microdnf install gcc` cannot be undone.** Adding a compiler "in case" failed the
   build, because the base image's `annobin-plugin-gcc` depends on gcc — so the real
   choice is "ship gcc permanently", not "add it cheaply". It turned out to be
   unnecessary: every dependency, including `tokenizers` and `pydantic-core`, has a
   manylinux wheel. A future dependency that does need to compile belongs in a separate
   builder stage.

Plus one incidental: `8bit-agents-activation-demo-main/` (125 MB, git-ignored, unrelated
to this project) was in `.dockerignore`'s blind spot, so every Docker build had been
copying it. With that and a CDK-level `exclude`, the staged build context went
**132 MB → 1.7 MB**.

---

## Phase 2 — Storage port

Replace the SQLite implementation with DynamoDB + S3. One implementation, no abstraction.

- 53 storage methods, grouped as §4a describes: runs, events, snapshots, analysis, documents.
- Snapshot bodies to S3 with a DynamoDB pointer (measured: mean 45 KB, max 2.2 MB, 1 of 619
  already over the 400 KB item limit).
- Per-request `sts:AssumeRole` scoped with `dynamodb:LeadingKeys` (§3), so a missing tenant
  filter in application code cannot leak data.

*Done when:* the **788 existing tests pass against the new backend** under `moto`, plus the
Phase 0.2 cross-tenant negative tests, plus a test asserting the scoped role actually refuses
a cross-partition read — that last one is what proves §3 rather than assuming it.

*Risk:* the tests are the contract, and any that only passed because of a SQLite behaviour
will surface here. Treat each as a question about the test, not an obstacle.

### Key design settled first — `docs/PHASE2-STORAGE-KEY-DESIGN.md`

The port turned out to have six places where the existing methods rely on something
SQLite makes free. Each is a decision that, made wrong, is found *after* the port is
written and costs a rewrite of the key schema — and two of them are silent. Recorded
before implementation rather than discovered during it:

1. **Numeric sort-key components must be zero-padded.** §4's literal
   `RUN#{run_id}#{seq}` sorts lexicographically, so `…#10 < …#9` and
   `get_events_after` — the most-used access pattern in the app — would silently skip
   and mis-order events. The event log is what `reconstruct_at_turn` replays, so
   mis-ordering it corrupts branch and resume rather than looking odd.
2. **`get_events` filters by turn, which is not in the sort key.** Query the run
   prefix and filter the attribute; no second index, because a GSI keyed on turn would
   add write cost to the hottest write path to save a filter over ~80 items.
3. **`list_snapshots` reads `status` out of the snapshot body.** With bodies in S3
   that is one GET per checkpoint to render a list of turn numbers. Denormalised onto
   the pointer, with the same "S3 is authoritative if they disagree" caveat §4a already
   applies to documents.
4. **`AUTOINCREMENT` has no equivalent, and the ids are load-bearing** — returned to
   callers and used as the ordering key for thread messages. Solved with an atomic
   `UpdateItem … ADD :1` counter, which *is* an autoincrement and is correct under
   concurrency. Deliberately not used for `events`, whose `seq` the engine already
   assigns.
5. **Two lookups address an item by id with no run or user context** (`get_thread`,
   `document_text`/`delete_document`). Needs a GSI on each of `threads` and
   `documents` — **absent from the Phase 1 stack**, and found by designing the port
   rather than by running it. A `Scan` is not the fallback: it reads across tenants, so
   a tenancy slip would become a full-table disclosure. ✅ **Added and deployed.**
6. **`owner_sub` has to reach the user-partitioned tables and most signatures lack
   it.** Resolving it internally from `run_id` was tempting and is wrong: the lookup
   item sits outside `USER#{sub}`, so the scoped credentials could not read it, which
   reintroduces exactly the ambient authority §3 exists to remove. So it becomes a
   **required keyword-only argument** on ~25 methods — the Phase 0.2 rule, for the
   Phase 0.2 reason. The callers already hold the value.

*Verified premise:* `asyncio.to_thread` + plain boto3 is intercepted by `moto`,
including concurrent writes and sort-key range reads — so the storage layer keeps its
`async def` signatures with no async AWS client and no event-loop blocking.

*Build order:* `storage/dynamo.py` complete → tested against `moto` → swap
`storage/__init__.py` last. Nothing is broken until the swap, and the swap plus the 41
construction sites is one mechanical step rather than a partially migrated tree.

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

## Phase 4 — ~~Run execution, still inside one Lambda~~ **CANCELLED**

**This phase cannot work, and that was measured on the Phase 1 deployment rather than
reasoned out.** It is left here rather than deleted because the mistake is instructive.

The plan was: "wire run creation to execute the whole conversation in the API Lambda's
background task, as it does today", justified by "measured turn latency (6–13 s) means ~30
turns fits a 15-minute Lambda". Both halves were wrong about Lambda's execution model.

**What actually happens.** `POST /api/runs` on the deployed Phase 1 stack returns **201
with a real LLM-generated codename** — so the synchronous part of `manager.create_run`
works, Bedrock works, the token works. Then the run vanishes. Never appears in
`GET /api/runs`, and `GET /api/runs/{name}` is 404. The CloudWatch logs show exactly one
line from the engine:

```
[INFO] Starting simulation 99e8458f-…: Whether serverless is worth the cold starts
Billed Duration: 8 ms
```

`"Starting simulation"` is the first statement in `run_simulation`, logged *before*
`db.create_run`. Nothing follows it, and the invocation billed **8 ms**.

**Why.** Lambda **freezes the execution environment when the handler returns.** An
`asyncio.create_task` background task is not a thread that outlives the response — it is a
coroutine on an event loop that stops being scheduled. The 15-minute figure is the limit on
a *single invocation*, and this invocation ends in milliseconds because the API responded.
The run row is never written, so there is not even a broken run to resume: the work is lost
with no trace beyond one log line.

The obvious repair does not exist either. Not returning until the run finishes would make
the invocation long enough — but **API Gateway's integration timeout is a hard 30 seconds**,
and a 20-turn run at 6–13 s/turn needs minutes.

**Consequence for the plan: Step Functions is a prerequisite, not an optimisation.** Phase 5
was framed as "move the loop to a state machine" for runs longer than one Lambda allows. It
is actually the only way a run executes at all. Phase 5 absorbs this phase's acceptance
criteria (a real conversation end to end, polled by the UI, producing a summary and a
dossier).

*What is salvaged:* the intent — "prove tenancy, storage, retrieval and Bedrock together
before adding orchestration" — is still right, and Phase 3's retrieval measurement already
does most of it without needing a run to execute.

---

## Phase 5 — Step Functions  *(now the first phase in which a run can execute)*

Move the loop to a Standard workflow per §5.2: `IngestDocuments` → `PrepareTurn` →
`GenerateTurn` → `CheckContinue`.

Promoted from "the phase that lifts the turn ceiling" to **the phase that makes run
execution possible at all** — see the cancelled Phase 4 above for the measurement. It
therefore also inherits Phase 4's acceptance criteria: a real conversation completing end to
end, streaming to the UI by polling, producing a summary, and a dossier showing retrieved
passages.

One consequence worth planning for now: `POST /api/runs` must become
`StartExecution` + return, and the run row has to be written **synchronously in the request**
rather than by the engine. Otherwise a client that gets a 201 still has nothing to poll —
which is precisely the failure observed above.

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
| 0 | ~~medium~~ **done** | 0.2 was indeed the big one: 28 routes |
| 1 | small–medium | container image size; IAM shape |
| 2 | **large** | 53 methods; tests surfacing SQLite assumptions |
| 3 | medium | quality regression hiding behind green plumbing tests |
| 4 | small | first real integration; expect Bedrock/IAM friction |
| 5 | medium | state machine plumbing, not logic |
| 6 | medium | authorisation correctness, negative cases |
| 7 | ongoing | — |

Phase 2 dominates. Phases 0 and 1 are worth doing carefully because they make Phase 2
debuggable.

## Progress

- ✅ **0.1 avatars out of the event log** (2026-09-10). Event payload 2,000,012 B → 117 B and
  snapshots ~1,958× smaller on a realistic image; also fixed the snapshot blocker, which the
  plan had listed separately.
- ✅ **0.4 embedding dimension** (2026-09-10). 1024, measured. Plus two measurement-harness
  defects fixed.
- ✅ **0.2 tenancy in the domain model** (2026-09-10). 28 routes scoped, per-user name
  uniqueness, 47 tests including a route-table completeness guard. The real 38-run
  database migrated in place with every run still visible.
- ⏩ **0.3 vector retrieval as the default** — moved to Phase 3. The investigation found
  and fixed a live silent-degradation bug in vector mode, which was the valuable part.

**Phase 0 is complete.** Nothing in it depended on an AWS account, which is why it went
first: the two hardest changes (tenancy, and avatars out of the event log) are now done
and debugged against 786 fast local tests rather than through a deployment.

- ✅ **Phase 1 CDK skeleton** (2026-09-10) — *written and verified locally.* `cdk synth`
  succeeds, 39 template assertions pass, the 791 MB Lambda image builds from the staged
  context and answers correctly under the Runtime Interface Emulator. Three defects
  surfaced and fixed, one of which (`lifespan="off"`) broke every storage route while
  leaving the phase's own acceptance check passing.
- ✅ **Phase 1 DEPLOYED** (2026-09-10) to account 791580863750, us-east-1. All four
  acceptance checks pass: Hosted UI login through the real authorization-code+PKCE flow, the
  SPA served from CloudFront (deep links included), unauthenticated `/api/health` → **401**
  from API Gateway before Lambda, authenticated → **200**. Live Bedrock call from the Lambda
  confirmed (`name_source: "llm"`).
  Two further defects found *only by deploying*:
  - **The Bedrock IAM policy could not authorise the default model.** A *global* inference
    profile makes Bedrock evaluate a **region-less** foundation-model ARN
    (`arn:aws:bedrock:::foundation-model/…`), which region-pinned ARNs cannot match. Every
    model call returned AccessDenied while `/api/health` returned 200 and the stack looked
    healthy. Fixed with a wildcarded region (an IAM `*` matches zero characters).
  - **CloudFront sent no `Cache-Control` header at all.** A cache *policy* governs the edge
    and says nothing to the browser, so `index.html` fell to heuristic caching — the same
    stale-`index.html` blank page, relocated to the client where an invalidation cannot
    reach it. Fixed with response headers policies per behaviour.
  - **Phase 4 is cancelled** as a result of a third finding; see that section.

**Next: Phase 2**, the storage port — the largest phase in the plan (53 methods) and the one
that makes the deployment more than a login screen. Phase 1 leaves the Lambda on SQLite in
`/tmp`, which is per-sandbox and ephemeral by design: that is the phase boundary, not a bug,
and `infra/README.md` says so where a demo operator will see it.

**Revised remaining order:** 2 (storage) → 3 (retrieval) → ~~4~~ → 5 (Step Functions, now
mandatory for a run to execute) → 6 (knowledge bases) → 7 (operations).

### What deploying taught, in one line

Every one of the three deployment defects — Bedrock IAM, CloudFront cache headers, frozen
background tasks — **passed `/api/health`**. A phase whose acceptance criterion is a health
check will sign off a stack that cannot invoke a model, caches its own index page, and
silently drops every run. Worth remembering when writing the "done when" for Phase 2.
