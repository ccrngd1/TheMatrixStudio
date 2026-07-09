# TheMatrix Simulation Studio

**Phase 1.5** - Multi-agent conversation simulator with a live "control-room" web UI plus a read-only post-run analysis layer (summaries + aside conversations).

TheMatrix Simulation Studio is a standalone tool for running multi-agent conversation simulations. Define a topic and a cast of personas, hit **Run**, and watch the conversation unfold live: a **cast board** of character cards (avatar + persona + goals), a **live-scrolling conversation feed**, a highlight of **who's speaking now**, and a **running token/$ cost meter**. Click a card for that agent's **dossier**; browse and **replay past runs** by their memorable codename. Built on LiteLLM for provider flexibility — bring your own API key for OpenAI, Anthropic, AWS Bedrock, or local models via Ollama.

## Features

- **Control-room web UI** (Phase 1): cast board, live conversation feed, active-speaker highlight, cost meter, per-agent dossier, new-run/cast-builder form, and searchable run history — served from the same process as the API.
- **Post-run analysis** (Phase 1.5, read-only): an auto-generated structured **summary** (consensus / dissenters / key ideas / open questions / overview; configurable at run creation, on by default) and **aside conversations** — ask the **analyst** (neutral, about the whole run), a **single persona** (in-character, using that agent's real stored persona), or the **room** (all personas react into the thread). Asides are private side-threads that never mutate the canonical run; their token/cost is tracked separately. Summaries and aside replies are model-generated *analysis*, labeled as such.
- **Live streaming**: runs stream over a WebSocket as they progress (late joiners catch up via replayed events, then continue live).
- **UI-only playback**: pause / resume / step / reveal-speed operate purely on the buffered client-side stream — they never pause, slow, or gate the engine (it always runs to completion at full speed).
- **Named runs**: every run gets a memorable, topically-resonant two-word codename (e.g. an AI-ethics topic → `trusted-robot`) via a cheap LLM call, with a random-wordlist fallback so naming never blocks a run.
- **Provider-agnostic**: Use any LLM supported by LiteLLM (OpenAI, Anthropic, Bedrock, OpenRouter, Ollama)
- **Event-sourced storage**: SQLite database captures full simulation history for replay and analysis
- **Avatar generation**: Optional persona portraits via a Stability image model on Bedrock (`stability.sd3-5-large-v1:0`), generated in parallel at run start with a mandatory initials/color placeholder fallback
- **CLI**: `run` a simulation from a JSON request file, or `serve` the web app
- **Docker support**: one image serves both the API and the built UI on one port

## Quick Start

### Local Installation

**Requirements**: Python 3.11+

```bash
# Clone the repository
git clone https://github.com/yourusername/matrix-sim-studio.git
cd matrix-sim-studio

# Install
pip install .

# Verify installation
matrix-studio --version
```

### Configuration (Bring Your Own Key)

Copy the example environment file and add your API keys:

```bash
cp .env.example .env
# Edit .env with your preferred editor
```

**Required configuration** depends on your chosen LLM provider:

#### For AWS Bedrock:
```bash
LITELLM_MODEL=bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0
# Bedrock API key / bearer token (recommended):
AWS_BEARER_TOKEN_BEDROCK=your_bearer_token
# ...or classic IAM keys:
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
AWS_REGION=us-east-1
```

#### For OpenAI:
```bash
LITELLM_MODEL=openai/gpt-4o
OPENAI_API_KEY=your_openai_key
```

#### For Anthropic:
```bash
LITELLM_MODEL=anthropic/claude-sonnet-4-6
ANTHROPIC_API_KEY=your_anthropic_key
```

#### For local Ollama:
```bash
LITELLM_MODEL=ollama/llama2
# No API key required
```

See [LiteLLM provider docs](https://docs.litellm.ai/docs/providers) for all supported models.

### Running the Web UI (Phase 1)

```bash
# Start the control-room server (defaults to MATRIX_HOST/MATRIX_PORT, i.e. 127.0.0.1:8000)
matrix-studio serve

# Or choose host/port explicitly
matrix-studio serve --host 0.0.0.0 --port 8000
```

Then open <http://127.0.0.1:8000> in your browser: define a cast and topic (or
**Load example**), hit **Run**, and watch the conversation stream live. Past runs
are listed by their memorable codename and can be replayed.

> The server serves the pre-built frontend from `matrix_studio/static/`. When
> installing from source, build it once with `cd frontend && npm install && npm run build`
> (the Docker image does this for you). Without a build, the API still runs and
> `/` returns a small JSON notice.

**Frontend dev mode** (hot reload, proxies `/api` to the backend on :8000):

```bash
matrix-studio serve            # terminal 1: backend
cd frontend && npm run dev     # terminal 2: Vite dev server
```

### Running a Simulation from the CLI (Phase 0 behavior, unchanged)

```bash
# Run an example simulation
matrix-studio run examples/minimal.json

# Run with custom output file
matrix-studio run examples/debate.json -o results.json

# Run with custom turn limit
matrix-studio run examples/coffeeshop.json --max-messages 10

# Skip database (faster for testing)
matrix-studio run examples/minimal.json --no-db
```

### Creating Custom Simulations

Create a JSON file defining your simulation:

```json
{
  "topic": "Your conversation topic",
  "cast": [
    {
      "name": "PersonaName",
      "persona": "Description of the persona's personality and background",
      "goals": ["Goal 1", "Goal 2"]
    }
  ],
  "config": {
    "max_messages": 15,
    "generate_avatars": true
  }
}
```

See `examples/` directory for complete examples.

## Docker Usage

The image is multi-stage: a Node stage builds the React/Vite frontend into
`matrix_studio/static`, and the Python stage installs the package and serves
both the API and the built UI on one port.

### Build the Image

```bash
docker build -t matrix-studio .
```

### Serve the Web UI (default)

```bash
docker run --rm -p 8000:8000 \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  matrix-studio
# open http://localhost:8000
```

### Run a Simulation from the CLI in the container

```bash
docker run --rm \
  --env-file .env \
  -v $(pwd)/examples:/examples \
  -v $(pwd)/data:/app/data \
  matrix-studio \
  python -m matrix_studio run /examples/minimal.json
```

## Project Structure

```
matrix-sim-studio/
├── matrix_studio/          # Main package
│   ├── engine/            # Simulation engine (litellm orchestration)
│   ├── storage/           # SQLite event-sourced storage
│   ├── api/               # FastAPI app, WebSocket stream, run manager (Phase 1)
│   ├── static/            # Built frontend assets (generated by the Vite build)
│   ├── naming.py          # Memorable run codename generation (Phase 1)
│   ├── settings.py        # Configuration management
│   ├── state.py           # Pydantic state models
│   ├── avatar.py          # Avatar generation (Stability SD3.5 on Bedrock)
│   └── __main__.py        # CLI entrypoint (run / serve subcommands)
├── frontend/              # React + Vite + TypeScript + Tailwind UI (Phase 1)
├── examples/              # Example simulation configs
├── tests/                 # Backend test suite (engine/API/storage/naming)
├── docs/                  # Documentation
├── pyproject.toml         # Package configuration
├── Dockerfile             # Multi-stage container (frontend build + Python app)
├── LICENSE                # Apache-2.0 license
└── README.md             # This file
```

## Storage and Data

Simulations are persisted to a SQLite database at `./data/matrix_studio.db` (configurable via `DATA_DIR` env var).

The database captures:
- **Runs**: Metadata for each simulation run
- **Events**: Append-only event log (speaker selections, responses, costs)
- **Snapshots**: Full state snapshots at completion

This event-sourced design enables future features like branching and replay (Phase 2).

## Avatar Generation

Avatar generation uses a Stability text-to-image model on AWS Bedrock
(`stability.sd3-5-large-v1:0`, served from `AVATAR_REGION=us-west-2`). Portraits
are generated **in parallel** at run start and stream to the UI via `avatar.ready`
events as each finishes. Requirements:
- AWS credentials in environment (bearer token or IAM keys; see Configuration above)
- `ENABLE_AVATARS=true` in your `.env`

**Graceful fallback (mandatory)**: if avatars are disabled or generation returns
nothing (no credentials, content filter, or error), the UI shows a deterministic
**initials/color placeholder** and the run proceeds normally. Avatars are
eye-candy, never a hard dependency.

To disable avatars entirely:
```bash
ENABLE_AVATARS=false
```

Or per-simulation in the request JSON:
```json
"config": {
  "generate_avatars": false
}
```

## Development

### Install in Editable Mode

```bash
pip install -e ".[dev]"
```

### Run Tests

```bash
pytest
```

### Project Dependencies

Core dependencies (from `pyproject.toml`):
- `litellm>=1.40.0` - Multi-provider LLM client
- `boto3>=1.34.0` - AWS SDK (for Bedrock avatars)
- `fastapi>=0.110.0` - Web framework (Phase 1)
- `uvicorn[standard]>=0.29.0` - ASGI server
- `aiosqlite>=0.20.0` - Async SQLite
- `pydantic>=2.0.0` - Data validation
- `pydantic-settings>=2.0.0` - Settings management
- `python-dotenv>=1.0.0` - Environment loading

## Roadmap

- **Phase 0**: Standalone CLI, provider-agnostic, event-sourced storage
- **Phase 1**: Control-room web UI — cast board, live watching, cost meter, scoped dossier, named runs, replay
- **Phase 1.5** (current): Read-only post-run analysis — structured end-of-run summary + aside conversations (analyst / persona / room). Introduces the thread/target/mode data model; no engine-loop changes, no mutation of the canonical run.
- **Phase 2**: Contribute mode (promote an aside into the conversation) + branching, checkpoint restore, intervention features; rich dossier (memory streams, reflections, relationships, "why did it say that?")
- **Phase 3**: Polish, in-browser BYO-key, enforced spend caps, templates

## Architecture Notes

### Engine

The simulation engine is a **hand-rolled async loop over LiteLLM** (not AutoGen). Each turn:
1. **Select speaker**: LLM decides who should speak next given personas and conversation history
2. **Generate response**: Selected agent generates their response given their persona and goals

This two-phase approach ensures natural conversation flow without rigid turn-taking.

### Event Sourcing

All simulation state changes are captured as events in an append-only log. Benefits:
- Full auditability of simulation runs
- Foundation for branching and "what-if" scenarios (Phase 2)
- Structured cost and token tracking per agent

### Provider Agnosticism

Configuration precedence: `environment variables` > `.env file` > `config.json defaults`

The `LITELLM_MODEL` variable accepts any LiteLLM model string. No code changes needed to switch providers.

## Documentation

- `docs/PROJECT-SPEC.md` — Full ideation/architecture spec
- `docs/PHASE0-REQUIREMENTS.md` — Phase 0 scope + acceptance criteria
- `docs/PHASE0-RESEARCH.md` — Technical decisions (web stack, Bedrock, packaging, license, storage)

## License

Apache-2.0 - See [LICENSE](LICENSE) file for full text.

## Contributing

Contributions are welcome. This is an early-phase project - expect active development.

## Support

For issues and questions, please open an issue on GitHub.
