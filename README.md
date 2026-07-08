# TheMatrix Simulation Studio

**Phase 0 Release** - Multi-agent conversation simulator with provider-agnostic LLM support.

TheMatrix Simulation Studio is a standalone tool for running multi-agent conversation simulations. Define a topic and a cast of personas, and watch them engage in natural dialogue. Built on LiteLLM for provider flexibility - bring your own API key for OpenAI, Anthropic, AWS Bedrock, or local models via Ollama.

## Features

- **Provider-agnostic**: Use any LLM supported by LiteLLM (OpenAI, Anthropic, Bedrock, OpenRouter, Ollama)
- **Event-sourced storage**: SQLite database captures full simulation history for replay and analysis
- **Avatar generation**: Optional persona portraits via Amazon Nova Canvas (Bedrock)
- **CLI-first**: Run simulations from JSON request files with simple command-line interface
- **Docker support**: Containerized deployment with environment-based configuration

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
LITELLM_MODEL=bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0
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
LITELLM_MODEL=anthropic/claude-3-5-sonnet-20241022
ANTHROPIC_API_KEY=your_anthropic_key
```

#### For local Ollama:
```bash
LITELLM_MODEL=ollama/llama2
# No API key required
```

See [LiteLLM provider docs](https://docs.litellm.ai/docs/providers) for all supported models.

### Running Your First Simulation

```bash
# Run an example simulation
matrix-studio examples/minimal.json

# Run with custom output file
matrix-studio examples/debate.json -o results.json

# Run with custom turn limit
matrix-studio examples/coffeeshop.json --max-messages 10

# Skip database (faster for testing)
matrix-studio examples/minimal.json --no-db
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

### Build the Image

```bash
docker build -t matrix-studio .
```

### Run a Simulation

```bash
# Run with environment file
docker run --rm \
  --env-file .env \
  -v $(pwd)/examples:/examples \
  -v $(pwd)/data:/app/data \
  matrix-studio \
  python -m matrix_studio /examples/minimal.json

# Run with output to host
docker run --rm \
  --env-file .env \
  -v $(pwd)/examples:/examples \
  -v $(pwd)/output:/output \
  matrix-studio \
  python -m matrix_studio /examples/debate.json -o /output/results.json
```

## Project Structure

```
matrix-sim-studio/
├── matrix_studio/          # Main package
│   ├── engine/            # Simulation engine (litellm orchestration)
│   ├── storage/           # SQLite event-sourced storage
│   ├── api/               # API stub (Phase 1)
│   ├── settings.py        # Configuration management
│   ├── state.py           # Pydantic state models
│   ├── avatar.py          # Avatar generation (Nova Canvas)
│   └── __main__.py        # CLI entrypoint
├── examples/              # Example simulation configs
├── tests/                 # Test suite
├── docs/                  # Documentation
├── pyproject.toml         # Package configuration
├── Dockerfile             # Container definition
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

Avatar generation uses Amazon Nova Canvas via AWS Bedrock. Requirements:
- AWS credentials in environment (see Configuration above)
- `ENABLE_AVATARS=true` in your `.env`

**Graceful fallback**: If AWS credentials are unavailable, avatars are returned as `null` and the simulation continues without error.

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

- **Phase 0** (current): Standalone CLI, provider-agnostic, event-sourced storage
- **Phase 1**: Web UI with cast board, live simulation watching, dossier views
- **Phase 2**: Branching, checkpoint restore, intervention features
- **Phase 3**: Polish, templates, cost controls

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
