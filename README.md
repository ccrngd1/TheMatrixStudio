# TheMatrix Simulation Studio

**Version 0.3.0** — Multi-agent conversation simulator with live control-room UI, checkpointing/branching, cognition system, and non-photorealistic avatars.

TheMatrix Simulation Studio is a standalone tool for running multi-agent conversation simulations. Define a topic and cast of personas, hit **Run**, and watch the conversation unfold live in a web control room. Features checkpointing, timeline branching, optional agent cognition (memory + reflection + goals), and anime-style avatar generation.

![Control Room](docs/screenshots/control-room-placeholder.png)
<!-- TODO: Replace with actual screenshot showing cast board + live conversation -->

![Cognition Dossier](docs/screenshots/dossier-placeholder.png)
<!-- TODO: Replace with actual screenshot showing agent dossier with memory stream + why-trace -->

![Branch Tree View](docs/screenshots/branch-tree-placeholder.png)
<!-- TODO: Replace with actual screenshot showing visual branch tree with parent/child relationships and timeline scrubber -->

![Cost Meter](docs/screenshots/cost-meter-placeholder.png)
<!-- TODO: Replace with actual screenshot showing live token/$ cost meter with optional spend cap and warning threshold -->

## Features

- **Live Control Room** — Cast board with character cards (avatar + persona + goals), live-scrolling conversation feed, active-speaker highlight, and running token/$ cost meter
- **Checkpointing & Branching** — Every turn is checkpointed; branch from any point to create "what-if" timelines with different interventions
- **Interventions** — Inject messages, edit goals, add/remove personas, continue discussions, or promote aside conversations into the main timeline
- **Agent Cognition** (Phase 2c, optional) — Agents form memories, reflect periodically, track relationships, and explain their reasoning ("why did they say that?")
- **Post-Run Analysis** — Auto-generated structured summary (consensus / dissenters / key ideas / open questions) plus aside conversations (ask the analyst, ask a persona, ask the room)
- **Non-Photorealistic Avatars** — Anime-style character portraits generated via Stability SD3.5 on AWS Bedrock (optional, with graceful fallback to initials)
- **Cost Visibility** — Live token/$ meter, optional hard spend cap per run, and creation-time cost estimate
- **Provider-Agnostic** — Bring your own API key for OpenAI, Anthropic, AWS Bedrock, OpenRouter, or local Ollama models via LiteLLM
- **Event-Sourced Storage** — SQLite database captures full simulation history for replay, branching, and audit
- **Named Runs** — Every run gets a memorable two-word codename (e.g., `trusted-robot`) for easy browsing
- **Docker + CLI** — One container serves both API and UI on a single port; or use the CLI to run simulations headlessly

## Quick Start

### 5-Minute Quickstart (pip)

**Requirements:** Python 3.11+

```bash
# 1. Clone and install
git clone https://github.com/yourusername/matrix-sim-studio.git
cd matrix-sim-studio
pip install .

# 2. Configure your API key (choose one provider)
cp .env.example .env
# Edit .env and set your key:
#   - AWS Bedrock: AWS_BEARER_TOKEN_BEDROCK or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY
#   - OpenAI: OPENAI_API_KEY
#   - Anthropic: ANTHROPIC_API_KEY
#   - Ollama: no key needed (local)

# 3. Start the control room
matrix-studio serve
# Open http://127.0.0.1:8000 in your browser

# 4. Load an example and hit Run
# Try examples/debate.json (AI in creative work) or examples/design-review.json (with cognition)
```

### 5-Minute Quickstart (Docker)

```bash
# 1. Clone the repository
git clone https://github.com/yourusername/matrix-sim-studio.git
cd matrix-sim-studio

# 2. Configure your API key
cp .env.example .env
# Edit .env (see above for providers)

# 3. Build and run
docker build -t matrix-studio .
docker run --rm -p 8000:8000 --env-file .env -v $(pwd)/data:/app/data matrix-studio

# 4. Open http://localhost:8000 and load an example
```

**NOTE:** Docker build has NOT been verified in this environment (unavailable). If it fails, please report an issue.

## Configuration

All settings can be configured via environment variables or `.env` file. Settings precedence: **environment variables > .env file > defaults**.

### Model & Provider

```bash
# Model selection (any LiteLLM-supported model string)
LITELLM_MODEL=bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0
LITELLM_TEMPERATURE=0.7
LITELLM_MAX_TOKENS=2048

# Selectable models in the UI dropdown (comma-separated)
AVAILABLE_MODELS=bedrock/global.anthropic.claude-sonnet-4-6,bedrock/amazon.nova-pro-v1:0
```

### Provider Credentials

**Keys stay server-side.** The browser never handles raw credentials; `/api/models` exposes model strings only.

#### AWS Bedrock
```bash
LITELLM_MODEL=bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0
AWS_BEARER_TOKEN_BEDROCK=your_bearer_token   # Recommended
# ...or classic IAM keys:
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
AWS_REGION=us-east-1
```
Bedrock also respects the boto3 credential chain (env vars, `~/.aws/credentials`, EC2 instance profile).

#### OpenAI
```bash
LITELLM_MODEL=openai/gpt-4o
OPENAI_API_KEY=sk-...
```

#### Anthropic
```bash
LITELLM_MODEL=anthropic/claude-sonnet-4-6
ANTHROPIC_API_KEY=sk-ant-...
```

#### Local Ollama
```bash
LITELLM_MODEL=ollama/llama2
# No API key required
```

See [LiteLLM provider docs](https://docs.litellm.ai/docs/providers) for all supported models.

### Simulation Defaults

```bash
MAX_MESSAGES=20                # Default max turns per simulation
MAX_RUN_COST_USD=0.0           # Per-run hard spend cap in USD (0 = OFF)
COST_WARN_THRESHOLD=1.0        # Warning threshold shown in UI cost meter
```

**Cost Cap:** When `MAX_RUN_COST_USD > 0`, the engine checks accumulated real cost after each turn. When the cap is reached, the run ends in a terminal `capped` status. The cap acts on LiteLLM-reported cost only; providers that don't report cost (e.g., local Ollama) are counted as $0.

### Avatar Generation

```bash
ENABLE_AVATARS=true            # Enable avatar generation
AVATAR_STYLE=anime             # Style: anime (default), illustration, 3d
AVATAR_MODEL_ID=stability.sd3-5-large-v1:0
AVATAR_REGION=us-west-2        # SD3.5 Large is served from us-west-2
```

**Avatar Style:** Default is `anime` (non-photorealistic stylized art) to avoid synthetic-media concerns. Avatars are optional eye-candy; generation failures fall back to initials/color placeholders and never block a run.

### Server & Storage

```bash
MATRIX_HOST=127.0.0.1
MATRIX_PORT=8000
DATA_DIR=./data                # SQLite database location
```

## Cognition & Honesty Note

Agent cognition (memory, reflection, relationships) is **model-generated introspection captured in-loop**, not ground truth. Agents self-report their reasoning ("why I said this"), but the model can be mistaken, confabulate, or rationalize. Treat cognition output as the agent's perspective, not fact.

**Cost impact:** Cognition mode uses structured JSON output (1 call per turn instead of plain-text), adds memory retrieval to each prompt, and triggers periodic reflection calls. Expect ~20-40% higher token usage when cognition is enabled.

## Usage

### Web UI (Control Room)

```bash
matrix-studio serve                    # Start on default host/port
matrix-studio serve --host 0.0.0.0 --port 8000
```

Open the UI in your browser. The new-run form lets you:
- Define topic + cast (personas + goals)
- Choose a model from the allowlist
- Enable cognition (memory, reflection, dynamic goals, relationships)
- Set a cost cap (optional hard spend limit)
- Load an example template

Past runs are listed by codename. Click a run to:
- **Replay** the conversation (scrub through the timeline)
- **Branch** from any turn (inject a message, edit goals, add/remove personas, continue)
- **Analyze** (view auto-generated summary or start aside conversations)
- **View dossier** (click an agent card for memory stream, reflections, relationships, why-trace)

### CLI (Headless Runs)

```bash
# Run a simulation from JSON
matrix-studio run examples/debate.json

# Custom output file
matrix-studio run examples/minimal.json -o results.json

# Custom turn limit
matrix-studio run examples/coffeeshop.json --max-messages 10

# Skip database (faster for testing)
matrix-studio run examples/minimal.json --no-db
```

### Creating Custom Simulations

Create a JSON file:

```json
{
  "topic": "Your conversation topic",
  "cast": [
    {
      "name": "PersonaName",
      "persona": "Description of the persona's personality, background, and perspective",
      "goals": ["Goal 1", "Goal 2"]
    }
  ],
  "config": {
    "max_messages": 15,
    "generate_avatars": true,
    "cognition": {
      "enabled": false,
      "memory": true,
      "reflection_every": 4,
      "goals_dynamic": false,
      "relationships": false,
      "retrieval_k": 5
    }
  }
}
```

**Cognition config** (optional, all default to off):
- `enabled`: Master switch for cognition
- `memory`: Form + retrieve agent memories
- `reflection_every`: Reflect every N turns (0 disables, default 4 when cognition is on)
- `goals_dynamic`: Allow agents to update their own goals mid-run
- `relationships`: Track per-agent stance toward others
- `retrieval_k`: Memories injected into each turn's prompt (default 5)

See `examples/` for ready-to-run templates.

## Project Structure

```
matrix-sim-studio/
├── matrix_studio/          # Main package
│   ├── engine/            # Simulation engine (litellm orchestration + cognition)
│   ├── storage/           # SQLite event-sourced storage
│   ├── api/               # FastAPI app + WebSocket stream + run manager
│   ├── static/            # Built frontend assets (from Vite build)
│   ├── settings.py        # Configuration management
│   ├── state.py           # Pydantic state models (AgentState, CognitionConfig, SimSnapshot)
│   ├── avatar.py          # Avatar generation (Stability SD3.5 on Bedrock)
│   ├── analysis.py        # Post-run summary + aside conversations (Phase 1.5)
│   ├── branching.py       # Branch primitive (Phase 2a/2b)
│   ├── naming.py          # Memorable run codename generation
│   └── __main__.py        # CLI entrypoint (run / serve subcommands)
├── frontend/              # React + Vite + TypeScript + Tailwind UI
├── examples/              # Example simulation configs
├── tests/                 # Backend test suite (194 tests)
├── docs/                  # Documentation
├── pyproject.toml         # Package configuration
├── Dockerfile             # Multi-stage container (frontend build + Python app)
├── LICENSE                # Apache-2.0
└── README.md             # This file
```

## Architecture

### Engine

The simulation engine is a **hand-rolled async loop over LiteLLM** (not AutoGen). Each turn:
1. **Select speaker:** LLM decides who should speak next given personas + conversation history
2. **Generate response:** Selected agent generates their response given their persona + goals

When cognition is enabled, the engine also:
- Retrieves the speaker's top-K memories (by importance + recency) and injects them into the prompt
- Parses structured JSON output (utterance + rationale + goal_served + formed_memories + goal_update + relationship_updates)
- Periodically triggers reflection calls (condense recent memories into higher-level beliefs)
- Emits structured events for memory formation, goal updates, relationship changes, and reflections

### Event Sourcing & Checkpointing

All simulation state changes are captured as events in an append-only log. After each turn, the engine persists a full `SimSnapshot` (serializable state: agents, conversation, status). Benefits:
- **Branching:** Fork the event log at turn N, apply a mutation, generate forward as a new run
- **Replay:** Reconstruct any moment by loading the snapshot at that turn
- **Auditability:** Full history for debugging, analysis, and compliance

Storage is SQLite (`./data/matrix_studio.db`). Snapshots are full per-turn (not deltas) — runs are short (≤ few dozen turns), so storage cost is negligible and reconstruction is O(1).

### Provider Agnosticism

The `LITELLM_MODEL` variable accepts any LiteLLM model string. No code changes needed to switch providers. Cost reporting and spend caps work when the provider reports usage; providers that don't (e.g., Ollama) are counted as $0.

## Development

### Install in Editable Mode

```bash
pip install -e ".[dev]"
```

### Run Tests

```bash
# Backend (194 tests, all mocked)
pytest

# Frontend (18 tests)
cd frontend
npm run build
NODE_ENV=test npx vitest run
```

**Test mocking:** All tests mock litellm + avatar generation to avoid live billable calls (the real environment carries a Bedrock key).

### Frontend Dev Mode

```bash
# Terminal 1: backend
matrix-studio serve

# Terminal 2: Vite dev server (hot reload, proxies /api to backend on :8000)
cd frontend
npm run dev
```

## Roadmap

- ✅ **Phase 0:** Standalone CLI, provider-agnostic, event-sourced storage
- ✅ **Phase 1:** Control-room web UI — cast board, live watching, cost meter, dossier, named runs, replay
- ✅ **Phase 1.5:** Post-run analysis — structured summary + aside conversations (analyst / persona / room)
- ✅ **Phase 2a:** Checkpointing & branching — per-turn snapshots, branch primitive, replay
- ✅ **Phase 2b:** Interventions — inject message, continue, edit goal, add/remove persona, promote aside
- ✅ **Phase 2c:** Agent cognition — memory stream, reflection, relationships, dynamic goals, why-trace
- ✅ **Phase 3:** Release polish — cost guards, BYO-key readiness, examples, docs, hygiene (v0.3.0)
- **Future:** Embedding-based memory retrieval, multi-modal inputs, hosted deployment

## Documentation

- `docs/PROJECT-SPEC.md` — Full ideation/architecture spec
- `docs/PHASE3-REQUIREMENTS.md` — Phase 3 (release polish) acceptance criteria
- `docs/PHASE2C-REQUIREMENTS.md` — Phase 2c (cognition) spec
- `docs/PHASE2B-REQUIREMENTS.md` — Phase 2b (interventions) spec
- `docs/PHASE2A-REQUIREMENTS.md` — Phase 2a (checkpointing/branching) spec
- `docs/PHASE1.5-REQUIREMENTS.md` — Phase 1.5 (analysis layer) spec
- `docs/PHASE0-RESEARCH.md` — Technical decisions (web stack, Bedrock, packaging, license, storage)

## License

Apache-2.0 - See [LICENSE](LICENSE) file for full text.

## Contributing

Contributions welcome. This is an active-development project.

## Support

For issues and questions, please open an issue on GitHub.

---

**Version 0.3.0** — Built with Claude Code. Phases 0-3 complete. Ready for production use with BYO API keys.
