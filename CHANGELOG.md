# Changelog

All notable changes to TheMatrix Simulation Studio are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
