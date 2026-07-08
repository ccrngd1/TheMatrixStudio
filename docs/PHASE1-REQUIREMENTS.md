# Requirements — TheMatrix Simulation Studio, Phase 1

**Project:** TheMatrix Simulation Studio (working title)
**Phase:** 1 — Control-Room UI over the existing engine
**Owner:** CC
**Author:** Main (CABAL)
**Date:** 2026-07-08
**Status:** DRAFT — awaiting CC sign-off before MasterControl handoff
**Spec:** `~/wiki/projects/matrix-sim-studio.md` (§3, §6 Phase 1)
**Builds on:** Phase 0 (COMPLETE, verified live 2026-07-08 against real Bedrock — text engine, SQLite event store, cold-start prompt, and Stability SD3.5 avatars all working)

---

## Plain-English Summary

Put a watchable, demo-ready web UI on top of the Phase 0 engine. A user opens a browser,
defines (or loads) a cast of persona agents and a topic, hits **Run**, and watches the
conversation unfold live as a "control room": a **cast board** of character cards (avatar +
persona + goals), a **live-scrolling conversation feed**, a highlight of **who's speaking now**,
and a **running token/$ cost meter**. Clicking a card opens that agent's **dossier** (persona,
goals, its own messages, its token/cost). Past runs are browsable and replayable.

This phase is **read-only observation**. No pausing, no editing, no branching — the engine still
runs to completion at full speed (Phase 0 behavior), and the UI simply *tails* what it emits. The
goal is to validate the interaction model — "is watching a cast board the experience CC and his
customers actually want?" — **cheaply, before** Phase 2 deepens the engine with event-sourcing,
checkpoints, and branch-from-checkpoint interventions.

**Why this phase now:** Phase 0 already emits structured events per turn to SQLite
(`sim.started`, `speaker.selected`, `agent.response` with tokens/cost, `sim.completed`). That is
exactly the stream a live UI needs. We add the thinnest possible live-emit seam on the engine, a
FastAPI + WebSocket server, and a single-page web app — no new *simulation* capabilities.

---

## Goal

A standalone web application, shipped in the same package/container as Phase 0, that:
- lets a user **create/load a simulation** (topic + cast + config) and start it from the browser,
- **streams the run live** (messages, active speaker, avatars, cost) with no user-side blocking,
- renders a **cast board** with per-run **generated avatars** (Stability SD3.5, already wired),
- shows a **per-agent dossier** limited to what current engine state exposes,
- shows a **live cost/token meter**,
- lets a user **browse and replay past runs** from the SQLite store,
- runs both `python -m matrix_studio serve` (local) and in the existing Docker image (one container),
- adds **zero** OpenClaw coupling and **no** new simulation features (no branching/intervention).

---

## Proposed Web Stack (one open decision for CC — see Handoff)

Recommended, chosen for a single-node, Docker-able, customer-facing showcase that contributors can maintain:

- **Backend:** FastAPI + Uvicorn (async — matches the async engine; the Phase 0 `api/` stub already anticipates it).
- **Real-time transport:** **WebSocket** for the live event stream. (SSE is simpler and sufficient for read-only Phase 1, but WebSocket is forward-compatible with Phase 2 interventions, which are bidirectional. Chosen to avoid a transport rewrite in Phase 2.)
- **Frontend:** **React + Vite + TypeScript + Tailwind CSS**, built to static assets and served by FastAPI from the same process → one container, one port. This makes the Dockerfile's existing placeholder Node build stage real.
- **State/store:** reuse the Phase 0 SQLite event store, with **additive, backward-compatible columns only** on the `runs` table for run name/description (§10). No changes to the event schema; existing rows/queries keep working.

---

## In Scope (Phase 1)

### 1. Engine live-emit seam (minimal change)
- Add an optional async callback to `run_simulation(...)`, e.g. `on_event: Callable[[dict], Awaitable[None]] | None = None`, invoked with each structured event **at the same points** it already calls `db.append_event(...)` (and for `avatar.ready`, see §4). Persistence behavior is unchanged; this only *also* pushes events to a live subscriber.
- Must NOT change the existing JSON-in/JSON-out contract, the event schema, or Phase 0 CLI behavior. All Phase 0 tests must still pass.
- No event-sourcing, no snapshots-per-turn, no checkpoint/branch machinery — that is Phase 2.

### 2. Backend API (FastAPI)
- `POST /api/runs` — start a new run from a request body (same shape as the CLI request JSON: `topic`, `cast[]`, `config{max_messages, generate_avatars}`, optional `model`, optional `name`, optional `description`). If `name` is omitted, the server generates a memorable unique one (§10). Returns `run_id` (and the resolved `name`) immediately; the simulation runs in a background task. Never blocks the HTTP response on completion.
- `GET /api/runs` — list past runs (from the `runs` table): id, **name, description**, topic, status, turn count, total cost, timestamps. Supports a `?q=` filter matching name/description/topic.
- `GET /api/runs/{ref}` — run metadata + final result (if complete). `{ref}` may be the `run_id` **or** the memorable name.
- `GET /api/runs/{ref}/events?after_seq=` — paged historical events for replay/late-join.
- `WS /api/runs/{ref}/stream` — live event stream. On connect, replays existing events for that run (so a late joiner catches up), then pushes new events as they occur, ending with `sim.completed`/`sim.failed`.
- `GET /api/name/suggest?topic=` *(optional helper)* — returns a suggested memorable name (and description) for the new-run form; purely a convenience, the client may also edit freely.
- `GET /api/models` (or static config) — the model string(s) available to select in the new-run form. Keys come from **server-side env/config** (Phase 0 `.env`), NOT the browser (full BYO-key browser UX is Phase 3).
- Serve the built frontend static assets from the same app.
- Serve the built frontend static assets from the same app.

### 3. Cast board (the centerpiece)
- One **character card** per agent: generated **avatar**, name, one-line persona summary, current goal(s), and a compact live indicator (speaking / idle) plus that agent's running message count and token/$ subtotal.
- Card visually highlights the **active speaker** as `speaker.selected` events arrive.
- Cards populate progressively as avatars finish (see §4) — a card renders immediately with a **placeholder** (initials on a deterministic color) and swaps to the portrait when `avatar.ready` fires.

### 4. Avatars (already wired in Phase 0 — make them live + parallel)
- Generate avatars **in parallel** at run start (Phase 0 currently generates them before the loop; parallelize with `asyncio.gather`), emitting an `avatar.ready` event `{agent_name, portrait_b64}` per avatar as it completes so cards fill in without blocking the conversation start.
- **Graceful fallback is mandatory:** if avatars are disabled or generation returns `None` (no image model access, content filter, no creds), the UI shows the initials/color placeholder and the run proceeds normally. Avatars are eye-candy, never a hard dependency.
- Uses the Phase 0 avatar module as-is (`stability.sd3-5-large-v1:0`, `AVATAR_REGION=us-west-2`, bearer-token auth). No changes needed beyond parallelization + the `avatar.ready` emit.

### 5. Live conversation feed
- Chat-style transcript that appends each message as `agent.response` events arrive, labeled by speaker, in turn order, auto-scrolling (with a "pause auto-scroll" affordance).
- Shows a subtle "thinking…" state between `speaker.selected` and that agent's `agent.response`.
- Handles the cold-start opener naturally (Phase 0 fix already ensures the first message reads as an opening remark).

#### 5a. Playback controls (UI-layer ONLY — must never gate generation)
The backend always runs full-speed to completion; events are buffered/persisted regardless of what the viewer does. The UI controls only how buffered events are *revealed*:
- **Default = live auto-play** — reveal events as they stream in (preserves the "watch it unfold" appeal).
- **Pause / resume** — pause stops *advancing the feed*; the engine keeps running and events keep buffering behind the pause point. Resume either jumps to live ("catch up") or continues auto-play from where the viewer paused (offer both).
- **Step forward one turn** — advance the feed by a single turn through already-buffered/persisted events.
- **Optional reveal-speed control** — pace at which buffered turns are revealed during auto-play.
- **Hard rule:** none of these controls send anything to the engine or alter generation timing, ordering, or cost. They operate purely on the client-side view of the event stream (same mechanism as run replay, §9, applied to a still-running buffered run). MasterControl must NOT implement a "next turn" button that blocks or paces generation — synchronous/UI-blocking stepping is explicitly cut (project spec §9).

### 6. Per-agent dossier (scoped to current engine state — read the constraint)
Clicking a card opens a dossier panel showing **only what the current engine actually exposes**:
- avatar, name, full persona text, goal list;
- that agent's messages (filtered from the transcript);
- that agent's token in/out and cumulative $ cost.
- **Explicitly deferred to Phase 2** (do NOT fake these): memory stream, reflections/beliefs, relationships graph, goal hierarchy, and the "why did it say that?" trace. The engine does not emit this structured state yet — that is the core of Phase 2. If a field has no real data, it must be **absent or labeled "available in a later version,"** never fabricated. (Honesty gate — same standard as Phase 0.)

### 7. Cost / token meter
- Persistent live meter: cumulative **$ cost** and **total tokens** for the run, updating as `agent.response` events carry per-turn `cost_usd`/tokens. Optional per-model/per-agent breakdown.
- No spend **cap/guardrail** enforcement in Phase 1 (that's a Phase 3 release-polish item) — display only. May show a soft warning banner past a configurable display threshold, but must not halt a run.

### 8. New-run / cast builder form
- A form to define a run without hand-editing JSON: topic field; add/remove/edit personas (name, persona text, goals); `max_messages`; toggle avatars; select model from `GET /api/models`.
- **Run name + description** (see §10): the form shows an auto-suggested memorable name (editable) and an auto-suggested description (editable) so the run is easy to find/reload later.
- "Load example" button that populates the form from the shipped `examples/*.json`.
- Submitting calls `POST /api/runs` and transitions to the live view.

### 9. Run history & replay
- A list of past runs (`GET /api/runs`) shown by **memorable name + description** (not raw UUIDs), with topic, status, turn count, cost, and timestamp; **searchable/filterable by name**.
- Selecting a run **loads/replays** it into the same cast-board/feed UI (with the playback controls of §5a) from stored events (`GET /api/runs/{id}/events`), so a completed run looks and paces the same as a live one. Read-only.
- A run is reloadable by its memorable name as well as its id (see §10).

### 10. Named, described runs (reload/lookup key)
Every simulation gets a stable, human-friendly identity so users can pull it back up in the UI:
- **Memorable name** — a short, memorable, **unique** name (e.g. a two-word codename like `amber-forum`, or a slug derived from the topic). Auto-generated at run creation; **editable** by the user in the new-run form. Uniqueness enforced (append a suffix/disambiguator if a collision would occur).
- **Description** — a one-line human description (auto-suggested from the topic/cast, editable) shown in the history list and dossier header.
- The internal `run_id` (UUID) remains the primary key and the WebSocket/stream key; the memorable name is an additional stable, unique lookup handle. `GET /api/runs/{ref}` accepts **either** the id or the name.
- **Storage:** additive columns on the existing `runs` table — `name` (unique), `description`, and (if useful) a normalized `slug`. This is the one deviation from "no schema changes": additive, backward-compatible columns only; existing rows/queries must keep working (nullable or backfilled).

### 11. Packaging (extend Phase 0, don't fork it)
- New entrypoint `python -m matrix_studio serve [--host --port]` starting the FastAPI app; keep the Phase 0 file-in/out CLI working unchanged (make the CLI subcommand-based: `run` = existing behavior, `serve` = new).
- Make the Dockerfile's placeholder Node stage real: build the frontend, copy static assets into the final image; `docker run -p ... --env-file .env <image>` serves the UI. Document local + Docker in the README.

---

## Out of Scope (defer)

**Phase 2 (engine deepening):** event-sourced state, per-turn full snapshots, checkpoint scrubber/timeline, branch tree, and ALL interventions (pause, redirect, edit goal, inject event/message, add persona mid-run). Phase 1 is run-to-completion + observe only. The UI should be *laid out* so Phase 2 controls can be added, but no such control is built.

**Phase 2 (rich dossier):** memory streams, reflections/beliefs, relationship graph, goal hierarchy, "why did it say that?" trace — requires structured per-agent state events the engine does not yet emit.

**Phase 3 (release polish):** in-browser BYO-key entry + secure key handling (Phase 1 keys come from server env), enforced spend caps, full docs site, example gallery/templates UX, license/NOTICE polish beyond what Phase 0 shipped.

**Also cut (per project spec §9):** any spatial map/sprites; any OpenClaw dependency; synchronous/UI-blocking stepping; re-running/reproducing an existing branch.

---

## UI ⇄ Engine Contract (Phase 0 event/result shapes the UI consumes)

**Events** (SQLite `events` table; each has `run_id, turn, seq, event_type, agent_name, payload, created_at`). The live stream forwards these plus `avatar.ready`:
- `sim.started` — payload: `{topic, cast: [names], config}`
- `avatar.ready` *(new in Phase 1)* — payload: `{agent_name, portrait_b64 | null}`
- `speaker.selected` — `agent_name` set — payload: `{turn}`
- `agent.response` — `agent_name` set — payload: `{content, tokens_in, tokens_out, cost_usd}`
- `sim.completed` — payload: `{total_turns, total_cost_usd}`
- `sim.failed` — payload: `{error}` (from the existing failure path)

**Result JSON** (`GET /api/runs/{id}`): `{run_id, status, topic, conversation: [{speaker, content}], agents: {name: {name, persona, goals, total_tokens_in, total_tokens_out, total_cost_usd, portrait}}, total_turns, total_cost_usd}`.

> Contract rule: Phase 1 must consume this shape **as-is** and must not silently change the event schema. If a field is genuinely needed for the UI and missing, add it as an **additive** event/field and note it here — do not repurpose existing fields.

---

## Acceptance Criteria (how we verify "done")

- [ ] `python -m matrix_studio run <req.json>` still behaves exactly as Phase 0 (regression); all Phase 0 tests pass.
- [ ] `python -m matrix_studio serve` starts the app; opening the browser shows the UI.
- [ ] New-run form (or example load) starts a run via `POST /api/runs` and returns a `run_id` without blocking on completion.
- [ ] Live view streams over WebSocket: messages append in order, active speaker highlights, cost meter increments — all **as the run progresses**, not only at the end.
- [ ] **Playback controls are UI-only:** pausing/stepping/slowing the feed does NOT pause or slow the engine — verify the run reaches completion (events keep persisting) while the viewer is paused, and "catch up" jumps to live. No "next turn" control gates generation.
- [ ] **Named runs:** a new run gets a memorable, unique name (auto-generated if not supplied) and description; the run is reloadable in the UI by that name via `GET /api/runs/{name}`, and the history list is filterable by name.
- [ ] Cast cards render immediately with placeholders and swap to real portraits as `avatar.ready` fires; a run with avatars **disabled or failing** still completes and displays placeholders (no crash).
- [ ] Clicking a card shows the scoped dossier (persona, goals, that agent's messages, its token/$); deferred fields are absent/labeled, **never fabricated**.
- [ ] A late-joining WebSocket client (connect mid-run) catches up via replayed events then continues live.
- [ ] Run history lists prior runs; selecting a completed run replays it into the same UI.
- [ ] `docker build .` succeeds (frontend built in the image) and `docker run -p <port> --env-file .env <image>` serves the working UI. **If Docker is unavailable in the build environment, say so explicitly — do not claim it passed.**
- [ ] Live end-to-end run performed against a real provider (Bedrock via the configured key). State clearly which checks were live vs mocked. Do NOT fabricate cost/latency/usage numbers.
- [ ] `grep -ri openclaw matrix_studio/ frontend/src` returns nothing; no new OpenClaw coupling.

---

## Constraints & Notes

- **Honesty gate (carried from Phase 0):** no fabricated metrics, no claimed production/deployment history, no invented dossier data. README/summary describe what the code does and what tests verify. Live vs mocked must be stated.
- **Keys stay server-side.** Phase 1 reads provider keys from server env/`.env` (git-ignored). The browser never handles raw keys. `.env` must remain git-ignored; never commit secrets.
- **Additive only on the engine.** The live-emit seam is an optional callback; it must not alter Phase 0 semantics, the event schema, or persisted output.
- **Avatars are optional eye-candy** — every avatar path must degrade to a placeholder without failing the run.
- **Model default** is `bedrock/global.anthropic.claude-haiku-4-5-...` (Phase 0 fix); the EOL `claude-3-5-sonnet-20241022` must not reappear anywhere.
- **Tests:** add a backend test suite (API routes, WS connect/replay/stream, run lifecycle with a mocked engine) and minimal frontend component/smoke tests. Keep all engine/avatar tests mocked (the real env carries a Bedrock key — unmocked tests would make live calls).
- **License/SPDX:** new source files carry the Apache-2.0 SPDX header, consistent with Phase 0.

---

## Handoff

- **Blocking decision for CC (one item):** confirm the **web stack** (FastAPI + WebSocket + React/Vite/TS/Tailwind) — or ask for PreCog research on alternatives first. Everything else in this doc is specified and ready.
- **Optional research (non-blocking):** PreCog could confirm the OSS license choice (spec §8.6) and Docker single-image front+back best practice (spec §8.5) in parallel; neither blocks starting the build.
- **On sign-off:** Main writes the MasterControl brief (repo `/root/projects/matrix-sim-studio`, this doc as authoritative acceptance criteria), MasterControl builds Phase 1 on top of the existing Phase 0 code, commits (does not push), and reports a completion report with each acceptance checkbox marked PASS / FAIL / NOT-VERIFIABLE-HERE and live-vs-mocked noted. Main reviews before relaying to CC.
- **Gate note:** unlike Phase 0 (whose requirements doc was still marked DRAFT when the build ran), **do not hand off Phase 1 until CC signs off on this doc.**
