# Requirements — TheMatrix Simulation Studio, Phase 4

**Status:** DRAFT — staged by Main 2026-07-19; NOT approved, NOT handed off. Gated on Phase 3 shipping first.
**Spec:** `docs/PROJECT-SPEC.md` (§4a cognition fidelity + priority hierarchy; §6 phase plan)
**Builds on:** Phases 0/1/1.5/2a/2b/2c (feature-complete engine + UI) and Phase 3 (release polish).
**Origin:** Convergent-design pass against an external emergent-narrative prompt ("Mestre Daedalus v2.0", r/PromptEngineering) — see §4a. Four items graduated from that review: a priority-hierarchy validation gate, latent/pending-thread state, an adaptive-pressure intervention, and a structured output contract.

---

## Summary

Phase 4 is **deeper cognition & steering** — the first phase since 2c to add genuine
simulation capability rather than polish. It hardens the engine's *conflict-resolution
behavior* (make the §4a priority hierarchy an enforced gate, not just a doc), gives the
world *memory of its own unfinished business* (pending threads), adds one *new steering
lever* (adaptive pressure), and offers an *optional structured narration contract* for
consumers that want it.

The through-line is the same honesty gate that shaped 2c: every new behavior must be
**causally real and agency-preserving**. A validation pass may *reject-and-regenerate*,
never silently rewrite. Pending threads are *state the loop actually consumes*, not a
post-hoc annotation. Adaptive pressure modulates the world, never the participant's freedom.

CC directive carried forward: do not thin the cognitive layer into cosmetics.

---

## What exists today (grounding — verify before build)

_From the 2c/3 docs and PROJECT-SPEC; the loop internals below should be re-read in code
(`matrix_studio/`) before writing the brief — flagged where verification is required._

- **Event system is additive.** `events` table is generic (`event_type TEXT` + JSON `payload`);
  new event types need no schema migration (established in 2a/2c).
- **State rides in snapshots for free.** `SimSnapshot` serializes the full `AgentState` dict per
  turn; any new field added to `AgentState`/global sim state is carried by the existing
  scrubber/branch machinery automatically (2c pattern).
- **Cognition is generated in-loop (2c).** `_generate_response` emits structured rationale
  (memories drawn on, goal served, stance) *in the same call* as the utterance; that state is fed
  forward. Phase 4 hooks into this same call, not a separate pass. **[verify: exact function names
  / return shape.]**
- **Interventions are branch-from-checkpoint (2b).** The intervention set (inject message, edit
  goal, add/remove persona, promote-aside, continue/restart) is implemented as: load checkpoint N →
  mutate state → resume as a new timeline. Adaptive pressure (4c) is a new member of this family.
  **[verify: where the mutation types are enumerated/dispatched.]**
- **Cost cap + terminal states (3).** `sim.capped`/`capped` pattern is the template for any new
  terminal or gated behavior.
- **No pre-emit validation exists.** The loop produces an utterance and emits it; there is no
  consistency gate between generation and emission today. 4a adds one. **[verify.]**

---

## Decisions carried in / proposed defaults (CC can override)

- **§4a priority order is authoritative:** World coherence > Causality > Continuity > Agent agency
  > Character consistency > Dramatic/emotional impact > Novelty. Never violate a higher for a lower.
- **Validation is reject-and-regenerate, bounded.** On a failed check the loop regenerates the turn
  up to N times (default N=1 retry), then emits the best attempt with a `validation.flagged` event
  rather than looping forever. It NEVER edits the model's output to "fix" it (that would fabricate
  cognition). Default: **validation ON, retry budget 1.**
- **Adaptive pressure default OFF**, opt-in per run (experimental), like the cost cap.
- **Structured output contract default OFF** — engine keeps emitting today's events; the
  Narrative/Consequences/State/Possibilities framing is an optional *view/formatter*, not a change
  to canonical events.

---

## In Scope (Phase 4)

### 4a. Priority-hierarchy validation gate (do first)
The §4a hierarchy becomes an **enforced pre-emit pass** in the generation loop.
- After a turn is generated and before it is committed as `agent.response`, run an ordered
  consistency check keyed on the hierarchy: coherence (respects established world rules) →
  causality (follows from prior state, no unearned coincidence) → continuity (no timeline/state
  contradiction) → agency (does not silently negate a prior participant action) → character
  consistency (in-line with the agent's own goals/beliefs from its 2c cognition state).
- **Mechanism (honesty-preserving):** the check is itself a small LLM/heuristic call over the
  candidate utterance + current sim state; on failure it **regenerates** (retry budget, default 1),
  then emits with a `validation.flagged` event carrying the failing principle. It MUST NOT rewrite
  the utterance in place.
- Emits a new `validation.checked` event (pass/fail + which principle), so the dossier/why-trace can
  show "this turn was validated / regenerated once for a causality violation."
- **Off-switch:** `settings.validation_enabled` (default ON) — OFF reproduces pre-4a behavior
  byte-for-byte.

### 4b. Latent-event / pending-thread state (item 8)
Give the world memory of unresolved setups and deferred actions.
- New first-class state: a `pending_threads` collection (global sim state + optionally per-agent),
  each entry = `{id, description, type (setup|promise|faction-action|deferred-consequence), origin_turn,
  status (open|resolved|abandoned), resolved_turn?}`. The engine analogue of REHOBOAM's
  Setups & Payoffs ledger.
- **Causally real:** open threads are retrieved into subsequent turn prompts (like memory in 2c), so
  they genuinely influence generation — not a passive annotation. When a turn resolves one, it is
  marked `resolved` with the turn ref.
- Events: `thread.opened`, `thread.resolved`, `thread.abandoned`. Rides in snapshots automatically.
- **Staleness surfacing:** open threads older than a configurable age (default 5 turns) are flagged
  in the dossier as "dangling," mirroring the ledger's stale-thread signal.
- Populating threads happens in-loop (the generating agent may emit "I am planting X" as part of its
  2c structured output) — **verify the 2c rationale schema can carry this** or extend it minimally.

### 4c. Adaptive-pressure intervention (item 10 — experimental, opt-in)
A new member of the 2b branch-from-checkpoint intervention family.
- Observes run-level signals (repetition, stalled threads, low novelty, turn budget remaining) and,
  when enabled, introduces pressure: escalate a moral/strategic dilemma, inject a new conflict,
  resolve a stale faction action, or shift pacing — always as a branch/mutation, never an in-place
  edit of history.
- **Agency guard (hard):** pressure modulates the *world/NPCs*, never removes the participant's
  choices (agency is #4 in the hierarchy; 4a validates it). Pressure that would negate participant
  freedom is rejected by the 4a gate.
- **Off by default**, opt-in per run (`settings.adaptive_pressure_enabled`), clearly labeled
  experimental. Ships last, gated behind 4a.

### 4d. Structured output contract (item 11 — optional view)
An optional narration format for consumers that want a game-master-style read-out.
- A formatter that projects a turn's canonical events into four sections:
  **Narrative** (what happened), **Consequences** (immediate + deferred changes), **Updated State**
  (world/agent/faction deltas — sourced from real state events, not invented), **Possibilities**
  (surfaced next directions, non-limiting).
- **Not a change to canonical events** — it is a derived view over existing 2c/4b state, exposed via
  an API/formatter flag (`settings.structured_output` or a per-request param). Default OFF.
- "Updated State" and "Consequences" must draw only from actual emitted state deltas + open/resolved
  threads (4b) — no fabricated consequences. If data is absent, the section says so.

---

## Explicitly NOT in Phase 4
- **Rewriting model output to enforce consistency.** The 4a gate rejects-and-regenerates or flags;
  it never edits an utterance in place (that fabricates cognition — violates the honesty gate).
- **Removing participant agency for drama.** Adaptive pressure never overrides a participant choice;
  the agency principle outranks dramatic impact in §4a.
- **Making the structured output the canonical record.** 4d is a derived view; canonical state stays
  the event log/snapshots.
- **Embedding-based memory retrieval.** Still deferred (carried from 2c/3). Pending-thread retrieval
  uses the existing recency/importance retrieval, not new vector infra.
- **Unbounded validation loops.** Retry budget is small and fixed; no "regenerate until perfect."
- **Multi-node / auth / hosted changes.** Out of scope, same as Phase 3.

---

## Acceptance criteria
- With `validation_enabled = OFF`, runs behave byte-for-byte as pre-4a (events/snapshots/cost
  identical). Regression-locked by test.
- With validation ON, a turn that violates a hierarchy principle emits `validation.checked` (fail +
  principle) and either regenerates within budget or emits with `validation.flagged`; the utterance
  is never edited in place. Test covers pass, regenerate-then-pass, and budget-exhausted-flag.
- `pending_threads` open/resolve/abandon correctly across a run; open threads appear in subsequent
  turn prompts (proven by a test that a resolved thread stops being retrieved); threads survive
  branch/scrubber reconstruction. Stale threads surface in the dossier.
- Adaptive pressure OFF = no behavior change; ON introduces pressure only as branches and never
  produces a turn that the 4a agency check rejects. Test covers off-unchanged + one pressure branch.
- Structured output OFF = today's API; ON returns the 4-section view sourced only from real state
  (a test asserts no consequence/state line lacks a backing event).
- Full backend + frontend suites pass; each item has its own tests + commit + push (2a/2b/2c cadence).
- CHANGELOG updated; version bumped (e.g. 0.3.x → 0.4.0).

---

## Proposed build order (safest first, riskiest last)
1. **4a — validation gate.** Smallest, unblocks the others (agency check is reused by 4c), already
   the documented invariant. Ship with off-switch + regression lock.
2. **4b — pending-thread state.** Additive state + events + in-loop retrieval; leans on the 2c
   feed-forward pattern and 2a snapshots.
3. **4d — structured output view.** Low coupling, mostly a formatter over 4b/2c state; can ship
   independently once 4b lands.
4. **4c — adaptive pressure.** Highest risk; opt-in/experimental; gated behind the 4a agency check.

Each step: additive, all prior tests green, its own tests + commit + push.

---

## Open questions (for CC)
1. **Validation retry budget** — default 1 retry (regenerate once, then flag-and-emit). More = higher
   fidelity but higher cost/latency. OK at 1?
2. **Validation mechanism** — small dedicated LLM check per turn (cost) vs cheaper heuristics +
   selective LLM only on suspected violations. I lean heuristic-first to control cost; confirm.
3. **Pending-thread staleness age** — default 5 turns (vs REHOBOAM ledger's 3 chapters — different
   unit). Tune?
4. **Is 4c worth building in this phase at all**, or park it as Phase 4.5/experimental branch? It is
   the one item that can fight the agency principle and needs the most care.
5. **Version target** — 0.4.0 (new capability) vs 0.3.x (if you consider this incremental)?
6. **Sequencing vs Phase 3** — this doc assumes Phase 3 ships first. Confirm no overlap desired.
