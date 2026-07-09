# Requirements — TheMatrix Simulation Studio, Phase 2b

**Status:** DRAFT — for CC approval (2026-07-09)
**Spec:** `docs/PROJECT-SPEC.md` (§3.6/§3.7 branching, §6 Phase 2b, §6a interaction design)
**Builds on:** Phase 2a (fork + resume-forward plumbing, per-turn checkpointing, scrubber).

---

## Summary

Phase 2b turns the 2a fork-and-resume primitive into the **full interaction set**: the user can now
*change the timeline* at any checkpoint and let the group continue. Every 2b operation is the **same
underlying move** — *load checkpoint at turn N → apply ONE mutation → resume generation forward as a
new run* — differing only in **which mutation** is applied. The parent run stays immutable; each
intervention produces a new branch run, watchable/replayable with the existing machinery.

CC directive (carried from 2a spec): **do not reduce scope.** The full set ships in 2b.

---

## Goal

An additive capability set on the existing engine + API + UI that:
- introduces a single **mutation-at-fork** hook: `reconstruct_at_turn` → apply mutation → resume,
- exposes the **six intervention operations** (below), each as a typed branch-with-mutation call,
- activates the Phase 1.5 disabled **"bring into conversation"** affordance (promote-aside-to-room),
- adds a **branch-tree visualization** in history (parent/child lineage across forks),
- keeps Phase 0/1/1.5/2a behavior **unchanged**; all existing tests pass,
- adds **zero** OpenClaw coupling and preserves the honesty gate (no fabricated state/metrics),
- never mutates a parent run (immutability invariant — mutations only ever seed a NEW branch).

---

## The six operations (all branch-with-mutation)

Each is a mutation applied to the reconstructed `(agents, conversation)` at `from_turn`, *before*
`resume_simulation` generates forward. All reuse 2a's `create_branch_run` + `execute_branch`.

1. **Inject a message.** Insert a user/narrator or attributed message into the conversation at the
   fork, then continue. The injected turn is a real conversation entry the group sees going forward.
2. **Promote aside → room** ("bring into conversation"). Take a chosen reply from an existing *aside*
   thread (Phase 1.5) and inject it into the canonical conversation as a branch mutation. Activates
   the disabled affordance in `AsidesDrawer`. Implemented as a specialization of #1 (the aside reply
   text becomes the injected message, attributed to its aside target).
3. **Continue / restart the discussion.** Branch at the last turn (or any turn) with an extended
   budget and *no* other mutation, letting the group keep talking. (2a's resume already does the
   mechanics; 2b exposes it as a first-class "continue" action with an explicit added budget.)
4. **Edit a goal.** Modify one agent's `goals` list in the reconstructed `AgentState` before resume,
   so subsequent turns reflect the new goal. Original run's goals are untouched.
5. **Add a persona.** Introduce a new cast member (name + persona text + goals) into `agents` at the
   fork; they participate in speaker selection from `from_turn + 1` forward.
6. **Remove a persona.** Drop a cast member from `agents` at the fork; they no longer speak forward.
   Their prior turns remain in the (immutable) transcript up to the fork.

Plus:
7. **Branch-tree visualization.** History shows the lineage: a run and its branches (and their
   branches) as a tree keyed on `parent_run_id` / `branch_turn`, with the mutation kind labeled per
   edge (e.g. "inject @ turn 12", "add persona @ turn 8").

---

## In Scope (Phase 2b)

### 1. Mutation-at-fork hook (engine/service, additive)
- A single typed **`BranchMutation`** applied after `reconstruct_at_turn(...)` and before
  `resume_simulation(...)`. One mutation per branch (compose by chaining branches). Shapes:
  - `{"kind": "inject_message", "speaker": str, "content": str}` — append a conversation entry.
  - `{"kind": "promote_aside", "thread_id": str, "message_id": str}` — resolve the aside reply
    server-side, then behave as `inject_message` attributed to the aside target.
  - `{"kind": "continue", "add_budget": int}` — no state change; extend the turn budget.
  - `{"kind": "edit_goal", "persona_name": str, "goals": [str]}` — replace that agent's goals.
  - `{"kind": "add_persona", "name": str, "persona": str, "goals": [str]}` — add to `agents`+cast.
  - `{"kind": "remove_persona", "name": str}` — drop from `agents` (kept in prior transcript).
- The mutation is recorded in the **branch run's config** (`config.branch_mutation`) so the branch is
  self-describing and the tree UI can label the edge. Additive; never written to the parent.
- **Validation** (HTTP 422 on violation): inject/edit/remove must reference a persona/thread/message
  that exists at the fork; `add_persona` name must not collide with an existing cast member;
  `remove_persona` must leave **≥1** persona; `edit_goal`/`add_persona` goals is a list of strings;
  `continue.add_budget ≥ 1`.

### 2. API (additive endpoints)
- Extend `POST /api/runs/{ref}/branch` body with an optional `mutation: BranchMutation` (2a's plain
  fork = no mutation, unchanged default). Keeps one branch entry point; the mutation is the variant.
- `GET /api/runs/{ref}/tree` — the lineage tree rooted at the run's top ancestor: nodes = runs
  (id, name, status, branch_turn, mutation kind), edges = parent→child. Read-only.
- Promote-aside reuses the branch endpoint (`mutation.kind = "promote_aside"`); no new write path in
  the threads router — the aside stays read-only, promotion creates a *new run*.

### 3. Frontend
- **Intervention controls at a checkpoint.** On the scrubber / a selected turn (completed run),
  offer the applicable actions: inject message, edit goal, add persona, remove persona, continue.
  Each opens a small form, posts the branch+mutation, and navigates to the new branch run (same as
  2a's branch flow). Clearly labeled "this creates a new timeline; the original is unchanged."
- **Activate "bring into conversation"** in `AsidesDrawer` (remove the disabled/"later version"
  state): a reply's action posts `promote_aside` and navigates to the new branch.
- **Branch-tree view** in History: render `/api/runs/{ref}/tree` as an indented/graph lineage with
  mutation labels; clicking a node opens that run. Replaces/augments the flat parent link from 2a.
- The in-thread **model picker** (shipped 2026-07-09) applies to branch generation as today.

### 4. Determinism / immutability (unchanged invariant)
- We only ever **generate forward** from the fork with the mutation applied. Non-determinism past the
  fork is expected and fine. The parent's event log and snapshots are never modified or re-run — all
  writes target the new branch `run_id` (enforced by construction, as in 2a).

---

## Explicitly NOT in Phase 2b
- **Rich per-agent introspection** (memory stream, reflections/beliefs, relationship graph, goal
  *hierarchy*, "why did it say that?" trace) — **Phase 2c**. 2b edits the *existing* flat `goals`
  list and cast; it does not add new cognitive-state events.
- **Mid-run (live) intervention.** 2b interventions branch from a *checkpoint* of a finished/paused
  run, producing a new run — consistent with the 2a model. Pausing a live run to intervene in place
  is out of scope (revisit only if CC wants it; the branch model already covers the use cases).
- **Multi-mutation-per-branch.** One mutation per branch in 2b (chain branches to compose). A batch
  editor is a possible later convenience, not required.
- **Spend caps / BYO-key UX / docs site** — Phase 3.

---

## Acceptance criteria
- Each of the six operations creates a new branch run whose forward turns reflect the mutation, with
  the parent unchanged (verified by comparing parent event log/snapshots before & after).
- `config.branch_mutation` is persisted and surfaced in `/api/runs/{ref}/tree`.
- Validation rejects malformed/again-nonexistent references with 422.
- "Bring into conversation" is live in the asides UI and promotes the correct reply text.
- Branch-tree renders correct lineage + mutation labels for a ≥3-level fork.
- Honesty gate intact: no fabricated state; mutations only reflect real user input.
- Full backend + frontend suites pass; new tests cover each mutation kind + validation + tree.

---

## Proposed build order (incremental, each shippable)
1. **Engine/service mutation hook** + `inject_message` + `continue` (smallest, exercises the path).
2. **`edit_goal`, `add_persona`, `remove_persona`** (state mutations on the reconstructed cast).
3. **`promote_aside`** + activate the asides affordance (depends on #1 inject path).
4. **`/api/runs/{ref}/tree`** + branch-tree UI.
5. **Frontend intervention forms** on the scrubber/turn view, wiring all mutations.

Each step: additive, all prior tests green, its own tests + commit.
