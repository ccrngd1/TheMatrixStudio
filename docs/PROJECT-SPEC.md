---
title: "TheMatrix Simulation Studio"
type: project
tags: [cabal, thematrix, simulation, product, ideation, generative-agents]
sources: ["github.com/joonspk-research/generative_agents"]
created: 2026-07-07
updated: 2026-07-08
confidence: high
status: research
owner: CC
related: ["[[thematrix]]", "[[cabal]]"]
---

# TheMatrix Simulation Studio — Ideation & Architecture (working title)

_Status: ideation. Owner: CC. Captured by Main from Slack ideation session 2026-07-07._
_Next: PreCog (research substrate + provider layer), MasterControl (build). Do NOT hand off until CC signs off on this doc._

## 1. What this is

A **standalone, distributable** multi-agent simulation tool — spiritual successor to Stanford's
`generative_agents` (Park et al., 2023), but with the parts that aged badly removed and the
missing interaction/visualization layer added.

Lineage: `github.com/joonspk-research/generative_agents`. We take the *idea* (persona agents with
memory/goals interacting over time) and the *appeal* (a watchable, steerable cast), and drop the
research-artifact baggage (spatial pixel-town, hand-rolled 2023 memory code, GPT-3.5-era cost model).

Builds on the existing **TheMatrix engine**, a hand-rolled async loop over the litellm SDK +
Bedrock — verified 2026-07-08. NOTE: AutoGen is listed in `requirements.txt` but is NOT used by
the engine code; orchestration is custom (`_select_next_speaker()` + `_generate_response()`, two
litellm calls per turn). Engine is already effectively standalone (JSON in/out, no OpenClaw memory
coupling).

## 2. Why (the justification bar)

Confirmed use case: **a demo/showcase tool CC ships to his customers**, so those customers can
demo multi-agent simulations to *their* internal users / business owners / stakeholders.

That makes this a **public release**, not an internal tool. Design and maintenance bar rises
accordingly (see §7). Portability is not a "nice to have" — it's the point.

## 3. Hard product decisions (locked in this session)

1. **Standalone. Zero OpenClaw dependency.** A hard dep on OpenClaw is unpalatable to external
   users. OpenClaw is not the host and not required to run.
2. **Provider-agnostic model layer via LiteLLM used as a library (not the proxy).** BYO API key
   (OpenAI / Anthropic / Bedrock / OpenRouter / local Ollama). Already proven: current engine calls
   `litellm.acompletion()` directly.
3. **No spatial simulation.** No map, no sprites, no pathfinding. That was the least-valuable,
   most-time-consuming part of Smallville.
4. **The UI centerpiece is a "cast board," not a town.** Each agent = a character card with a
   generated avatar, personality summary, current goal/plan, and a live-scrolling memory/thought
   feed. Click → full dossier (memory stream, reflections/beliefs, relationships, goal hierarchy,
   "why did it say that?" trace).
5. **Per-run generated avatars.** At sim-init, an LLM invents appearance details per persona →
   image-gen renders a portrait. Generated fresh each run (cast differs run-to-run). Generate in
   parallel at startup; cache with the run. Purely eye-candy, accepted as such.
6. **Execution is decoupled from interaction.** The engine runs at full speed to completion (or a
   turn budget), emitting a checkpoint per turn. The UI subscribes to the stream ("live watch" =
   tailing checkpoints; generation never blocks on the user).
7. **Intervention = branching.** Pause/redirect/inject-event/add-persona is implemented as:
   load checkpoint N → mutate state → resume as a *new* timeline. Original run is untouched/recorded.
8. **Cost visibility is a first-class feature.** Live token/$ meter during runs. Customer
   stakeholders will demand it.

## 4. Architectural keystone: event-sourced, serializable state

Both the rich persona UI *and* the branching model require the same thing:
**the engine's entire state must be a fully serializable snapshot** — every agent's memory stream,
goals, relationships, plus global sim state (event log, turn counter, active cast).

Implementation pattern: **event sourcing** — append-only event log + periodic full snapshots;
reconstruct any moment by replaying events from the nearest snapshot. A **branch = fork the event
log at turn N**. Keeps storage sane for long runs (store deltas, not full state per turn).

Corollary — the engine must be **introspectable**: it emits *structured per-agent state events*
(memory objects, goal updates, belief changes), not just a chat transcript. This is the contract
between engine and UI, and it's the genuinely novel/hard part. The old generative-agents code does
not provide this cleanly.

### Determinism note (pre-empt the trap)
LLM calls are non-deterministic, so re-running from a checkpoint will NOT reproduce the original
future — and that's fine, because **we never re-run the original.** The original branch is already
recorded; we only ever generate *forward* from a branch point with the change applied. This is why
the branch model is correct and synchronous live-editing would have been a nightmare.

## 5. Target architecture

- **Standalone service** — own process, own state store (event-sourced), own web UI + websocket layer.
- **Model layer** — LiteLLM library, provider-agnostic, BYO key via config.
- **Engine** — evolve from the current hand-rolled litellm loop (custom LLM-based speaker selection
  + per-persona response generation); add event-sourced state + checkpointing + structured
  state-event emission. No framework to fight — state (`conversation`, `last_speaker`, cast) is
  already plain, serializable data.
- **UI** — cast-board web app: character cards, live dossiers, checkpoint scrubber/timeline,
  branch tree, token/$ meter, avatar rendering.
- **Interventions** — pause, redirect (edit goal/inject message), inject event, add persona; all as
  branch-from-checkpoint operations.

## 6. Phased plan (deliberately decoupled — don't build fancy engine + fancy UI at once)

- **Phase 0 — Extract & de-couple.** Lift engine out of the OpenClaw agent wrapper. Swap Bedrock
  hardcoding for provider-agnostic config. Standalone runnable, JSON in/out. (Small — engine is
  already close.)
- **Phase 1 — Control-room UI over the existing engine.** Cast board, live watch, per-agent
  dossiers (as rich as current engine state allows), avatars, cost meter. Validate the interaction
  model is what CC/customers actually want, cheaply, before deepening the engine.
- **Phase 1.5 — Post-run analysis layer (read-only).** End-of-run **summary** (consensus /
  dissenters / key ideas), and **scoped aside conversations** over a *completed* run: talk to one
  persona (in-character), to a neutral analyst, or to the whole room — all **read-only**, none
  mutating the canonical run. Introduces the thread/target/mode abstraction that Phase 2 builds on.
  See `docs/PHASE1.5-REQUIREMENTS.md`.
- **Phase 2 — Event-sourced engine + branching + Contribute mode.** Add serializable state,
  checkpointing, structured state events, branch-from-checkpoint interventions, timeline/branch UI.
  **Turns on "Contribute" mode** on the Phase 1.5 thread abstraction: promoting an aside back into
  the canonical conversation, injecting user input, and restarting/continuing the group discussion
  are all branch-from-checkpoint operations (see §6a). Same UI contract.
- **Phase 3 — Release polish.** Install story, BYO-key config UX, docs, license, examples, cost guards.

## 6a. Phase 1.5 & Phase 2 interaction design (added 2026-07-08, CC design session)

The post-run interaction surface is one unifying concept: **scoped threads over a run**, each with a
**target** and a **mode**. This resolves the "private aside vs. feed-it-back-in" tension by making
them the *same feature with a mode toggle* — and it draws the Phase 1.5 / Phase 2 line cleanly.

**Target (who you're talking to):**
- **One persona** — routed through that persona's own system prompt + the full transcript as context; answers in-character (expand a point, defend, self-fact-check).
- **The analyst** — a neutral summarizer/analyst voice over the whole transcript (no persona).
- **The room** — all personas (the group).

**Mode (does it change the canonical record):**
- **Aside (read-only)** — reply lives *in that side-thread only*; the canonical run is never mutated. Multiple asides can coexist and do not see each other. **← all of Phase 1.5.**
- **Contribute (mutating)** — the reply/input is injected back into the canonical conversation, the group sees it, and the sim continues from there. **← Phase 2 (a branch-from-checkpoint).**

**Phase split (the boundary to hold):**
- **Phase 1.5 = Aside mode, any target.** Read-only lenses over a *finished* run: summary, ask-the-analyst, ask-a-persona, ask-the-room. No engine loop, no new canonical turns, no branching. Asides persist attached to the run but are clearly **not canon**.
- **Phase 2 = Contribute mode + restart/continue.** "Promote this aside into the room," "inject my input and let the group continue," and "restart/continue the larger conversation" are the **same branch primitive** (§3.7): load checkpoint → apply the injected message → generate *forward* as a new timeline; the original run stays immutable and recorded.
- **Why design the thread abstraction now (in 1.5) even though Contribute is Phase 2:** so Phase 2 is a clean *add* (flip on a mode + a "bring into conversation" action on the existing thread UI) rather than a rebuild. Data model (threads, target, mode) and UI anticipate Contribute from day one.
- **Boundary rule:** a Phase 1.5 aside is *ephemeral analysis* — it must never alter the run or ripple to other personas. The moment a persona's clarification is meant to become "what everyone now knows," that is Phase 2's immutable branch — do not implement a half-way mutable version in 1.5.
- **Schema note:** MasterControl already added `parent_run_id` / `branch_turn` columns to `runs` in Phase 1, anticipating branching. Phase 2's Contribute/restart uses these; Phase 1.5 does not.

## 7. Public-release scope multipliers (accepted, must be resourced)

Shipping to customers adds, beyond the engine + UI:
- Install/run story — DECIDED: easy-run Python project (pip/pyproject) + Docker container image.
- BYO-key configuration UX + secure key handling.
- Documentation 
- License decision — DECIDED: open source, best current OSS license (PreCog to confirm which).
- Cost visibility / guardrails (live meter + optional spend cap) — stakeholders will watch spend.
- Example simulations / templates so customers get value in 5 minutes.

## 8. Open questions (for PreCog research / CC decision)

1. **Engine substrate:** the current loop is already a purpose-built, serializable litellm loop
   (NO AutoGen despite requirements.txt). Question is now smaller: keep/extend the hand-rolled loop
   (leaning yes) vs adopt a framework. Prior "AutoGen serialization risk" is retracted — no AutoGen
   in the code. Remove unused autogen deps from requirements.txt during extraction.

### 8a. ⚠️ Doc-vs-code divergence in the inherited TheMatrix build (verified 2026-07-08)
The original build's docs (`DESIGN.md`, `ARCHITECTURE.md`, `IMPLEMENTATION_SUMMARY.md`,
`FINAL_REPORT.md`) all describe the system as built on **AutoGen `SelectorGroupChat`**. The shipped
code (`simulate_conversation.py`) does **NOT** import or use AutoGen — orchestration is a hand-rolled
async litellm loop. The build evidently intended AutoGen, documented it as such, then quietly
hand-rolled it (likely AutoGen 0.4 API friction); the "why" was never recorded.
**Directive for PreCog/MasterControl: trust the code, not the inherited docs, on this project.**

### 8b. Should the larger project adopt AutoGen? (assessment 2026-07-08 — recommendation: NO)
What AutoGen 0.4+ genuinely offers that's relevant:
- `save_state()` / `load_state()` on agents/teams — checkpointing primitives.
- Actor-model runtime (`autogen-core`) — async, message-passing, for many parallel agents.
- Human-in-the-loop (`UserProxyAgent`) — built-in intervention hook.
- Tool/function-calling + multi-provider model clients.

Why we still would NOT build on it:
- It provides ZERO of the value-adding parts (memory streams, reflection, goals, introspectable
  state, avatars, branching-as-a-feature). It's orchestration plumbing for the part we've already
  solved (~200 lines). We'd still build every differentiating piece ourselves.
- Its `save_state` serializes AutoGen's internal shape — coupling our event log to their schema/
  versioning. That fights our event-sourcing design (we want OUR state shape), reintroducing the
  "framework holds hidden state" problem with an API on top.
- `UserProxyAgent` HITL is synchronous/blocking — the exact model we rejected in favor of
  run-to-completion + branch-from-checkpoint (§3.6/3.7). Importing a capability we chose not to use.
- Churn risk: AutoGen went through a full 0.2→0.4 rewrite; API still moving. Bad bet for a tool we
  ship to customers and maintain for years.
- Actor runtime is overkill for small, sequential-turn casts.

Where AutoGen IS worth engaging:
- As **reference prior-art** (not a dependency): read its `save_state`/`load_state` design to inform
  OUR checkpoint schema.
- **Reconsider only if** (a) agents need rich in-sim tool-use, or (b) we scale to large parallel
  casts where an actor runtime earns its weight. Neither is v1.

Verdict: AutoGen is an orchestration framework; this is a cognitive-architecture + interaction-
surface project. Keep the hand-rolled loop, mine AutoGen's checkpointing design for ideas, leave the
door open for tool-use/scale later.
2. **State/storage backend:** SQLite (embeddable, ships easily) vs Postgres vs event-log files.
   Leaning SQLite for a distributable single-node tool.
3. **Web stack:** front-end framework + real-time transport (websockets/SSE). TBD — open for research.
4. **Image-gen provider for avatars:** ✅ DECIDED (CC 2026-07-08) — **must be an Amazon Bedrock
   image model** (e.g. Titan Image Generator v2 / Nova Canvas — PreCog to confirm best fit + BYO-key
   fit). Keep optional so a run works without image-gen (fallback to initials/color).
5. **Packaging/distribution model** (§7) — ✅ DECIDED (CC 2026-07-08) — **ship as a Python project
   that runs easily locally (pip/pyproject, `python -m` entrypoint), AND package into a Docker
   container** so teams can run it as-is. PreCog to research the cleanest way to do both from one
   codebase (pyproject + Dockerfile, single-node).
6. **License** — ✅ DECIDED (CC 2026-07-08) — **open source; use whatever is currently considered
   the best/most-standard OSS license.** PreCog to determine current consensus (MIT vs Apache-2.0,
   patent-grant considerations for a customer-facing tool).

## 9. Explicitly cut (do not resurrect)

- Spatial map / sprites / movement / pathfinding.
- Hard OpenClaw dependency of any kind.
- Synchronous, UI-blocking simulation stepping.
- Re-running/reproducing an existing branch (unnecessary given the branch model).
