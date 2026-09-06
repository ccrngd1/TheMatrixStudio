# Changelog

All notable changes to TheMatrix Simulation Studio are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Phases 5 and 6 are complete in the working tree but not released; the last version
bump was 0.4.0 (Phase 4).

### Added - Phase 6 (Structured Personas)
- **Convictions, not just goals.** Cast members may carry a `structured` block — `background.formative_events[].lesson`, `preferences.optimises_for` / `dismisses` / `persuaded_by`, and `viewpoints[]` with `position` / `formed_by` / `firmness` / `evidence_that_shifts` / `underlying_concern`. Goals are *satisfiable*, so a persona holding one can be talked into any plan satisfying it; convictions are *defended*. Additive to the `persona` prose string, never a replacement. New module `matrix_studio/personas.py` (pure models + rendering: no LLM, no database, no state), new `AgentState.structured`, new `PersonaConfig` (`config.personas`). Off by default: with `enabled` false a `structured` block is ignored and prompts are **byte-identical** to pre-Phase-6, asserted by diffing real prompts rather than inspecting the renderer.
- **`underlying_concern` is withheld, enforced by the code path.** Two renderings: `render_private()` for the speaker's own system prompt, `render_public()` (role + `optimises_for`, one line) for the moderator's persona list. The moderator prompt is the one place every persona appears at once, so rendering the private block there would put each withheld concern one prompt away from the whole cast — and drawing that concern out is the exercise. Also stripped from the `persona.structured` event and the dossier API (both are exported/rendered surfaces). Tests scan *every* prompt in a real run.
- **`validity` reaches no prompt at all** — an operator calibration note (are the firmest positions also the soundest? they should not be), used only for post-run scoring. `tests/test_examples.py` asserts the shipped example stays calibrated.
- **Re-tuned dismissal rule**, which was the premise validation's explicit ship condition. Arm C's naive "judge only against your own priorities" rule degraded discussion into repetitive parallel monologues (talking-past 4/5, within-speaker similarity +37%, cross-speaker similarity *worse than control*). The shipped rule limits **priorities, not attention**: answer the substance of a challenge directly, state the other side at its strongest, then decline *once*. Three prohibitions matching the three observed failure shapes, each test-locked. `dismissal_rule: false` reproduces the Arm C configuration for re-measurement and takes the `dismisses` list with it, so that configuration cannot be reached by accident.
- **`requires-escalation` firmness** kept rather than narrowed away: it says the speaker lacks the *authority* to concede, so being overruled produces "I'll have to take this further", not agreement — the one firmness level giving a persona something honest to do other than agree or repeat itself.
- **A defended position with no exit condition is named as such** in the prompt ("you have not named anything that would change your mind … do not invent a condition you do not have"), because silence lets the model either stonewall or fabricate. Found by reading a real rendered prompt.
- **New event:** `persona.structured` (turn 0, per cast member with structure, emitted only when the feature is on). **New dossier field:** `structured`. **New example:** `examples/structured-personas.json` — the validated Arm B cast in the shipped schema. **New doc:** `docs/PHASE6-STRUCTURED-PERSONAS.md`.
- Convictions survive a fork (`reconstruct_at_turn` seeds `structured` from the stored cast, private fields included) and an in-place resume.

### Changed
- An unknown `firmness` is **rejected**, not silently downgraded — 422 at the API boundary (`PersonaModel.structured` is typed as the real model, not a loose dict), raised at run start from the CLI. The block is parsed even when the feature is off, so a typo surfaces immediately rather than the day someone enables the flag.

### Measured - Phase 6 (Arm D, live, 2026-09-06)
- **Fourth validation arm** added to `scripts/build_validation_arms.py` and `scripts/score_validation.py`: `arm-d-shipped` is the control prose + a `structured` block + `personas.enabled`, deliberately *not* Arm B's rendered persona string (reusing that would measure the hand-written prose and the engine's rendering at once). Arm D is the only arm that legitimately differs in `config`, so the byte-identity test now enumerates its two permitted extra keys by set difference rather than skipping it. Scored when present, skipped when absent, so the original three-arm comparison stays reproducible.
- **Result: mixed.** First arm ever to produce **evidence-driven position change — 2 of 2** (previous best: Arm B with 1 change, 0 evidence-driven); one fires exactly on its `evidence_that_shifts`. Arm C's parallel-monologue failure did **not** reproduce (talking-past 2 vs C's 4). Accommodation 0.400, matching Arm B, well below the control's 0.667.
- **But worse than Arm B on divergence** (cross-speaker similarity 0.1895 vs 0.1597), and that comparison is **confounded**: Arm D's turns are 37% longer and the metric is Jaccard token overlap, so length inflates it. Not separable at n = 1. Arm B's hand-written prose still wins on raw divergence.
- **Two properties could not be tested.** `requires-escalation` never fired because the room never overruled the persona holding it. The withheld concern was never drawn out because nobody asked why — zero verbatim leaks (withholding works), zero "why" questions in 15 turns. The second is a **design finding**, not just a measurement gap: nothing in a run creates pressure to ask a stakeholder why, so `underlying_concern` may be inert in practice.
- Cost: $0.0719 run + $0.0207 judge. Five follow-ups recorded in `docs/BACKLOG.md`, led by "repeat at several seeds" — `n = 1` on a non-deterministic model.

### Not claimed
- The premise validation's null result is **unchanged**: Arm D also scored 5 distinct positions and 5/5 specificity. A fourth arm at the same ceiling is more evidence the metric cannot express a gain, not evidence there is none.

## [0.4.0] - 2026-07-19

### Added - Phase 4 (Deeper Cognition & Steering)
- **4a — Priority-hierarchy validation gate:** An enforced pre-emit consistency pass over each generated turn, keyed on the hierarchy (world coherence > causality > continuity > agent agency > character consistency > dramatic impact > novelty). Heuristic-first; a small LLM confirmation call is made only on suspected violations (cost control), fail-open on checker errors. A violating turn is regenerated once (`validation_retry_budget`, default 1), then emitted **verbatim** with a `validation.flagged` event — model output is never rewritten in place. New events: `validation.checked` (per attempt), `validation.flagged`. New settings: `validation_enabled` (default ON; OFF is byte-for-byte pre-4a, regression-locked by test), `validation_retry_budget`. The turn trace API now surfaces the validation trail.
- **4b — Pending-thread ledger (setups & payoffs):** First-class `pending_threads` state (`PendingThread` on `SimSnapshot`), opt-in via `config.cognition.threads`. Agents plant/resolve/abandon threads through the same single structured generation call (`thread_updates`); open threads are fed into subsequent turn prompts (causally real) and recorded as the turn's `thread_refs`. Resolutions are accepted only for threads genuinely in-context (no fabricated payoffs). New events: `thread.opened` / `thread.resolved` / `thread.abandoned` (lossless replay). The ledger rides every snapshot and survives branch/scrub/resume reconstruction. New API: `GET /api/runs/{ref}/pending-threads` with staleness flags (`thread_stale_after`, default 5 turns); dossier lists the agent's planted threads.
- **4d — Structured output view (optional):** `GET /api/runs/{ref}/turns/{turn}/structured` projects a turn's canonical events into Narrative / Consequences / Updated State / Possibilities. Narrative is verbatim utterances; every consequence/state line carries the `source_seq` of its backing canonical event; Possibilities are the open 4b threads (non-limiting); absent data is stated, never invented. Default OFF (`structured_output` setting or `?opt_in=true` per request); a derived read-only view — canonical events unchanged.
- **4c — Adaptive-pressure intervention (EXPERIMENTAL, opt-in):** New `adaptive_pressure` branch-mutation kind. Observes run-level signals at the fork (repetition, stale threads, remaining budget — real state only) and injects ONE narrator-voiced world event as a branch turn, preceded by a `pressure.applied` audit event. Hard agency guard (shared with 4a): pressure modulates the world only; generated text negating participant choice is regenerated once, then the whole intervention is rejected (HTTP 422) — nothing emitted, nothing rewritten. Default OFF (`adaptive_pressure_enabled`), refused at both API and engine layers while disabled. Marked experimental in README and UI.

### Changed
- Version bumped 0.3.0 -> 0.4.0 (new simulation capability).
- Branches / in-place resumes now carry the run's cognition config (and 4b thread ledger) forward; runs without a cognition config behave exactly as before.

## [0.3.0] - 2026-07-09

### Added - Phase 3 (Release Polish)
- **Cost guards:** Optional per-run hard spend cap (`max_run_cost_usd` setting, default 0 = OFF). Engine checks accumulated cost after each turn; when cap is reached, run ends in terminal `capped` status with `sim.capped` event. Additive, opt-in feature; cap=0 behaves byte-for-byte identical to pre-Phase-3.
- **Provider readiness check:** `/api/health` endpoint now returns per-provider has-key booleans (openai, anthropic, bedrock) for BYO-key setup UX. NEVER exposes key values.
- **Secret safety tests:** Comprehensive test suite asserting no `.env`-style credential values ever appear in any API response (health, models, runs, events).
- **Example templates:** 4 curated examples demonstrating different use cases:
  - `minimal.json`: Simple 2-person conversation
  - `debate.json`: 3-person debate on AI in creative work
  - `coffeeshop.json`: 4-person reunion conversation
  - `design-review.json`: 3-person design review with cognition enabled (showcases Phase 2c features)
- **Documentation:** Complete README rewrite describing shipped v0.3.0 tool (control room, branching, cognition, avatars), 5-minute quickstart (pip + Docker), full configuration reference, cognition/honesty note, usage guide, architecture.
- **Screenshot placeholders:** Labeled TODO markers for control-room and dossier screenshots.
- **Test mode:** `_MSS_TEST_MODE` env var to disable `.env` loading in tests (avoids permission issues in restricted environments).

### Changed
- Version bumped from 0.1.0 to 0.3.0 (feature-complete, pre-1.0 polish)
- Terminal events now include `sim.capped` (WebSocket closes on cost-cap-hit)
- README no longer says "Phase 1.5" — accurately describes all shipped features through Phase 2c + Phase 3

### Fixed
- Settings class now respects test mode to avoid `.env` permission errors in test environments

## [0.2.0] - 2026-07-09

### Added - Phase 2c (Agent Cognition)
- **Memory system:** Agents form, retrieve, and cite memories (importance-scored, tagged, timestamped). Top-K retrieval by importance + recency; retrieved items are the turn's causal `memory_refs`.
- **Reflection:** Periodic higher-level belief formation (every N turns, configurable via `cognition.reflection_every`). Emits `agent.reflected` events.
- **Dynamic goals:** Agents can update their own goals mid-run (via `goal_update` field in structured output). Emits `goal.updated` events.
- **Relationships:** Per-agent stance tracking toward other participants. Emits `relationship.updated` events.
- **Why-trace:** "Why did they say that?" — rationale + goal_served captured per turn when cognition is enabled.
- **Rich dossier API:** `/api/runs/{ref}/agents/{name}/dossier` returns full agent state (memory stream, goals, relationships, conversation history).
- **Trace API:** `/api/runs/{ref}/trace?turn={N}` returns the causal chain for a specific turn (speaker selection reason, retrieved memories, rationale, goal served).
- **Cognition config:** Per-run opt-in via `config.cognition` object (enabled, memory, reflection_every, goals_dynamic, relationships, retrieval_k). All default to pre-2c behavior when disabled.

### Added - Phase 2b (Interventions)
- **Inject message:** Branch from turn N and inject a message (user or narrator) into the conversation. Persisted as a real branch turn with `injected` flag.
- **Continue:** Extend the turn budget (`add_budget`) to let the group keep talking.
- **Edit goal:** Change a persona's goals at the branch point (in-place state mutation).
- **Add persona:** Introduce a new character mid-conversation (in-place state mutation).
- **Remove persona:** Remove a character from the cast (in-place state mutation, requires ≥1 persona remain).
- **Promote aside:** Promote an aside conversation's reply into the main timeline as an injected message, then continue the discussion.
- **Branch tree UI:** Visual branch tree in frontend showing parent/child relationships.

### Added - Phase 2a (Checkpointing & Branching)
- **Per-turn checkpointing:** Full `SimSnapshot` persisted after every turn (run_id, turn, topic, agents, conversation, status). Stored in `snapshots` table with `UNIQUE(run_id, turn)`.
- **Branch primitive:** Fork the event log at turn N → resume forward as a new run (new `run_id`, `parent_run_id`, `branch_turn` columns). Original run immutable.
- **Checkpoint scrubber/replay:** UI timeline scrubber to jump to any turn; reconstruct state by loading snapshot at that turn.
- **Resume simulation:** `resume_simulation` engine entry point continues from a checkpoint (distinct from fresh-start `run_simulation`).
- **Branch API:** `POST /api/runs/{ref}/branch` creates a new branch run from a parent at a specific turn.

## [0.1.5] - 2026-07-08

### Added - Phase 1.5 (Post-Run Analysis Layer)
- **Structured summary:** Auto-generated end-of-run summary (consensus, dissenters, key ideas, open questions, overview). Configurable field set + optional focus lens. On by default, persisted as JSON blob attached to run.
- **Aside conversations:** Read-only side-threads over completed runs (never mutate canonical timeline):
  - **Analyst mode:** Ask a neutral summarizer/analyst about the run.
  - **Persona mode:** Ask a specific persona (in-character, using their stored persona).
  - **Room mode:** Ask all personas (group response into the thread).
- **Thread model:** Scoped threads with target (analyst / persona / room) and mode (aside = read-only, contribute = mutating, Phase 2 feature). Data model anticipates Contribute from day one.
- **Summary regeneration:** `/api/runs/{ref}/summary:regenerate` endpoint to regenerate summary with different config (field set, focus, instructions).
- **Thread API:** `/api/runs/{ref}/threads` (list), `POST /api/runs/{ref}/threads` (create), `/api/runs/{ref}/threads/{id}/messages` (retrieve), `POST /api/runs/{ref}/threads/{id}/messages` (send).
- **Thread persistence:** Threads + messages stored in `threads` and `thread_messages` tables (Phase 1.5 schema extension).
- **Cost tracking:** Aside token/cost tracked separately from canonical run; summary cost added to `runs.summary_cost_usd`.

## [0.1.0] - 2026-07-08

### Added - Phase 1 (Control-Room Web UI)
- **Web UI:** Cast board with character cards (avatar + persona + goals), live-scrolling conversation feed, active-speaker highlight, cost meter, searchable run history.
- **New-run form:** Topic + cast builder, model picker (from `AVAILABLE_MODELS`), cognition config (Phase 2c forward-looking), avatar toggle, summary config.
- **Per-agent dossier:** Click a card to view that agent's full state (as rich as engine exposes; Phase 2c deepens this).
- **Live WebSocket stream:** `/api/runs/{ref}/stream` replays historical events then subscribes for live tail. Late joiners catch up automatically.
- **Run codenames:** Every run gets a memorable two-word codename (e.g., `trusted-robot`) via LLM-generated topically-resonant suggestions with random-wordlist fallback. Shown in UI, used as URL ref (`/api/runs/{codename}`).
- **Cost meter:** Live token/$ display in UI, accumulated per agent and per run.
- **UI-only playback:** Pause / resume / step / reveal-speed controls operate on client-side buffered stream (engine always runs to completion at full speed).
- **Avatar generation:** Parallel portrait generation via Stability SD3.5 on Bedrock (`stability.sd3-5-large-v1:0`). Mandatory initials/color fallback; avatars are optional eye-candy. Emits `avatar.ready` events as each finishes.
- **Model selection:** Per-run model override (picker in new-run form), persisted in `runs.config.model`.
- **FastAPI backend:** REST API + WebSocket, serves built React frontend as static assets from same process (one container, one port).

### Added - Phase 0 (Standalone CLI + Event-Sourced Storage)
- **Standalone CLI:** `matrix-studio run <file.json>` to run simulations headlessly; `matrix-studio serve` to start web server.
- **Provider-agnostic engine:** Hand-rolled async litellm loop (not AutoGen). Two-phase turn: (1) select speaker via LLM, (2) generate response via LLM. Supports any LiteLLM model (OpenAI, Anthropic, Bedrock, OpenRouter, Ollama).
- **Event-sourced storage:** Append-only event log + snapshots in SQLite (`./data/matrix_studio.db`). Tables: `runs`, `events`, `snapshots`.
- **Configuration:** Pydantic settings loaded from env vars or `.env` file (precedence: env > .env > defaults). Settings: `LITELLM_MODEL`, `LITELLM_TEMPERATURE`, `LITELLM_MAX_TOKENS`, AWS/OpenAI/Anthropic credentials, `MAX_MESSAGES`, `DATA_DIR`, `MATRIX_HOST`, `MATRIX_PORT`, avatar config.
- **Dockerfile:** Multi-stage build (Node stage builds React frontend, Python stage installs package + serves API+UI).
- **License:** Apache-2.0 (patent grant suits a customer-facing tool).
- **Test suite:** 186 backend tests (all mocked; no live LLM calls).

## [Unreleased]

### Planned
- Embedding-based memory retrieval (deferred from Phase 2c)
- Multi-modal inputs (images, audio)
- Hosted/multi-tenant deployment

---

**Note:** Phases 0-3 built 2026-07-07 through 2026-07-09 by MasterControl (orchestrated by CC). Each phase is a shippable increment; all existing tests remain green at every commit.
