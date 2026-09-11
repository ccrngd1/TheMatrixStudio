# Phase 5 design: moving the turn loop into Step Functions

Companion to `AWS-SERVERLESS-ARCHITECTURE.md` §5.2/§6, which draw the state machine and
give the reasoning for choosing one. This is the level below: the specific places where
the drawing does not survive contact with the existing engine, and what to do instead.

Written before the implementation. Phase 4's cancellation is the argument for doing so —
that phase was planned from Lambda's *documented* limits and died on its *actual*
execution model, discovered only after deployment.

Status: **implemented, deployed and verified 2026-09-11** —
`scripts/verify_turn_loop.py`, 20 checks against the real state machine.

One thing this document got wrong, corrected below in §6: it said the stop check was
"exact at `turn_budget=1`". It was not. The engine's `should_stop` is synchronous and
closes over the run row read at the slice's *start*, so a stop arriving during a turn
was invisible and the machine looped once more — measured as two turns past the request
rather than one. Fixed with a re-read at the end of each slice.

---

## What has to become true

A run cannot execute at all today. `POST /api/runs` returns 201, logs `Starting
simulation`, and the sandbox freezes when the handler returns. So this phase is not an
optimisation of a working loop; it is the loop's first working home.

---

## 1. The cross-turn state is seven things, and six are already persisted

The turn loop (`_run_turns`, 680 lines) carries exactly this across an iteration:

| state | already persisted? | how a fresh invocation gets it |
|---|---|---|
| `agents` (memory, tokens, cost, relationships) | ✅ `SimSnapshot.agents` | snapshot at turn N |
| `conversation` | ✅ `SimSnapshot.conversation` | snapshot at turn N |
| `pending_threads` | ✅ `SimSnapshot.pending_threads` | snapshot at turn N |
| `turn` | ✅ `SimSnapshot.turn` | snapshot at turn N |
| `last_speaker` | derivable | `conversation[-1]["speaker"]` |
| `seq_counter` | ✅ the event log | `max_seq(run_id) + 1`, an O(1) read |
| **`firsthand_citations`** | ❌ **nowhere** | see §2 |

That table is the reason this phase is tractable, and it is not luck: Phase 2a chose a
full snapshot per turn over deltas, so "reconstruction is O(1) (load one row)". A turn
is already a pure function of (state at N) → (events, snapshot at N+1). That *is* a
state-machine iteration, as §6 says.

## 2. `firsthand_citations` is a latent bug that this phase would make permanent

`_run_turns` initialises it to `[]` and appends `(speaker, title)` for every first-hand
citation. It is what makes a **second-hand** attribution verifiable: "Priya cited X as
saying Y" is accepted only if Priya genuinely cited X first-hand earlier.

It is initialised to `[]` at **both** call sites — the fresh run and `resume_simulation`.
So a resumed or branched run has already forgotten who cited what, and every legitimate
second-hand credit in it is treated as unverifiable. Rare enough today to have gone
unnoticed.

**Under this phase every turn is a resume.** The ledger would be empty on every turn
after the first, permanently, and second-hand crediting would silently never work.

**Decision:** add `firsthand_citations` to `SimSnapshot` (additive, defaults `[]`, so
stored snapshots still parse) *and* reconstruct it in `reconstruct_at_turn` from the log.
Both, because they serve different paths: the snapshot is the O(1) fast path a turn
Lambda uses, and the reconstruction covers resume, branch (which copies events) and any
snapshot written before the field existed. The log is sufficient — `agent.response`
already carries `citation_provenance`.

## 3. State is reloaded, never passed through the execution payload

Tempting shape: `PrepareTurn` returns the reconstructed state, `GenerateTurn` consumes
it. §5.2 draws them as separate states, which invites exactly this.

**It cannot work.** A Step Functions state's input/output is capped at **256 KB**, and
snapshot bodies here are **mean 45 KB, max 2.2 MB** (measured over 619 real snapshots in
`PHASE2-STORAGE-KEY-DESIGN.md` §3 — one of them already exceeds DynamoDB's 400 KB item
limit). A long conversation exceeds the payload quota, so the machine would work in
testing and fail on the runs people care about.

**Decision:** the execution payload carries only identifiers and counters —
`{run_id, owner_sub, turn, max_messages, status, total_cost_usd}` — a few hundred bytes
that cannot grow with the transcript. Every turn loads its own state.

**Consequence, accepted:** `PrepareTurn` and `GenerateTurn` collapse into **one** Lambda
per turn. Keeping them separate would mean either exceeding the payload cap or loading
the same snapshot twice per turn to avoid it. §5.2's split is a drawing convenience; the
payload limit makes it actively wrong, so this is a deliberate deviation.

## 4. `_run_turns` gains a turn budget rather than being rewritten

The loop body is 680 lines with substantial closure state. Extracting one iteration into
a standalone function is a large, mechanical, silent-failure-prone change.

**Decision:** add `turn_budget` to `_run_turns`. It limits how many turns *this call*
generates and returns a **non-terminal** `status: "running"` when the budget is spent
rather than falling through to `sim.completed`. The loop, the terminal handling, the
stop check and the cost cap all stay exactly where they are.

- Local, CLI, tests: `turn_budget=None` → runs to completion. **Unchanged path.**
- A turn Lambda: `turn_budget=1` → one turn, returns `"running"`, the machine loops.

This also means the batch-vs-per-turn question becomes a **tuning knob** rather than an
architecture: `turn_budget=1` is what ships, because per-turn gives one-turn retry
granularity on the throttling §7 calls "the real operational risk", a uniform ~10 s
invocation with no risk of a 15-minute timeout mid-run, and one-turn stop latency.

## 5. Every turn trims its own dangling tail, so a retry is idempotent

`Retry` on the turn state is the whole point of using an orchestrator for Bedrock
throttling. But a turn that fails *after* appending events and *before* saving its
snapshot leaves a partial turn behind; a naive retry would then append a second copy of
turn N+1 under fresh seqs, and the event log — the source of truth `reconstruct_at_turn`
replays — would contain two versions of one turn.

**Decision:** each turn invocation calls `truncate_after_turn(last_checkpoint)` before
generating, which is precisely step 2 of `resume_run_in_place`. The mechanism is already
built and tested; this reuses it per turn instead of per resume. A retry then starts from
the same clean state as the original attempt.

## 6. The stop and cost decisions are returned by the Lambda, not read by the machine

§6 says the stop predicate "is a DynamoDB read in `CheckContinue`". Step Functions can do
that natively with an SDK integration and no Lambda.

**Rejected, on tenancy grounds.** A direct integration reads with the **state machine's**
role. Every storage access in this system goes through per-request credentials scoped to
one `owner_sub` (§3), and the whole point of `for_owner` is that no code path reads a
tenant's data with ambient authority. A native integration would be the one exception,
sitting on the hottest path, and `dynamodb:LeadingKeys` could not constrain it because
the state machine's role is not per-tenant.

**Decision:** the turn Lambda — which already holds scoped credentials and already reads
the run row for its config — returns `stop_requested`, `total_cost_usd` and `status`.
`CheckContinue` is a pure `Choice` over its own input. Same guarantee, one authority.

The stop flag itself moves from `RunManager._stop_requested` (an in-memory set) to a
`stop_requested` attribute on the run row. That is a fix, not a port: today a stop only
works if the request happens to land on the process running the turn, which under Lambda
concurrency is close to never.

Ordering is preserved: the check happens **after** the turn is emitted and checkpointed,
so a stop lets the turn in flight finish. That is `_run_turns`'s existing contract and it
does not move.

**Correction, from the deployment.** The engine's `should_stop` is a synchronous
callable, so it cannot await a DynamoDB read — it can only report what was known when
the slice started. A stop requested *during* a turn was therefore invisible: the slice
returned `running`, the machine looped, and one more turn was generated. Measured:
asking during turn 2 produced a log ending at turn 4, where the contract says turn 3.

`execute_slice` now re-reads the flag after generating and ends the run itself when it
is set. One `GetItem` per slice, which is the cheapest thing in a turn by orders of
magnitude, and it restores the semantics the local path always had — there the
predicate is a live closure over an in-memory set, so it was never stale.

The verification script's original check for this was `len(turns) >= asked_at`, a lower
bound. It passed on both the broken and the correct behaviour. The bound that mattered
was the upper one, and it is now asserted.

## 7. `POST /api/runs` writes the run row synchronously

Named in the plan already, and it is the specific failure Phase 4 observed: a client got
a 201 for a run that never existed, so there was nothing to poll and nothing to resume.

**Decision:** the route creates the run row (status `pending`), then calls
`StartExecution`, then returns. The engine no longer calls `create_run` — it flips
`pending` → `running` on the first turn. If `StartExecution` fails, the run row exists in
`pending` with the failure recorded, which is a state a user can see and retry rather
than a silent loss.

## 8. Live streaming becomes polling

The in-memory `RunBroker` fans events to WebSocket subscribers in the API process. Turns
now run in a different process, so there is nothing to fan out.

`GET /api/runs/{ref}/events?after_seq=` already exists and the frontend's `api.ts`
already calls it. The acceptance criteria say "streaming to the UI by polling", and the
`connections` table's own comment marks WebSocket fan-out as v2.

**Decision:** the frontend polls `after_seq` while a run is non-terminal. The WebSocket
route stays for local single-process use rather than being deleted, because it is the
only thing that gives sub-second latency and it still works there.

---

## 9. Branch and resume are the same machine with a different first state

§6 says branch and resume "need no new machinery" because both already
reconstruct-and-generate-forward. That is right, and the seam is precise: both
`execute_branch` and `resume_run_in_place` split cleanly at their `resume_simulation`
call. Everything before it establishes a checkpoint; everything after it generates
turns, which is what a slice does.

**Decision:** the execution payload carries a `mode` (`fresh` | `branch` | `resume`),
and the machine's first state dispatches on it. `Turn`, `CheckContinue` and `Finalise`
are shared verbatim.

Three things this exposed that were harmless in one process and are not across Lambdas:

1. **The effective budget was never stored anywhere.** `branch_budget` extends a run's
   budget when the fork or checkpoint already sits at it, and an `inject_message`
   mutation bumps it by one. Both were local variables. A slice reads the run row, so
   without persisting it a resume reads `turn >= max_messages`, finalises immediately,
   and reports `complete` having generated nothing. Now a `budget` attribute, preferred
   over `config_json` by `budget_of`.
2. **The mutation must be applied in prepare, exactly once.** `resume_simulation`
   applies it before its loop; a slice calls `resume_simulation` per turn. Leaving it
   to the engine would re-inject the same message on every turn of the branch.
3. **A resume cannot reuse the run-derived execution name.** Names are unique for 90
   days and `ExecutionAlreadyExists` is deliberately treated as success, so a resume of
   a run that already executed would start nothing and say it was fine. Varying it is
   safe for a resume because the concurrency guard there is the run's status, which is
   checked and flipped synchronously before the call.

### And one real bug the deployment found

Resuming a run whose log already ended produced **two `sim.completed` events**.
`truncate_after_turn` removes events *past* the checkpoint, and a terminal event is
emitted *at* the last turn — so it survived the trim by exactly one turn.

Nothing raised, because `reconstruct_at_turn` ignores `sim.*` and state replay was
unaffected. The consequence is in the reader: the viewer marks a run finished on the
first terminal event it sees and stops polling, so the resumed turns never appear.
Reachable without contrivance — stop a run, then resume it.

Fixed with `clear_terminal_events`, called by both the resume and the branch paths
(a fork at the parent's last turn copies the parent's marker too) and in both the
orchestrated and local implementations, so the two cannot diverge.

## Build order

1. `firsthand_citations` on the snapshot and in `reconstruct_at_turn` — independent, and
   fixes a live bug regardless of the rest.
2. `turn_budget` in `_run_turns`, with the non-terminal return.
3. `execute_run_slice()` — load state at N, run `turn_budget` turns, return the machine's
   next payload. This is the turn Lambda's whole body, testable in-process.
4. `stop_requested` on the run row, replacing the in-memory set.
5. Lambda handlers for ingest / turn / finalise.
6. The CDK state machine, with `Retry` and `Choice`.
7. `POST /api/runs` → synchronous row + `StartExecution`.
8. Frontend polling.

*Done when:* a 40+ turn run completes (impossible in one Lambda), a stop lands after the
turn in flight is persisted, and a cost cap terminates a run as `capped`.
