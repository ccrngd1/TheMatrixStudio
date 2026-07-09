# Requirements — TheMatrix Simulation Studio, Phase 2a

**Project:** TheMatrix Simulation Studio (working title)
**Phase:** 2a — Event-sourced foundation (per-turn checkpointing + branch primitive + checkpoint scrubber)
**Owner:** CC
**Author:** Main (CABAL)
**Date:** 2026-07-09
**Status:** DRAFT — awaiting CC sign-off. Do NOT hand off to MasterControl until CC approves.
**Spec:** `docs/PROJECT-SPEC.md` (§3.6/§3.7 branching, §4 event-sourcing keystone, §6 Phase 2a, §6a)
**Builds on:** Phase 0/1/1.5 (COMPLETE, verified live 2026-07-09; HEAD 27c040f). Engine, SQLite event store, FastAPI+WS, React UI, named runs, replay, summaries+asides all working.

---

## Plain-English Summary

Phase 2a lays the **foundation** for interventions/branching without yet exposing any user-facing
intervention. Two capabilities:

1. **Per-turn checkpointing.** Today the engine saves ONE snapshot at completion. Phase 2a saves a
   **full serializable snapshot after every turn** so the exact simulation state at any turn N can be
   reconstructed. (`SimSnapshot` already models the complete state — agents with memory/goals/
   relationships + conversation; `snapshots` already keys on `UNIQUE(run_id, turn)`. This is mostly
   an additive save-call inside the turn loop, not a schema change.)
2. **Branch primitive.** Given a run and a turn N, create a **new run** that copies history up to
   turn N and **resumes generating forward** from that checkpoint as its own timeline. The original
   run is never modified or re-run. Uses the existing `parent_run_id` / `branch_turn` columns.

Plus a **checkpoint scrubber** in the UI: scrub a completed run to any turn and view the exact state
at that point (read-only), and a raw **"branch from here"** action that forks a new run resuming from
that turn with **no mutation yet** (mutations/interventions are Phase 2b).

**What Phase 2a is NOT:** no user-facing interventions (inject message, promote aside, edit goal,
add/remove persona) — those are Phase 2b (full set, per CC). No rich per-agent introspection events —
that's Phase 2c. 2a delivers the plumbing: checkpoint-per-turn, fork-and-resume, and the scrubber.

**Why this phase now:** branching is the keystone the whole Phase 2 interaction model rests on. Get
the event-sourced foundation right and reviewable in isolation before layering the (larger, full)
intervention set on top in 2b. Same UI contract; a branch is just another run, so the existing
WS/replay/history machinery already displays it.

---

## Goal

A standalone additive capability set on the existing engine + API + UI that:
- persists a **full snapshot per turn** during a run (running snapshots + the existing completion snapshot),
- can **reconstruct exact state at any turn** and **replay** a completed run turn-by-turn (scrubber),
- can **branch** a run at turn N into a new run that **resumes generation forward** (no mutation in 2a),
- records the branch relationship (`parent_run_id`, `branch_turn`) and lists/threads it in history,
- keeps Phase 0 run-to-completion, Phase 1 live-watch, and Phase 1.5 summary/asides **unchanged**,
- adds **zero** OpenClaw coupling and preserves the honesty gate (no fabricated state/metrics).

---

## In Scope (Phase 2a)

### 1. Per-turn checkpointing (engine, additive)
- After each turn's `agent.response` (and after the cold-start opener), the engine persists a **full
  `SimSnapshot`** for that turn with `status="running"`, `turn=<n>`, current `agents` + `conversation`.
  Reuse `db.save_snapshot` (already `INSERT OR REPLACE` on `UNIQUE(run_id, turn)`).
- The existing **completion snapshot** (turn = final, `status="complete"`) is retained unchanged.
- **Must not change** Phase 0 semantics, the JSON in/out contract, the event schema, or observable
  run behavior beyond the extra snapshot rows. All existing tests must pass. Live-watch (Phase 1) and
  summary/asides (Phase 1.5) are unaffected (they read the completion snapshot / transcript).
- Emit an optional `checkpoint.saved` event `{turn}` (additive) so a live client *could* show
  checkpoint availability — but this must be additive and not required by any existing consumer.
- **Storage note:** full-snapshot-per-turn (CC-approved default; runs are short, ≤~a few dozen turns).
  Delta-encoding is explicitly deferred — revisit only if long runs make storage a problem. Document
  the choice in code so the tradeoff is legible.

### 2. State reconstruction + replay API
- `GET /api/runs/{ref}/snapshots` — list available checkpoint turns for a run (turn numbers + status).
- `GET /api/runs/{ref}/snapshots/{turn}` — the full `SimSnapshot` at turn N (agents + conversation as
  of that turn), read-only. 404 if that turn has no checkpoint.
- Existing `GET /api/runs/{ref}/events?after_seq=` already supports turn-by-turn replay; 2a adds the
  point-in-time **state** view to complement the event replay.

### 3. Branch primitive (engine + service + API)
- `POST /api/runs/{ref}/branch` — body `{from_turn: int, name?: string, description?: string}`.
  Creates a **new run** with `parent_run_id = <ref run id>`, `branch_turn = from_turn`, a fresh
  `run_id`, and (if omitted) an auto-generated topical codename (reuse the Phase 1 naming module).
- **Semantics (per spec §3.7 / determinism note):**
  1. Load the parent's checkpoint at `from_turn` (reconstruct `agents` + `conversation`).
  2. Copy the parent's event log **up to and including** `from_turn` into the new run (so the branch
     replays identically up to the fork), and seed a per-turn snapshot at `from_turn` for the new run.
  3. **Resume generation forward** on the new run from `from_turn + 1` using the standard turn loop,
     until its own turn budget/completion. Emits the normal event stream + per-turn checkpoints under
     the new `run_id` (so live-watch and replay work for the branch with no new machinery).
  4. The **parent run is never modified and never re-run.** Non-determinism is expected and fine — we
     only ever generate forward from the fork with (in 2a) no change applied.
- The branch runs as a **background task** (like `POST /api/runs`), returns the new `run_id`+codename
  immediately, and is watchable over the existing `WS /api/runs/{ref}/stream`.
- Engine change is **additive**: a `resume_simulation(...)` entry (or a `start_turn`/seed-state param
  on `run_simulation`) that begins from a provided state + turn instead of fresh cast init. The
  fresh-start path (Phase 0) stays byte-for-byte behaviorally identical.
- **2a applies NO mutation** at the fork (that is Phase 2b). The branch is a clean "continue forward
  from turn N" fork. This keeps 2a's surface small and reviewable.

### 4. Checkpoint scrubber + branch UI (frontend, read-only + branch action)
- **Scrubber:** on a completed (or branched) run, a turn slider/scrubber lets the user move to any
  turn N and see the cast board + conversation feed **as of that turn** (drive from `snapshots/{turn}`
  and/or event replay). Read-only; reuses the Phase 1 replay view.
- **"Branch from here" action** at the current scrubber position → calls `POST .../branch` with
  `from_turn` → navigates to the new run's live view (it generates forward). Clearly labeled that the
  original is preserved.
- **Branch lineage in history:** the history list / run view shows parent↔branch relationships
  (e.g. "branched from `<parent>` at turn N", and a parent shows its branches). A simple lineage
  list is sufficient for 2a; the full **branch-tree visualization is Phase 2b**.
- The Phase 1.5 disabled "bring into conversation" affordance stays disabled (it becomes live in 2b).

### 5. Named branches
- Branches get a memorable codename like any run (reuse Phase 1 naming). Default description notes the
  lineage, e.g. "branch of `<parent>` @ turn N". Editable, same as run creation.

---

## Out of Scope (defer)

**Phase 2b (interventions + Contribute mode — FULL set, CC: do not reduce):** promote-aside-to-room,
continue/restart the discussion, inject a message, edit a goal, add/remove a persona — all as
branch-with-mutation operations — plus the branch-tree visualization and activating the "bring into
conversation" affordance. 2a builds the fork+resume plumbing these sit on, but applies NO mutation.

**Phase 2c (introspectable engine + rich dossier):** structured per-agent state events (memory
stream, reflections/beliefs, goal updates, relationships), rich dossier UI, "why did it say that?"
trace. 2a persists whatever state the engine already tracks; it does not add new introspection events.

**Phase 3 (release polish):** BYO-key UX, spend caps, docs site, example gallery.

**Also cut (project spec §9):** spatial map/sprites; OpenClaw dependency; synchronous/UI-blocking
stepping; **re-running/reproducing an existing branch** (we only ever generate forward from a fork).

---

## Acceptance Criteria (how we verify "done")

- [ ] Phase 0 CLI (`run`), Phase 1 live-watch, and Phase 1.5 summary/asides behavior are unchanged (regression); all existing tests pass.
- [ ] A run persists a full `SimSnapshot` per turn (running snapshots at turns 1..N-1 plus the completion snapshot at N); verify snapshot count == turn count for a fresh run.
- [ ] `GET /api/runs/{ref}/snapshots` lists checkpoint turns; `GET /api/runs/{ref}/snapshots/{turn}` returns the exact reconstructed state (agents + conversation) at turn N; unknown turn → 404.
- [ ] `POST /api/runs/{ref}/branch {from_turn}` creates a new run with `parent_run_id`/`branch_turn` set, returns run_id + codename immediately (non-blocking), and the branch generates forward to completion as its own timeline.
- [ ] The branch's event log replays identically to the parent up to `from_turn`, then diverges (forward-generated). The **parent run is byte-for-byte unchanged** after branching (verify parent events/snapshots/cost identical before vs after).
- [ ] The branch is live-watchable over the existing WS stream and appears in history with its lineage; a completed branch replays like any run.
- [ ] Checkpoint scrubber: moving to turn N shows the cast board + feed as of turn N (read-only); "branch from here" forks a new run resuming from N and navigates to it.
- [ ] Branches get a topical codename (reuse Phase 1 naming; fallback never blocks); lineage shown in history/run view.
- [ ] Live end-to-end against real Bedrock: branch the imported `bridge-kibble` run at a mid-turn and confirm it resumes forward with new turns; state live-vs-mocked; do NOT fabricate cost/latency.
- [ ] `grep -ri openclaw matrix_studio/ frontend/src` returns nothing; no new OpenClaw coupling.
- [ ] `docker build .` still succeeds (or NOT-VERIFIABLE-HERE stated honestly if Docker absent).
- [ ] **Frontend static bundle rebuilt** (`npm run build`) so `serve` reflects the scrubber/branch UI — the report must confirm the served bundle contains the new UI, not just that `tsc` passed. (Lesson from the Phase 1.5 update: committing .tsx is not enough; the generated bundle must be rebuilt.)

---

## Constraints & Notes

- **Additive only on the engine.** Per-turn checkpointing is an extra save call; `resume_simulation` is a new entry that seeds state. The fresh-start Phase 0 path must remain behaviorally identical. No event-schema changes beyond the additive `checkpoint.saved` event.
- **Immutability is the core invariant.** Branching never mutates or re-runs the parent. A branch is always a new run. Verify parent is unchanged after a branch in a test.
- **Non-determinism is expected** (spec determinism note): resuming from a checkpoint will not reproduce the parent's future, and that is correct — we never re-run the original.
- **Honesty gate (carried):** no fabricated state/metrics; snapshots reflect real engine state; state live-vs-mocked; if Docker unavailable, say so.
- **Keys server-side** (.env, git-ignored). Model default `bedrock/global.anthropic.claude-haiku-4-5-...`; EOL `claude-3-5-sonnet-20241022` must not appear.
- **Storage:** full-snapshot-per-turn (CC-approved). Note the tradeoff in code; delta-encoding deferred. Snapshots reuse the existing table/`save_snapshot` (`UNIQUE(run_id, turn)`); no schema change expected for 2a (branch columns already exist).
- **Tests:** engine (per-turn checkpoint count, resume-from-state correctness, fresh-start unchanged), storage (snapshot list/get by turn), API (snapshots + branch routes, non-blocking branch, parent-unchanged invariant, branch lineage), and minimal frontend smoke tests (scrubber renders a past turn; branch action posts). Keep LLM calls MOCKED in the suite.
- **Frontend build:** MasterControl MUST run `npm run build` and confirm the served bundle carries the new UI (see acceptance criteria). Do not rely on `tsc` alone.
- **SPDX Apache-2.0** header on new source files.

---

## Handoff

- **Gate:** DRAFT. Do NOT hand off to MasterControl until CC signs off on this doc (hold the DRAFT gate).
- **Open technical calls (confirm on sign-off):**
  1. **Checkpoint storage** — full-snapshot-per-turn (proposed default) vs delta-encoding. Leaning full for now (short runs). Confirm OK to defer deltas.
  2. **Resume entry shape** — new `resume_simulation(...)` fn vs a seed-state param on `run_simulation`. Implementer's choice unless CC has a preference; either must keep the fresh-start path identical.
- **On sign-off:** Main writes the MasterControl brief (repo `/root/projects/matrix-sim-studio`, this doc authoritative), MasterControl builds on HEAD, commits (does not push), rebuilds the frontend bundle, and reports each acceptance checkbox PASS/FAIL/NOT-VERIFIABLE-HERE with live-vs-mocked noted + commit hash. Main independently verifies (tests + live branch on `bridge-kibble` + served-bundle check) before relaying to CC, then pushes to origin.
