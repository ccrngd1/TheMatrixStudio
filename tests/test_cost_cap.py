# SPDX-License-Identifier: Apache-2.0
"""
Phase 3 tests for the cost cap feature.

The cost cap is an additive, opt-in feature that stops generation when accumulated
real cost reaches a USD ceiling. When the cap is 0 (default), behavior MUST be
byte-for-byte identical to pre-Phase-3.
"""

import pytest
from matrix_studio.engine import run_simulation
from matrix_studio.settings import Settings, get_settings
from matrix_studio.storage import Database


@pytest.mark.asyncio
async def test_cost_cap_off_unchanged_behavior(tmp_path, monkeypatch):
    """
    Regression lock: with max_run_cost_usd=0 (default), runs behave byte-for-byte
    as pre-Phase-3. The cost cap adds zero overhead and emits no cap-related events.
    """
    # Mock litellm to return zero cost (simulating a provider that doesn't report cost)
    from unittest.mock import AsyncMock, MagicMock
    import litellm

    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="Test response"))]
    mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=20)
    # No cost reported
    mock_response._hidden_params = {}

    async def mock_acompletion(*args, **kwargs):
        return mock_response

    monkeypatch.setattr(litellm, "acompletion", mock_acompletion)

    # Override settings to ensure cap is OFF (disable .env loading for tests)
    settings = Settings(max_run_cost_usd=0.0, data_dir=str(tmp_path), _env_file=None)
    monkeypatch.setattr("matrix_studio.settings._settings", settings)
    monkeypatch.setattr("matrix_studio.engine.simulator.get_settings", lambda: settings)

    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    request = {
        "topic": "test topic",
        "cast": [
            {"name": "Alice", "persona": "Alice persona", "goals": ["goal1"]},
            {"name": "Bob", "persona": "Bob persona", "goals": ["goal2"]},
        ],
        "config": {"max_messages": 3, "generate_avatars": False},
    }

    result = await run_simulation(request, db=db)

    # Assert run completed normally (not capped)
    assert result["status"] == "complete"
    assert "cap_usd" not in result

    # Assert no sim.capped event in the database
    events = await db.get_events(result["run_id"])
    event_types = [e["event_type"] for e in events]
    assert "sim.capped" not in event_types
    assert "sim.completed" in event_types

    await db.close()


@pytest.mark.asyncio
async def test_cost_cap_hit(tmp_path, monkeypatch):
    """
    With a cap set below projected cost, the run stops at/under the cap and emits
    sim.capped. The final status is 'capped' (terminal).
    """
    from unittest.mock import AsyncMock, MagicMock
    import litellm

    # Mock litellm to return a cost of $0.10 per call
    call_count = 0

    async def mock_acompletion(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content=f"Response {call_count}"))]
        mock_response.usage = MagicMock(prompt_tokens=100, completion_tokens=200)
        mock_response._hidden_params = {"response_cost": 0.10}
        return mock_response

    monkeypatch.setattr(litellm, "acompletion", mock_acompletion)

    # Set a cap of $0.25 - should hit after 2-3 turns (each turn has 2 LLM calls: select + generate)
    settings = Settings(max_run_cost_usd=0.25, data_dir=str(tmp_path), _env_file=None)
    monkeypatch.setattr("matrix_studio.settings._settings", settings)
    monkeypatch.setattr("matrix_studio.engine.simulator.get_settings", lambda: settings)

    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    request = {
        "topic": "test topic",
        "cast": [
            {"name": "Alice", "persona": "Alice persona", "goals": ["goal1"]},
            {"name": "Bob", "persona": "Bob persona", "goals": ["goal2"]},
        ],
        "config": {"max_messages": 10, "generate_avatars": False},
    }

    result = await run_simulation(request, db=db)

    # Assert run was capped before reaching max_messages
    assert result["status"] == "capped"
    assert result["total_turns"] < 10
    assert result["total_cost_usd"] >= 0.25
    assert result["cap_usd"] == 0.25

    # Assert sim.capped event exists
    events = await db.get_events(result["run_id"])
    event_types = [e["event_type"] for e in events]
    assert "sim.capped" in event_types
    assert "sim.completed" not in event_types

    # Find the capped event and verify payload
    import json
    capped_event = next(e for e in events if e["event_type"] == "sim.capped")
    payload = json.loads(capped_event["payload"]) if isinstance(capped_event["payload"], str) else capped_event["payload"]
    assert payload["cap_usd"] == 0.25
    assert payload["total_cost_usd"] >= 0.25

    # Check final snapshot has status "capped"
    from matrix_studio.storage.database import Database as DB
    snapshot = await db.get_snapshot(result["run_id"], result["total_turns"])
    assert snapshot is not None
    assert snapshot.status == "capped"

    await db.close()


@pytest.mark.asyncio
async def test_cost_cap_not_hit(tmp_path, monkeypatch):
    """
    With a cap set above the run's total cost, the run completes normally and emits
    sim.completed (not sim.capped).
    """
    from unittest.mock import AsyncMock, MagicMock
    import litellm

    # Mock litellm to return a small cost
    async def mock_acompletion(*args, **kwargs):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="Test response"))]
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=20)
        mock_response._hidden_params = {"response_cost": 0.01}
        return mock_response

    monkeypatch.setattr(litellm, "acompletion", mock_acompletion)

    # Set a high cap that won't be reached
    settings = Settings(max_run_cost_usd=10.0, data_dir=str(tmp_path), _env_file=None)
    monkeypatch.setattr("matrix_studio.settings._settings", settings)
    monkeypatch.setattr("matrix_studio.engine.simulator.get_settings", lambda: settings)

    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    request = {
        "topic": "test topic",
        "cast": [
            {"name": "Alice", "persona": "Alice persona", "goals": ["goal1"]},
            {"name": "Bob", "persona": "Bob persona", "goals": ["goal2"]},
        ],
        "config": {"max_messages": 3, "generate_avatars": False},
    }

    result = await run_simulation(request, db=db)

    # Assert run completed normally
    assert result["status"] == "complete"
    assert result["total_cost_usd"] < 10.0

    # Assert sim.completed (not capped)
    events = await db.get_events(result["run_id"])
    event_types = [e["event_type"] for e in events]
    assert "sim.completed" in event_types
    assert "sim.capped" not in event_types

    await db.close()


@pytest.mark.asyncio
async def test_cost_cap_zero_cost_providers(tmp_path, monkeypatch):
    """
    When litellm reports no cost (e.g., local Ollama), the cap never triggers.
    We count $0 for uncounted calls; never fabricate cost.
    """
    from unittest.mock import AsyncMock, MagicMock
    import litellm

    # Mock litellm with NO cost reporting
    async def mock_acompletion(*args, **kwargs):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="Test response"))]
        mock_response.usage = MagicMock(prompt_tokens=100, completion_tokens=200)
        mock_response._hidden_params = {}  # NO response_cost
        return mock_response

    monkeypatch.setattr(litellm, "acompletion", mock_acompletion)

    # Set a cap - should NOT trigger since cost stays at $0
    settings = Settings(max_run_cost_usd=0.10, data_dir=str(tmp_path), _env_file=None)
    monkeypatch.setattr("matrix_studio.settings._settings", settings)
    monkeypatch.setattr("matrix_studio.engine.simulator.get_settings", lambda: settings)

    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    request = {
        "topic": "test topic",
        "cast": [
            {"name": "Alice", "persona": "Alice persona", "goals": ["goal1"]},
            {"name": "Bob", "persona": "Bob persona", "goals": ["goal2"]},
        ],
        "config": {"max_messages": 3, "generate_avatars": False},
    }

    result = await run_simulation(request, db=db)

    # Assert run completed normally (cap never hit since cost is $0)
    assert result["status"] == "complete"
    assert result["total_cost_usd"] == 0.0

    events = await db.get_events(result["run_id"])
    event_types = [e["event_type"] for e in events]
    assert "sim.completed" in event_types
    assert "sim.capped" not in event_types

    await db.close()
