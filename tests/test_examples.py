# SPDX-License-Identifier: Apache-2.0
"""
Phase 3 tests for example simulation files.

Verify that each example in examples/ is valid JSON, loads in the new-run form,
and runs via the CLI (mocked engine to avoid live LLM costs).
"""

import json
from pathlib import Path
import pytest
from matrix_studio.engine import run_simulation


EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def get_example_files():
    """Get all .json files from examples/ directory."""
    return list(EXAMPLES_DIR.glob("*.json"))


@pytest.mark.parametrize("example_path", get_example_files(), ids=lambda p: p.name)
def test_example_valid_json(example_path):
    """Each example file is valid JSON."""
    with open(example_path) as f:
        data = json.load(f)
    assert "topic" in data
    assert "cast" in data
    assert isinstance(data["cast"], list)
    assert len(data["cast"]) > 0


@pytest.mark.parametrize("example_path", get_example_files(), ids=lambda p: p.name)
def test_example_has_required_fields(example_path):
    """Each example has the required schema fields."""
    with open(example_path) as f:
        data = json.load(f)

    # Required fields
    assert isinstance(data["topic"], str)
    assert len(data["topic"]) > 0

    # Each persona must have name, persona, and goals
    for persona in data["cast"]:
        assert "name" in persona
        assert "persona" in persona
        assert "goals" in persona
        assert isinstance(persona["name"], str)
        assert isinstance(persona["persona"], str)
        assert isinstance(persona["goals"], list)


@pytest.mark.asyncio
@pytest.mark.parametrize("example_path", get_example_files(), ids=lambda p: p.name)
async def test_example_runs_with_mocked_engine(example_path, monkeypatch, tmp_path):
    """
    Each example can be loaded and run through the engine (mocked to avoid live LLM calls).
    """
    from unittest.mock import MagicMock
    import litellm
    from matrix_studio.storage import Database
    from matrix_studio.settings import Settings

    # Mock litellm
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="Mocked response"))]
    mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=20)
    mock_response._hidden_params = {}

    async def mock_acompletion(*args, **kwargs):
        return mock_response

    monkeypatch.setattr(litellm, "acompletion", mock_acompletion)

    # Mock avatar generation (to avoid Bedrock calls)
    async def mock_generate_avatar(name, persona, seed=None):
        return None  # Avatars are optional

    monkeypatch.setattr("matrix_studio.engine.simulator.generate_avatar", mock_generate_avatar)

    # Load example
    with open(example_path) as f:
        request = json.load(f)

    # Override max_messages to keep test fast
    if "config" not in request:
        request["config"] = {}
    request["config"]["max_messages"] = 2  # Just 2 turns for testing
    request["config"]["generate_avatars"] = False  # Disable avatars for speed

    # Setup test DB
    settings = Settings(data_dir=str(tmp_path), _env_file=None)
    monkeypatch.setattr("matrix_studio.settings._settings", settings)
    monkeypatch.setattr("matrix_studio.engine.simulator.get_settings", lambda: settings)

    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    # Run simulation
    result = await run_simulation(request, db=db)

    # Verify basic result structure
    assert "run_id" in result
    assert "status" in result
    assert result["status"] in ["complete", "failed", "capped"]
    assert "conversation" in result
    assert len(result["conversation"]) > 0

    await db.close()


def test_design_review_example_has_cognition():
    """
    The design-review example specifically demonstrates cognition enabled.
    """
    example_path = EXAMPLES_DIR / "design-review.json"
    with open(example_path) as f:
        data = json.load(f)

    assert "config" in data
    assert "cognition" in data["config"]
    assert data["config"]["cognition"]["enabled"] is True
    assert "memory" in data["config"]["cognition"]
