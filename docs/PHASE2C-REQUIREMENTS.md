# Requirements — TheMatrix Simulation Studio, Phase 2c

**Status:** DRAFT v2 — four opens resolved by CC 2026-07-09; ready for build approval
**Spec:** `docs/PROJECT-SPEC.md` (§6 Phase 2c, §8b why-not-AutoGen, §6a interaction design)
**Builds on:** Phase 2a (event log + per-turn snapshots) and Phase 2b (branch-with-mutation).

---

## Summary

Phase 2c is the **cognitive layer** — the genuinely novel part of the tool. 2a/2b made the timeline
*editable and branchable*; 2c makes each agent's turn *explainable*. The engine begins emitting
**structured per-agent cognitive-state events** as a natural byproduct of generation — a memory
stream, reflections/beliefs, goal updates, and relationship read-outs — and the UI surfaces them as a
**rich per-agent dossier** plus a **"why did it say that?"** trace on any turn.

The hard constraint that shapes the whole phase: **honesty gate.** Today there is *no* real "why"
data. `_select_next_speaker` makes an LLM call and throws away its reasoning; `_generate_response`
produces an utterance with no captured rationale; `AgentState.memory_stream` and `.relationships`
are declared in `state.py` but **never populated**. 2c must generate this cognition *in the loop, as
the actual input to the next utterance* — never reconstruct it post-hoc as a plausible-sounding
rationalization. State that is emitted must be state that genuinely drove generation, or it is
explicitly labeled model interpretation, not ground truth.

CC directive carried forward: this is the differentiating layer — **do not thin it into cosmetics.**

---

## The core design decision (honesty gate)

There are two ways to produce a "why," and only one is honest:

- **(A) Introspection-at-generation — CHOSEN.** Extend the generation step so each speaking agent
  emits, *in the same call that produces its utterance*, a small structured rationale: which memories
  it drew on, which goal the turn served, and its current stance toward the others. That state is
  then **fed forward** into subsequent turns (retrieved into the next prompt), so it is causally real
  — the memory the dossier shows is the memory the next turn actually consumed. This is the
  event-sourced, cognition-in-the-loop model §6 calls for.
- **(B) Post-hoc reconstruction — REJECTED as ground truth.** A separate pass that "explains" a
  finished turn is a rationalization, not a cause, and violates the honesty gate. We permit a *narrow*
  post-hoc variant ONLY as an explicitly-labeled **aside** (Phase 1.5 machinery, "ask why did X say
  that") — model interpretation, never persisted as the agent's actual state.

Everything below is option A: cognition is produced *during* the run and persisted as events +
snapshot state, exactly like `agent.response` is today.

---

## What exists today (grounding — no fabrication)

- `state.py`: `MemoryItem` (timestamp/content/importance/tags/metadata) and `AgentState`
  (`memory_stream`, `goals`, `relationships`, token/cost totals, portrait) — **schemas present,
  `memory_stream` and `relationships` never written.** Only `persona` + `goals` are populated.
- Engine events emitted: `sim.started`, `speaker.selected`, `agent.response`, `checkpoint.saved`,
  `sim.completed`, `sim.failed`, `avatar.ready`. The `events` table is generic
  (`event_type TEXT` + JSON `payload`) → **new event types are additive, no schema migration.**
- `SimSnapshot` serializes the full `AgentState` dict per turn → once `memory_stream`/`relationships`
  are populated, they **ride along in existing snapshots for free** (scrubber/branch reconstruct
  them automatically).
- `_select_next_speaker` returns only the chosen name; the selector's reasoning is discarded.
- `_generate_response` returns `{content, tokens_in, tokens_out, cost_usd}` — no rationale field.
- `analysis.py` already has the honest "post-hoc reflection" aside framing — reused for the labeled
  option-B "why" aside, NOT for canonical state.

---

## In Scope (Phase 2c)

### 1. Engine: cognition-in-the-loop (additive to the generation step)
- **Structured generation.** Extend `_generate_response` so the speaker returns, alongside its
  utterance, a small structured object (single call, JSON-mode; utterance is the primary field so a
  parse failure degrades gracefully to today's plain-text behavior):
  - `rationale`: one-sentence, first-person reason for *this* turn (real generation output).
  - `goal_served`: which of the agent's current `goals` this turn advances (or "none").
  - `memory_refs`: ids of the memory items the prompt surfaced to it (see retrieval below).
- **Memory formation.** After each turn, the speaker forms 0–2 `MemoryItem`s (what it just learned/
  decided), appended to its `memory_stream` with `importance` and `tags`. Emitted as a
  `memory.formed` event; persisted in the agent's `AgentState` (rides the snapshot).
- **Memory retrieval.** Before a turn, the top-K memories (by importance + recency) are injected into
  that agent's prompt; the ids surfaced are exactly the `memory_refs` the rationale may cite. This is
  what makes memory *causal*, not decorative.
- **Reflection/beliefs.** Every N turns (default N=4, **on** when cognition is enabled; CC
  2026-07-09) an agent condenses recent
  memories into a higher-level `belief` (a `MemoryItem` tagged `reflection`), emitted as
  `agent.reflected`. Keeps the stream from growing unboundedly and produces the "beliefs" view.
- **Goal updates.** If an agent's turn implies a goal shift, it may update its own `goals`; emitted
  as `goal.updated` (before/after). This makes goals dynamic (2b only *edits* the static list at a
  fork; 2c lets the agent evolve them mid-run). Off by default; opt-in per run.
- **Relationships.** After a turn, the speaker may update a short stance string toward another named
  agent (`relationships[other] = "..."`), emitted as `relationship.updated`.
- **Speaker-selection reasoning.** Capture the one-line reason the selector chose this speaker
  (already an LLM call — stop discarding it) into the `speaker.selected` payload as `reason`.
- **All of the above are opt-in via run config flags** (below) and **default to the current
  behavior** so Phase 0/1/1.5/2a/2b runs are byte-for-byte unchanged when cognition is off.

### 2. Run config (additive flags, all default OFF = today's behavior)
- `cognition.enabled` (bool, default False) — master switch. When False, generation is exactly the
  current plain-text path; no new events; zero extra token cost.
- `cognition.memory` (bool) — form + retrieve memories.
- `cognition.reflection_every` (int, default 4 = ON, CC 2026-07-09) — reflect every N turns.
  Reflection is **on by default** when `cognition.enabled` is true; set to 0 to disable.
- `cognition.goals_dynamic` (bool) — allow self goal updates.
- `cognition.relationships` (bool) — track stance strings.
- `cognition.retrieval_k` (int, default 5) — memories injected per turn.
- Cost note: cognition adds tokens per turn (structured output + retrieval context). The live cost
  meter (Phase 3) will reflect it; 2c surfaces per-turn token deltas so the cost is visible, not
  hidden. Honesty gate: never claim cognition is free.

### 3. API (additive, read-only)
- `GET /api/runs/{ref}/agents/{name}/dossier` — the rich dossier for one agent: current goals,
  memory stream (with importance/tags), beliefs (reflections), relationship map, token/cost totals.
  Assembled from the run's events + latest snapshot (no recomputation, no fabrication).
- `GET /api/runs/{ref}/turns/{turn}/trace` — the "why did it say that?" trace for one turn: the
  chosen speaker + selection `reason`, the utterance, its `rationale`, `goal_served`, and the
  `memory_refs` (resolved to their `MemoryItem`s) that were in-context for that turn. All real
  captured data; if a run had cognition off, returns `{available: false}` (no invented trace).
- Optional labeled aside: `POST /api/runs/{ref}/turns/{turn}/why` — reuses Phase 1.5 aside machinery
  to produce a **post-hoc, explicitly-labeled** interpretation for runs that predate cognition.
  Clearly marked "model interpretation, not the agent's recorded state." (Option B, quarantined.)

### 4. Frontend
- **Per-agent dossier panel.** From a run/agent, open a dossier: goals, memory stream (scrollable,
  importance-weighted), beliefs, relationship chips, running token/cost. Live-updates from the event
  stream during a running sim (reuses the existing SSE/event wiring).
- **"Why did it say that?" on a turn.** On any `agent.response` in the transcript/scrubber, a control
  opens the trace: selection reason → rationale → goal served → the memories that were in context
  (each linking into the dossier). Clearly labeled ground-truth (captured) vs. the option-B aside
  (interpretation) when the run had cognition off.
- **Memory/relationship visualization.** A compact relationship view (who-regards-whom) and a memory
  timeline per agent. Keep it legible for small casts; no spatial map (explicitly cut, §9).
- All 2c UI is **additive and gated**: runs without cognition show the existing views unchanged, with
  the dossier/trace offering the labeled post-hoc aside instead of a fabricated trace.

### 5. Determinism / honesty invariants
- Cognition state is only ever **produced forward during generation** and persisted with the turn it
  came from. It is never back-filled onto earlier turns or onto runs that had cognition off.
- Branch/mutation (2b) interoperates: a branch reconstructs cognitive state from the fork snapshot
  (it rides `AgentState`), and forward turns continue producing it. `edit_goal` at a fork sets the
  starting goals; `goals_dynamic` then evolves them — no conflict.
- The parent run is never mutated (2a/2b invariant holds unchanged).

---

## Explicitly NOT in Phase 2c
- **Framework adoption (AutoGen et al.).** Per §8b the verdict stands: keep the hand-rolled loop;
  mine AutoGen's `save_state` design for ideas only. 2c is our cognitive schema, not theirs.
- **Vector DB / embeddings for memory retrieval.** v1 retrieval is importance+recency over the
  in-process `memory_stream` (small casts, short runs). Embedding-ranked recall is a later option,
  not required; do not add a new storage dependency for it in 2c.
- **Cross-run / persistent agent memory.** Memory lives within a run (and its branches via the
  snapshot). Agents do not carry memory between unrelated runs.
- **Spatial map / sprites / movement.** Cut (§9), stays cut.
- **Live cost caps / BYO-key UX / docs site / packaging.** Phase 3. 2c only *surfaces* per-turn token
  deltas so the added cost is honest and visible.
- **Retro-fabricated traces.** A run that ran with cognition off gets `{available:false}` + the
  labeled option-B aside — never an invented ground-truth trace.

---

## Acceptance criteria
- With `cognition.enabled=false`, a run's events, snapshots, JSON result, and token cost are
  **identical** to pre-2c (regression-locked by an equality test against the current path).
- With cognition on: each `agent.response` has a captured `rationale` + `goal_served`; `memory.formed`
  events populate `memory_stream`; retrieval injects the top-K memories and the `memory_refs` cited
  in a rationale are a subset of those injected (causal linkage verified, not just present).
- `reflection_every=N` produces one `agent.reflected` belief per agent per N turns; `goals_dynamic`
  produces `goal.updated` before/after payloads; `relationships` produces `relationship.updated`.
- `speaker.selected` carries a non-empty `reason` when cognition is on.
- `GET .../dossier` and `GET .../turns/{turn}/trace` return only real captured data; trace returns
  `{available:false}` for cognition-off runs (no fabrication).
- Cognitive state survives branch reconstruction (fork a cognition-on run → dossier at the fork
  matches the parent's snapshot at that turn; forward turns extend it).
- The option-B "why" aside is clearly labeled model-interpretation and never persisted as AgentState.
- Full backend + frontend suites pass; new tests cover each event type, the off=unchanged invariant,
  the causal memory linkage, and the dossier/trace endpoints.

---

## Proposed build order (incremental, each shippable)
1. **Config flags + structured generation** — `cognition.enabled`, JSON-mode `_generate_response`
   returning `rationale`/`goal_served` with graceful plain-text fallback; capture selector `reason`
   into `speaker.selected`. Regression test: off = byte-identical to today. (Smallest real slice.)
2. **Memory stream** — `memory.formed` events, `memory_stream` population, top-K retrieval into the
   prompt, `memory_refs` linkage. Verify causal subset property.
3. **Reflection + dynamic goals + relationships** — `agent.reflected`, `goal.updated`,
   `relationship.updated`; each behind its own flag.
4. **Read APIs** — `/dossier` and `/turns/{turn}/trace` (+ the quarantined option-B `/why` aside).
5. **Frontend** — dossier panel, "why did it say that?" trace on a turn, relationship/memory views;
   all gated so cognition-off runs are visually unchanged.

Each step: additive, all prior tests green, its own tests + commit + push (same cadence as 2a/2b).

---

## Decisions (CC 2026-07-09)
1. **Reflection: ON by default** (`reflection_every=4`) whenever cognition is enabled; 0 disables.
2. **Structured-output transport: single litellm JSON-mode call** (assistant's call) — cheaper and
   keeps the rationale causally tied to the utterance it explains (one generation, one record).
3. **Legacy-run "why" trace: SHIP the labeled option-B aside in 2c** — reuse Phase 1.5 aside
   machinery; unmistakably labeled "model interpretation, not the agent's recorded reasoning";
   delivered as a read-only aside, never persisted as `AgentState`.
4. **Retrieval ranking: importance+recency heuristic for v1** — no embeddings / vector store
   (single-node install, §7); embedding-based semantic relevance is a documented later option.

## Open questions (for CC / PreCog)
- None blocking — all four opens resolved above. Embedding-based retrieval is deferred, not cut;
  revisit if recall quality on longer runs proves insufficient.
