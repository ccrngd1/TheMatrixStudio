# SPDX-License-Identifier: Apache-2.0
"""Tests for the simulation engine - verifying the litellm orchestration loop."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from matrix_studio.engine import run_simulation
from matrix_studio.storage import Database


class MockLiteLLMResponse:
    """Mock LiteLLM response object."""

    def __init__(self, content: str, tokens_in: int = 100, tokens_out: int = 50):
        self.choices = [
            MagicMock(
                message=MagicMock(content=content)
            )
        ]
        self.usage = MagicMock(
            prompt_tokens=tokens_in,
            completion_tokens=tokens_out,
        )
        self._hidden_params = {"response_cost": 0.001}


@pytest.fixture
async def test_db():
    """Create a temporary test database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()
    Path(db_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_run_simulation_basic(test_db):
    """Test basic simulation execution with mocked LiteLLM."""
    request = {
        "topic": "Planning a picnic",
        "cast": [
            {"name": "Alice", "persona": "Organized planner", "goals": ["Plan details"]},
            {"name": "Bob", "persona": "Easy-going friend", "goals": ["Have fun"]},
        ],
        "config": {
            "max_messages": 3,
            "generate_avatars": False,
        },
    }

    # Mock litellm.acompletion
    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock_completion:
        # Mock responses: first call selects speaker, second call generates response
        mock_completion.side_effect = [
            # Turn 1: select Alice, then Alice responds
            MockLiteLLMResponse("Alice"),
            MockLiteLLMResponse("Let's plan this picnic carefully!"),
            # Turn 2: select Bob, then Bob responds
            MockLiteLLMResponse("Bob"),
            MockLiteLLMResponse("Sure, I'm excited!"),
            # Turn 3: select Alice, then Alice responds
            MockLiteLLMResponse("Alice"),
            MockLiteLLMResponse("Great! Let's make a list."),
        ]

        result = await run_simulation(request, db=test_db)

        # Verify simulation completed
        assert result["status"] == "complete"
        assert result["total_turns"] == 3
        assert len(result["conversation"]) == 3

        # Verify conversation content
        assert "Alice" in result["agents"]
        assert "Bob" in result["agents"]

        # Verify LiteLLM was called (2 calls per turn: select + generate)
        assert mock_completion.call_count == 6


@pytest.mark.asyncio
async def test_run_simulation_no_database():
    """Test simulation can run without database persistence."""
    request = {
        "topic": "Quick chat",
        "cast": [
            {"name": "Alice", "persona": "Helper", "goals": []},
        ],
        "config": {
            "max_messages": 2,
            "generate_avatars": False,
        },
    }

    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock_completion:
        mock_completion.side_effect = [
            MockLiteLLMResponse("Alice"),
            MockLiteLLMResponse("Hello!"),
            MockLiteLLMResponse("Alice"),
            MockLiteLLMResponse("How are you?"),
        ]

        # Run without database
        result = await run_simulation(request, db=None)

        assert result["status"] == "complete"
        assert result["total_turns"] == 2


@pytest.mark.asyncio
async def test_simulation_saves_events(test_db):
    """Test that simulation saves events to database."""
    request = {
        "topic": "Test conversation",
        "cast": [
            {"name": "Alice", "persona": "Test", "goals": []},
        ],
        "config": {
            "max_messages": 2,
            "generate_avatars": False,
        },
    }

    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock_completion:
        mock_completion.side_effect = [
            MockLiteLLMResponse("Alice"),
            MockLiteLLMResponse("Message 1"),
            MockLiteLLMResponse("Alice"),
            MockLiteLLMResponse("Message 2"),
        ]

        result = await run_simulation(request, db=test_db)
        run_id = result["run_id"]

        # Check that events were saved
        events = await test_db.get_events(run_id)
        assert len(events) > 0

        # Should have: sim.started, speaker.selected, agent.response for each turn, sim.completed
        event_types = [e["event_type"] for e in events]
        assert "sim.started" in event_types
        assert "speaker.selected" in event_types
        assert "agent.response" in event_types
        assert "sim.completed" in event_types


@pytest.mark.asyncio
async def test_simulation_saves_snapshot(test_db):
    """Test that simulation saves completion snapshot."""
    request = {
        "topic": "Test snapshot",
        "cast": [
            {"name": "Alice", "persona": "Test", "goals": ["Goal 1"]},
            {"name": "Bob", "persona": "Test", "goals": ["Goal 2"]},
        ],
        "config": {
            "max_messages": 2,
            "generate_avatars": False,
        },
    }

    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock_completion:
        mock_completion.side_effect = [
            MockLiteLLMResponse("Alice"),
            MockLiteLLMResponse("Hello Bob"),
            MockLiteLLMResponse("Bob"),
            MockLiteLLMResponse("Hi Alice"),
        ]

        result = await run_simulation(request, db=test_db)
        run_id = result["run_id"]

        # Check that snapshot was saved
        snapshot = await test_db.get_snapshot(run_id)
        assert snapshot is not None
        assert snapshot.run_id == run_id
        assert snapshot.status == "complete"
        assert len(snapshot.agents) == 2
        assert "Alice" in snapshot.agents
        assert "Bob" in snapshot.agents


@pytest.mark.asyncio
async def test_simulation_tracks_costs():
    """Test that simulation tracks token usage and costs."""
    request = {
        "topic": "Cost tracking test",
        "cast": [
            {"name": "Alice", "persona": "Test", "goals": []},
        ],
        "config": {
            "max_messages": 2,
            "generate_avatars": False,
        },
    }

    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock_completion:
        mock_completion.side_effect = [
            MockLiteLLMResponse("Alice", tokens_in=100, tokens_out=50),
            MockLiteLLMResponse("Response 1", tokens_in=150, tokens_out=75),
            MockLiteLLMResponse("Alice", tokens_in=100, tokens_out=50),
            MockLiteLLMResponse("Response 2", tokens_in=150, tokens_out=75),
        ]

        result = await run_simulation(request, db=None)

        # Check that costs were tracked
        alice = result["agents"]["Alice"]
        assert alice["total_tokens_in"] > 0
        assert alice["total_tokens_out"] > 0
        assert result["total_cost_usd"] > 0


@pytest.mark.asyncio
async def test_simulation_max_messages_override():
    """Test that max_messages config is respected."""
    request = {
        "topic": "Short conversation",
        "cast": [
            {"name": "Alice", "persona": "Test", "goals": []},
        ],
        "config": {
            "max_messages": 5,
            "generate_avatars": False,
        },
    }

    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock_completion:
        # Provide enough responses for 5 turns
        responses = []
        for _ in range(5):
            responses.append(MockLiteLLMResponse("Alice"))
            responses.append(MockLiteLLMResponse("Message"))
        mock_completion.side_effect = responses

        result = await run_simulation(request, db=None)

        # Should stop at exactly 5 turns
        assert result["total_turns"] == 5
        assert len(result["conversation"]) == 5


@pytest.mark.asyncio
async def test_simulation_error_handling():
    """Test that simulation handles LiteLLM errors gracefully."""
    request = {
        "topic": "Error test",
        "cast": [
            {"name": "Alice", "persona": "Test", "goals": []},
        ],
        "config": {
            "max_messages": 2,
            "generate_avatars": False,
        },
    }

    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock_completion:
        # First call succeeds, second call fails with error message placeholder
        mock_completion.side_effect = [
            MockLiteLLMResponse("Alice"),
            Exception("API Error"),
        ]

        result = await run_simulation(request, db=None)

        # Engine handles individual response errors gracefully and continues
        # The response will have an error message placeholder
        assert result["status"] in ["complete", "failed"]
        # Check that error was logged in conversation
        if result["status"] == "complete":
            assert len(result["conversation"]) > 0
            assert "[Error generating response" in result["conversation"][0]["content"]


@pytest.mark.asyncio
async def test_speaker_selection_logic():
    """Test that speaker selection works correctly."""
    request = {
        "topic": "Speaker test",
        "cast": [
            {"name": "Alice", "persona": "Person A", "goals": []},
            {"name": "Bob", "persona": "Person B", "goals": []},
            {"name": "Charlie", "persona": "Person C", "goals": []},
        ],
        "config": {
            "max_messages": 3,
            "generate_avatars": False,
        },
    }

    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock_completion:
        # Explicitly select different speakers
        mock_completion.side_effect = [
            MockLiteLLMResponse("Alice"),
            MockLiteLLMResponse("Alice speaks"),
            MockLiteLLMResponse("Bob"),
            MockLiteLLMResponse("Bob speaks"),
            MockLiteLLMResponse("Charlie"),
            MockLiteLLMResponse("Charlie speaks"),
        ]

        result = await run_simulation(request, db=None)

        # Verify different speakers were selected
        speakers = [msg["speaker"] for msg in result["conversation"]]
        assert "Alice" in speakers
        assert "Bob" in speakers
        assert "Charlie" in speakers


@pytest.mark.asyncio
async def test_simulation_with_avatars_disabled():
    """Test that avatar generation can be disabled."""
    request = {
        "topic": "No avatars",
        "cast": [
            {"name": "Alice", "persona": "Test", "goals": []},
        ],
        "config": {
            "max_messages": 1,
            "generate_avatars": False,
        },
    }

    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock_completion:
        mock_completion.side_effect = [
            MockLiteLLMResponse("Alice"),
            MockLiteLLMResponse("Hello"),
        ]

        with patch("matrix_studio.engine.simulator.generate_avatar") as mock_avatar:
            result = await run_simulation(request, db=None)

            # Avatar generation should not be called
            mock_avatar.assert_not_called()

            # Agent should not have a portrait
            assert result["agents"]["Alice"]["portrait"] is None
