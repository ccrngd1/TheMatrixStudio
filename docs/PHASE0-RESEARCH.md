# TheMatrix Simulation Studio — Phase 0 Research
**Prepared by:** PreCog (CABAL research facet)  
**Date:** 2026-07-08  
**For:** Main → CC Phase 0 requirements doc  
**Source of truth for engine:** `simulate_conversation.py` (hand-rolled async litellm loop — ~200 LOC, no AutoGen)

---

## Decisions Summary

| # | Question | Recommendation |
|---|----------|----------------|
| 1 | Web stack + real-time transport | **FastAPI** (backend) + **React + Vite** (frontend) + **WebSockets** (real-time) |
| 2 | Bedrock image model for avatars | **Amazon Nova Canvas** (`amazon.nova-canvas-v1:0`) via direct **boto3** call |
| 3 | Packaging pattern (Python + Docker) | **pyproject.toml** as single source of truth; `pip install .` locally; multi-stage **Dockerfile** installs from same pyproject; secrets via `.env` + `python-dotenv` |
| 4 | OSS license | **Apache-2.0** |
| 5 | Storage backend | **Confirm SQLite** (with WAL + `aiosqlite`); tables: `runs`, `events`, `snapshots` |
| 6 | AutoGen save_state prior art | Adopt type-discriminated Pydantic state models with `type` + `version` fields; hierarchical checkpoint shape |

---

## 1. Web Stack + Real-Time Transport

### What the UI demands
The cast-board needs **two data flows**:
- **Server → client:** live simulation turn events, per-agent thought feeds, token/cost meter ticks, checkpoint completion signals
- **Client → server:** pause commands, branch requests, persona injection, goal edits

SSE handles only server→client push; REST handles client→server but requires polling for completions. **WebSockets handle both directions natively** — one persistent connection for the full interaction loop. Given the UI has both push feeds *and* interactive interventions, WebSockets are the right primitive.

### Backend: FastAPI
- Native async (`asyncio` + `async def`), fits perfectly with the existing `asyncio.run(main())` engine loop
- Built-in WebSocket support (`websockets` under the hood), native file responses for serving the static frontend build
- Clean dependency injection, Pydantic models (shared with state schema)
- Ships as a Python package, zero extra deps beyond `fastapi` + `uvicorn[standard]`
- FastAPI can serve the compiled frontend static build from `matrix_studio/static/` — no separate nginx/node server in Docker

### Frontend: React + Vite
- Most maintainable small-team choice in 2025/26: largest ecosystem, most component libraries, most hiring pool
- Vite gives sub-second HMR and a clean `dist/` bundle that drops into FastAPI's static serving
- Alternative worth considering: **SvelteKit** — leaner bundles, reactive-by-default (better for streaming feeds), but smaller ecosystem and less common for data-heavy dashboards
- Character card grids, branch tree visualization (consider `reactflow` or `d3` for the DAG), live scrolling thought feeds are all well-served by React component ecosystem
- For the branch DAG specifically: `reactflow` (MIT license, React-native) or `d3-dag` are both viable; `reactflow` is simpler to wire up

### Transport protocol
- **WebSockets** (not SSE) — bidirectional, single connection, lower overhead for the cast-board interaction model
- Message envelope: `{type: "sim_event" | "agent_state" | "cost_tick" | "checkpoint" | "error", payload: {...}, run_id, turn}`
- FastAPI WebSocket endpoint: `/ws/run/{run_id}` — engine pushes events to the WS connection; client sends intervention commands on same socket

### Summary
`FastAPI + React/Vite + WebSockets`. Frontend static build bundled into the Python package under `matrix_studio/static/`; FastAPI `mount()` serves it. No separate node server in production or Docker.

---

## 2. Bedrock Image Model: Titan v2 vs Nova Canvas

### The options
| | Titan Image Generator v2 | Nova Canvas v1 |
|--|--------------------------|----------------|
| Model ID | `amazon.titan-image-generator-v2:0` | `amazon.nova-canvas-v1:0` |
| Released | 2024-Q1 | 2024-Q4 (Nova family launch) |
| Architecture | Titan (older gen) | Nova (newer diffusion architecture) |
| Portrait quality | Adequate, tends toward generic faces | Noticeably better photorealism, richer detail |
| Cost (512×512) | ~$0.008/image | ~$0.040/image (5× more) |
| Latency | ~5–8s | ~6–10s |
| API shape | `invoke_model` via bedrock-runtime | `invoke_model` via bedrock-runtime (same call pattern) |
| Style control | Basic prompt, seed | Prompt, seed, color palette conditioning, background removal |

_Pricing note: AWS pricing pages are authoritative; figures above are from published AWS Bedrock pricing as of 2025. Verify at [https://aws.amazon.com/bedrock/pricing/](https://aws.amazon.com/bedrock/pricing/) before billing assumptions._

### LiteLLM compatibility
**LiteLLM does NOT route Bedrock image-generation models.** LiteLLM's Bedrock provider handles only text/chat completion models (`invoke_model` for text). Image generation on Bedrock requires:
1. A direct **`boto3` `bedrock-runtime.invoke_model()`** call
2. Model-specific JSON request body (different schema for Titan vs Nova Canvas)
3. Response body is base64-encoded image data, not a chat message

This is fine — the engine already depends on `boto3>=1.34.0` (in `requirements.txt`). A thin `avatar.py` module using boto3 directly keeps the dependency clean and is already present.

### Recommendation: **Amazon Nova Canvas**

For avatar portraits that ship in a customer-facing demo tool:
- Quality difference is meaningful — portraits are the first thing stakeholders see on the cast board
- The 5× cost premium is negligible at avatar generation volume: a 6-persona cast generates **6 images per run**, totaling ~$0.24/run vs ~$0.048/run — a trivial difference for a demo tool
- Nova Canvas's color palette conditioning is useful: the LLM can specify a character's palette (hair color, clothing) and Nova Canvas respects it more reliably
- Spec §3.5 explicitly marks avatars as "purely eye-candy" but also notes they're generated fresh per run — quality matters for first impressions

Keep avatar generation **optional** per spec: if no AWS credentials are present, fall back to initials/color avatar in the frontend. The backend should emit a `"portrait": null` field in persona state when image-gen is unavailable.

### Nova Canvas API shape (boto3)
```python
import boto3, json, base64

client = boto3.client("bedrock-runtime", region_name="us-east-1")

body = json.dumps({
    "taskType": "TEXT_IMAGE",
    "textToImageParams": {
        "text": "<portrait prompt>",
        "negativeText": "deformed, low quality, cartoon"
    },
    "imageGenerationConfig": {
        "numberOfImages": 1,
        "height": 512,
        "width": 512,
        "cfgScale": 8.0,
        "seed": 42
    }
})

response = client.invoke_model(
    modelId="amazon.nova-canvas-v1:0",
    body=body,
    contentType="application/json",
    accept="application/json"
)
result = json.loads(response["body"].read())
image_b64 = result["images"][0]  # base64 PNG
```
_Note: the exact response field name may differ; check the Bedrock API docs for the current Nova Canvas response schema during implementation._

---

## 3. Packaging Pattern: Python + Docker from One Codebase

### Single source of truth: `pyproject.toml`

The cleanest pattern for shipping both `pip install` and Docker from the same codebase is to let **`pyproject.toml` be the only package definition**. Docker installs from it; local users install from it; CI builds from it.

**Directory layout:**
```
matrix-sim-studio/
├── pyproject.toml          ← single source of truth
├── Dockerfile              ← multi-stage, installs via pip
├── .dockerignore
├── .env.example            ← committed template (no secrets)
├── matrix_studio/
│   ├── __main__.py         ← `python -m matrix_studio` entrypoint
│   ├── engine/             ← evolved from simulate_conversation.py
│   ├── api/                ← FastAPI app + WebSocket handlers
│   ├── static/             ← compiled React/Vite build (committed or built at install time)
│   └── storage/            ← SQLite layer
├── frontend/               ← React/Vite source (separate from package)
│   ├── package.json
│   └── src/
└── examples/               ← starter simulation JSON configs
```

**`pyproject.toml` key sections:**
```toml
[project]
name = "matrix-sim-studio"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "litellm>=1.40.0",
    "boto3>=1.34.0",
    "fastapi>=0.110.0",
    "uvicorn[standard]>=0.29.0",
    "aiosqlite>=0.20.0",
    "pydantic>=2.0.0",
    "python-dotenv>=1.0.0",
]

[project.scripts]
matrix-studio = "matrix_studio.__main__:main"

[tool.setuptools.package-data]
matrix_studio = ["static/**/*"]   # bundle compiled frontend
```

**Local run:**
```bash
pip install .
python -m matrix_studio         # or: matrix-studio
# or dev mode:
pip install -e ".[dev]"
```

**Dockerfile (multi-stage — keeps image lean):**
```dockerfile
# Stage 1: build the React frontend
FROM node:20-alpine AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build              # outputs to /app/frontend/dist

# Stage 2: Python app
FROM python:3.11-slim AS app
WORKDIR /app
COPY pyproject.toml .
COPY matrix_studio/ matrix_studio/
# Copy compiled frontend into package static dir
COPY --from=frontend-builder /app/frontend/dist matrix_studio/static/
RUN pip install --no-cache-dir .
EXPOSE 8000
CMD ["python", "-m", "matrix_studio", "--host", "0.0.0.0", "--port", "8000"]
```

### Secrets / BYO-key config

Pattern: **`.env` file + environment variables**, loaded at startup via `python-dotenv`.

```bash
# .env.example (committed — no real values)
LITELLM_MODEL=bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION=us-east-1
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
MATRIX_PORT=8000
MATRIX_DATA_DIR=./data
```

Startup priority: env vars > `.env` file > `config.json` defaults. In Docker: `docker run --env-file .env matrix-studio` or `-e KEY=val`. Never bake secrets into the image.

The existing `config.json` pattern in the engine (provider config, model ID, temperature) should be generalized into a `settings.py` using Pydantic `BaseSettings` — reads from env, `.env`, or config file, with clear precedence.

---

## 4. OSS License: MIT vs Apache-2.0

### The distinction that matters for a customer-facing tool

Both licenses are permissive and open source. The key difference:

| | MIT | Apache-2.0 |
|--|-----|-----------|
| Patent grant | None (silent) | Explicit: contributors grant users a license to any patents covering their contribution |
| Patent retaliation | None | Yes: if you sue any contributor for patent infringement related to the project, your license terminates |
| Attribution | Copyright notice | Copyright notice + NOTICE file |
| Complexity | ~170 words | ~1100 words |
| Enterprise acceptance | Universal | Universal (preferred by legal teams) |

### Recommendation: **Apache-2.0**

For a **customer-facing tool that CC ships to enterprise customers**:
- Enterprise legal teams routinely prefer Apache-2.0 over MIT because the explicit patent grant closes a real (if rarely exercised) legal exposure — some legal review gates will approve Apache-2.0 automatically but flag MIT as "no patent grant, review required"
- Patent retaliation clause protects the project and its users from patent trolling scenarios
- The added complexity (NOTICE file) is trivial
- Major OSS projects targeting enterprise distribution use Apache-2.0: Kubernetes, TensorFlow, Apache Kafka, OpenTelemetry, LiteLLM itself
- MIT is the right choice for small utility libraries; Apache-2.0 is right for tools shipped to enterprise customers

**Action:** Add `LICENSE` (Apache-2.0 text), add `NOTICE` file with project name and copyright year, add `SPDX-License-Identifier: Apache-2.0` header to source files.

Reference: [https://choosealicense.com/licenses/apache-2.0/](https://choosealicense.com/licenses/apache-2.0/)

---

## 5. Storage: SQLite for Event-Sourced State

### Confirm the SQLite lean

**Verdict: Confirmed. SQLite is the right choice.** Rationale:

- **Ships with Python stdlib** (`sqlite3`) — zero install friction for local users; `aiosqlite` is the async wrapper and a single pip dep
- **Single-file database** — the entire simulation history is one `.db` file; trivially portable, copyable, backupable
- **WAL (Write-Ahead Logging) mode** — enables concurrent reads while the engine appends events; critical for the UI reading while the sim runs
- **SQLite handles the scale** — each simulation turn is one event row (~1–5 KB of JSON). A 1000-turn sim with 6 agents = ~6000 event rows. SQLite handles millions of rows without complaint
- **Postgres is overkill** — adds an external process, Docker service dependency, connection pooling concerns. This is a single-node distributable tool by design (spec §5)
- **Flat JSON files** — simpler but no transactional guarantees, harder to query "give me all agent states at turn N", no atomic snapshot commits

### Proposed schema (3-table design)

```sql
-- One row per simulation run
CREATE TABLE runs (
    id TEXT PRIMARY KEY,              -- UUID
    name TEXT,
    topic TEXT NOT NULL,
    cast_json TEXT NOT NULL,          -- JSON array of persona definitions
    config_json TEXT,                 -- model config, turn budget, etc.
    status TEXT DEFAULT 'pending',    -- pending | running | complete | failed | branched
    parent_run_id TEXT,               -- NULL for root; set for branches
    branch_turn INTEGER,              -- turn number this branched from
    created_at INTEGER NOT NULL,      -- unix timestamp
    completed_at INTEGER
);

-- Append-only event log (THE source of truth)
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    turn INTEGER NOT NULL,
    seq INTEGER NOT NULL,             -- within-turn sequence (speaker select=0, response=1, etc.)
    event_type TEXT NOT NULL,         -- see event type registry below
    agent_name TEXT,                  -- NULL for sim-level events
    payload TEXT NOT NULL,            -- JSON blob
    created_at INTEGER NOT NULL,
    UNIQUE(run_id, turn, seq)
);
CREATE INDEX events_run_turn ON events(run_id, turn);

-- Full state snapshots (for fast checkpoint restoration)
CREATE TABLE snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    turn INTEGER NOT NULL,
    state_json TEXT NOT NULL,         -- full SimState JSON
    created_at INTEGER NOT NULL,
    UNIQUE(run_id, turn)
);
```

**Event type registry (initial set):**
- `sim.started`, `sim.completed`, `sim.failed`, `sim.paused`
- `speaker.selected` — `{speaker: str, candidates: [str]}`
- `agent.response` — `{speaker: str, message: str, tokens_in: int, tokens_out: int, cost_usd: float}`
- `agent.goal_updated` — `{agent: str, old_goal: str, new_goal: str}`
- `agent.memory_added` — `{agent: str, memory: {...}}`
- `intervention.applied` — `{type: "pause"|"inject_event"|"goal_redirect"|"add_persona", payload: {...}}`
- `avatar.generated` — `{agent: str, prompt: str, image_path: str}`

**Branch operation:** Create new `runs` row with `parent_run_id` + `branch_turn`. Copy events from parent up to `branch_turn`, then append intervention event, then continue engine. No mutation of the parent run's events.

**Checkpoint restore:** Find nearest `snapshots` row at or before target turn; load `state_json`; replay `events` from that turn forward. Fast because snapshot interval is configurable (e.g., every 5 turns).

---

## 6. AutoGen 0.4 save_state/load_state — Prior Art for Our Schema

_Reference only. Do not depend on AutoGen. Mine the design patterns._

### What AutoGen actually ships

The installed `autogen-agentchat>=0.4.0` in the existing `requirements.txt` (which we are removing — see Phase 0 tasks) contains a clean, inspectable state schema in `autogen_agentchat/state/_states.py`.

### Key patterns to adopt

**Pattern 1: Type-discriminated Pydantic state models with versioning**
```python
# AutoGen's base:
class BaseState(BaseModel):
    type: str = Field(default="BaseState")   # discriminator
    version: str = Field(default="1.0.0")    # schema migration key
```
Every state model carries its own type name and schema version. This enables forward compatibility: when you load an old checkpoint, you can detect the version and migrate. **We should do the same** for every event payload type and snapshot structure.

**Pattern 2: Hierarchical state (team → agent → memory)**
AutoGen's `TeamState` contains `agent_states: Mapping[str, Any]` — a dict keyed by agent name, each value being that agent's serialized state. `AssistantAgentState` contains `llm_context` (the message history). State composes cleanly by nesting.

Our equivalent:
```python
class SimSnapshot(BaseModel):
    type: str = "SimSnapshot"
    schema_version: str = "1.0.0"
    run_id: str
    turn: int
    topic: str
    agents: dict[str, AgentState]       # keyed by agent name
    sim_event_log: list[SimEvent]       # sim-level events only

class AgentState(BaseModel):
    type: str = "AgentState"
    schema_version: str = "1.0.0"
    name: str
    system_message: str
    memory_stream: list[MemoryItem]
    goals: list[str]
    relationships: dict[str, str]       # other_agent_name → relationship_desc
    conversation_history: list[dict]    # last N messages this agent has "seen"
    total_tokens: int
    total_cost_usd: float
```

**Pattern 3: Separate message thread from agent state**
AutoGen stores `message_thread` (the global conversation log) in the *group chat manager state*, distinct from each *agent's* internal state (their model context). This is the right separation: the shared transcript is a sim-level concern; each agent's memory/goals/beliefs is an agent-level concern. Our `events` table mirrors this naturally (sim-level vs agent-level events).

**Pattern 4: `model_dump()` / `model_validate()` as the serialization boundary**
AutoGen uses Pydantic's `model_dump()` to produce the dict that gets stored, and `model_validate()` to restore it. The dict (not the model object) is what crosses persistence boundaries. We should do the same: `state_json = snapshot.model_dump_json()` in SQLite; `SimSnapshot.model_validate_json(row["state_json"])` on restore.

### What AutoGen gets wrong (avoid in our design)
- `version` field is set but not actually used for migration logic in the codebase — we should implement actual migration on version mismatch
- `llm_context` is opaque (the message history is stored as raw dicts) — we should store typed event objects so the engine can reconstruct rich cognitive state, not just replay raw LLM messages
- No concept of "branch" — state is restore-in-place, which is destructive. Our branch model (new run row, copied events) is superior and already planned

---

## Phase 0 Task Checklist (for Main's requirements doc)

These are the direct outputs of this research as concrete Phase 0 tasks:

1. **Remove AutoGen deps** — strip `autogen-agentchat`, `autogen-ext` from `requirements.txt`; replace with clean `pyproject.toml`
2. **Extract engine** — lift `simulate_conversation.py` into `matrix_studio/engine/` as a standalone module; remove OpenClaw coupling
3. **Provider-agnostic config** — replace hardcoded `bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0` with Pydantic Settings from env/`.env`; support all LiteLLM prefixes
4. **`python -m matrix_studio`** — wire up FastAPI app with `uvicorn`, serve static placeholder, add WebSocket `/ws/run/{run_id}` stub
5. **SQLite storage layer** — implement `runs`, `events`, `snapshots` tables with `aiosqlite`; hook engine to emit events
6. **Avatar stub** — `avatar.py` with boto3 Nova Canvas call; graceful fallback to null if no AWS creds
7. **`pyproject.toml`** — full project metadata, deps, console_scripts, package_data for `static/`
8. **Dockerfile** — multi-stage (Node frontend build + Python app); `.env.example`
9. **Apache-2.0 LICENSE + NOTICE files**
10. **Pydantic state models** — `SimSnapshot`, `AgentState`, `MemoryItem` with `type` + `schema_version` fields

---

_Research confidence: HIGH on licensing, packaging, storage, web stack. MEDIUM on Nova Canvas pricing (verify on AWS Bedrock pricing page before committing to cost estimates in docs). LiteLLM image-gen gap is confirmed by inspection of the installed library's Bedrock provider code._
