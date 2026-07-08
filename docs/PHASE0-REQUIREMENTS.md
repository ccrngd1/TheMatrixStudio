# Requirements — TheMatrix Simulation Studio, Phase 0

**Project:** TheMatrix Simulation Studio (working title)
**Phase:** 0 — Extract & De-couple
**Owner:** CC
**Author:** Main (CABAL)
**Date:** 2026-07-08
**Status:** DRAFT — awaiting CC sign-off before MasterControl handoff
**Spec:** `~/wiki/projects/matrix-sim-studio.md`
**Research:** `shared/builder-pipeline/research/matrix-sim-studio-phase0-research.md`
**Engine source of truth:** `/root/.openclaw/workspace-thematrix/thematrix/simulate_conversation.py` (hand-rolled async litellm loop, ~200 LOC, NO AutoGen — trust the code, not the inherited docs)

---

## Plain-English Summary

Take the existing TheMatrix simulation engine — a small, working Python script that runs multi-agent
conversations through LiteLLM — and turn it into a clean, standalone Python project that anyone can
`pip install` and run, or run as a Docker container. No OpenClaw dependency, no hardcoded model,
provider-agnostic via BYO API key. This phase ships **no UI and no new simulation features** — it is
purely the foundation: a properly packaged, provider-agnostic, standalone version of what already
works, plus the storage skeleton (SQLite event log) and stubs that Phases 1–3 build on.

**Why this phase first:** the engine is already ~90% standalone (JSON in/out, no memory coupling). We
de-risk everything downstream by getting packaging, config, and the storage contract right before we
invest in the cast-board UI (Phase 1) or event-sourced branching (Phase 2).

---

## Goal

A standalone, distributable Python project (`matrix-sim-studio`) that:
- runs the existing simulation engine with **zero OpenClaw coupling**,
- selects its model provider from **config/env (BYO key)**, not hardcoded Bedrock,
- runs both as an easy local Python app **and** a Docker container,
- persists runs to a **SQLite event-sourced store** (schema laid down; full branching is Phase 2),
- ships under **Apache-2.0**.

---

## In Scope (Phase 0)

1. **Repo/package skeleton** — new standalone project, `pyproject.toml` as the single source of truth.
   Layout per research doc §3:
   ```
   matrix-sim-studio/
   ├── pyproject.toml
   ├── Dockerfile
   ├── .dockerignore
   ├── .env.example
   ├── LICENSE            (Apache-2.0)
   ├── NOTICE
   ├── README.md
   ├── matrix_studio/
   │   ├── __main__.py    (python -m matrix_studio entrypoint)
   │   ├── engine/        (extracted from simulate_conversation.py)
   │   ├── storage/       (SQLite layer)
   │   ├── settings.py    (Pydantic BaseSettings)
   │   └── state.py       (Pydantic state models)
   └── examples/          (starter simulation JSON configs, carried over)
   ```

2. **Extract the engine** — lift `simulate_conversation.py` into `matrix_studio/engine/` as a standalone
   module. Remove any OpenClaw-agent-wrapper coupling. Preserve existing behavior:
   `_select_next_speaker()` + `_generate_response()`, two litellm calls per turn, `max_messages`
   termination. JSON request-in / result-out must still work (carry over `example_request.json`).

3. **Provider-agnostic config** — replace the hardcoded default
   `bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0` with a Pydantic `Settings` object
   (`settings.py`) that reads, in precedence order: **env vars > `.env` file > `config.json` defaults**.
   Must accept any LiteLLM model string (e.g. `openai/gpt-4o`, `anthropic/...`, `bedrock/...`,
   `ollama/...`). Ship `.env.example` with all keys, no real values.

4. **Entrypoint** — `python -m matrix_studio` (and a `matrix-studio` console script) that runs a
   simulation from a config/request file and writes results. CLI args: input request path, output path,
   `--max-messages` override. (FastAPI/WebSocket server is Phase 1 — Phase 0 may include a **stub**
   `api/` module but no functional UI is required.)

5. **SQLite storage layer** — implement the 3-table event-sourced schema (research §5) with `aiosqlite`,
   WAL mode: `runs`, `events`, `snapshots`. Engine emits structured events (`sim.started`,
   `speaker.selected`, `agent.response` with token/cost, `sim.completed`, etc. per the event registry)
   into `events` as it runs. A run is recoverable from the DB. **Branch operations and checkpoint-restore
   logic are Phase 2** — Phase 0 only lays the tables + append path + one full snapshot at completion.

6. **Pydantic state models** (`state.py`) — `SimSnapshot`, `AgentState`, `MemoryItem`, each carrying
   `type` + `schema_version` fields (prior-art pattern, research §6). Used for the completion snapshot.

7. **Avatar stub** — `matrix_studio/avatar.py` with a direct **boto3 Nova Canvas** call
   (`amazon.nova-canvas-v1:0`, `bedrock-runtime.invoke_model`). NOTE: LiteLLM does not route Bedrock
   image models — must be boto3. **Graceful fallback:** if no AWS creds/region present, return
   `portrait: null` and do not error. Wiring avatars into a UI is Phase 1; Phase 0 just needs the
   callable + fallback.

8. **Remove unused AutoGen deps** — strip `autogen-agentchat` / `autogen-ext` from the inherited
   `requirements.txt`; they are never imported. New deps live in `pyproject.toml` (research §3 list).

9. **Dockerfile** — multi-stage. Phase 0 has no frontend yet, so the Node build stage may be a
   placeholder/no-op; the Python stage must produce a working `python -m matrix_studio` image.
   `docker run --env-file .env ...` must run a simulation.

10. **Licensing** — `LICENSE` (Apache-2.0 full text), `NOTICE` (project + copyright), and
    `SPDX-License-Identifier: Apache-2.0` headers on source files.

---

## Out of Scope (defer to later phases)

- Cast-board web UI, character cards, live dossiers, thought feeds — **Phase 1**.
- FastAPI server + WebSocket streaming as a *working* surface — **Phase 1** (stub only in Phase 0).
- Event-sourced **branching**, checkpoint-restore, intervention/timeline — **Phase 2**.
- Real avatar rendering in a UI — **Phase 1** (Phase 0 = callable + fallback only).
- Natural-conclusion detection, max-rounds-per-persona, timeout enforcement (never implemented; not
  this phase).
- Cost meter UI, spend caps, example templates polish — **Phase 3**.

---

## Acceptance Criteria (how we verify "done")

- [ ] `pip install .` in a clean venv succeeds; `python -m matrix_studio --help` runs.
- [ ] `grep -ri "openclaw" matrix_studio/` returns **nothing** (zero OpenClaw coupling).
- [ ] `grep -ri "autogen" .` returns nothing outside a `venv/` (deps removed, no imports).
- [ ] Running a sample request produces a valid result JSON **and** persists a `runs` row + `events`
      rows + one completion `snapshot` in the SQLite DB.
- [ ] Model provider is selected from env/`.env`/config — verified working with **at least two
      different providers** (e.g. a Bedrock model and an OpenAI model), no code change between them.
- [ ] `docker build .` succeeds and `docker run --env-file .env <image>` runs a simulation to completion.
- [ ] Avatar module: with AWS creds → returns a base64 image; without creds → returns `null`, no crash.
- [ ] `LICENSE` (Apache-2.0) + `NOTICE` present; SPDX headers on source files.
- [ ] `README.md` documents local run, Docker run, and BYO-key config.
- [ ] Existing engine behavior preserved (same speaker-selection + response loop, `max_messages`
      termination) — a carried-over example request yields a coherent multi-turn transcript.

---

## Constraints & Notes

- **Trust the code, not the inherited docs** on the engine — those docs were corrected 2026-07-08 but
  the code is authoritative.
- **Keep avatar generation optional** — the tool must run fully without image-gen or AWS.
- **No new simulation intelligence this phase** — resist adding memory-stream/reflection/goals features;
  those ride on the Phase 2 event-sourced engine. Phase 0 is plumbing + packaging only.
- **Nova Canvas pricing is MEDIUM-confidence** in research — do not bake cost numbers into user-facing
  docs without verifying on the AWS Bedrock pricing page.
- Verify the exact Nova Canvas response field name against live Bedrock API docs at implementation time.

---

## Handoff

On CC sign-off → MasterControl (Stage 5). MasterControl owns implementation-level task decomposition
and Claude Code dispatch. This doc defines **what** and **acceptance**, not the step-by-step build.
Build target dir: `/root/projects/protoGen/matrix-sim-studio/` (per MasterControl project convention),
or a dedicated standalone repo if CC prefers it live outside protoGen given it's a shippable product —
**open question for CC.**
