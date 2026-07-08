# SPDX-License-Identifier: Apache-2.0
"""Tests for storage module - event-sourced SQLite database."""

import tempfile
import time
from pathlib import Path

import pytest

from matrix_studio.state import AgentState, MemoryItem, SimSnapshot
from matrix_studio.storage import Database


@pytest.fixture
async def db():
    """Create a temporary test database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()

    # Cleanup
    Path(db_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_create_run(db):
    """Test creating a simulation run."""
    run_id = "test-run-123"
    topic = "Test conversation"
    cast = [
        {"name": "Alice", "persona": "Friendly", "goals": ["Be helpful"]},
        {"name": "Bob", "persona": "Curious", "goals": ["Learn things"]},
    ]
    config = {"max_messages": 10}

    await db.create_run(
        run_id=run_id,
        topic=topic,
        cast=cast,
        config=config,
    )

    # Verify run was created
    run = await db.get_run(run_id)
    assert run is not None
    assert run["id"] == run_id
    assert run["topic"] == topic
    assert run["status"] == "pending"


@pytest.mark.asyncio
async def test_update_run_status(db):
    """Test updating run status."""
    run_id = "test-run-456"
    await db.create_run(
        run_id=run_id,
        topic="Test",
        cast=[{"name": "Alice", "persona": "Test"}],
    )

    # Update to running
    await db.update_run_status(run_id, "running")
    run = await db.get_run(run_id)
    assert run["status"] == "running"

    # Update to complete with timestamp
    completion_time = int(time.time())
    await db.update_run_status(run_id, "complete", completion_time)
    run = await db.get_run(run_id)
    assert run["status"] == "complete"
    assert run["completed_at"] == completion_time


@pytest.mark.asyncio
async def test_append_events(db):
    """Test appending events to the event log."""
    run_id = "test-run-789"
    await db.create_run(
        run_id=run_id,
        topic="Test",
        cast=[{"name": "Alice", "persona": "Test"}],
    )

    # Append multiple events
    await db.append_event(
        run_id=run_id,
        turn=0,
        seq=0,
        event_type="sim.started",
        payload={"topic": "Test", "agent_count": 2},
    )

    await db.append_event(
        run_id=run_id,
        turn=1,
        seq=0,
        event_type="speaker.selected",
        agent_name="Alice",
        payload={"speaker": "Alice", "candidates": ["Alice", "Bob"]},
    )

    await db.append_event(
        run_id=run_id,
        turn=1,
        seq=1,
        event_type="agent.response",
        agent_name="Alice",
        payload={
            "speaker": "Alice",
            "message": "Hello",
            "tokens_in": 100,
            "tokens_out": 50,
            "cost_usd": 0.001,
        },
    )

    # Retrieve events
    events = await db.get_events(run_id)
    assert len(events) == 3
    assert events[0]["event_type"] == "sim.started"
    assert events[1]["event_type"] == "speaker.selected"
    assert events[1]["agent_name"] == "Alice"
    assert events[2]["event_type"] == "agent.response"


@pytest.mark.asyncio
async def test_get_events_range(db):
    """Test retrieving events by turn range."""
    run_id = "test-run-range"
    await db.create_run(
        run_id=run_id,
        topic="Test",
        cast=[{"name": "Alice", "persona": "Test"}],
    )

    # Add events across multiple turns
    for turn in range(5):
        await db.append_event(
            run_id=run_id,
            turn=turn,
            seq=0,
            event_type="test.event",
            payload={"turn": turn},
        )

    # Get events from turn 2 to 4
    events = await db.get_events(run_id, from_turn=2, to_turn=4)
    assert len(events) == 3
    assert events[0]["turn"] == 2
    assert events[-1]["turn"] == 4


@pytest.mark.asyncio
async def test_save_and_retrieve_snapshot(db):
    """Test saving and retrieving a full state snapshot."""
    run_id = "test-run-snapshot"
    await db.create_run(
        run_id=run_id,
        topic="Test",
        cast=[{"name": "Alice", "persona": "Test"}],
    )

    # Create a snapshot
    agent = AgentState(
        name="Alice",
        persona="Friendly helper",
        goals=["Be helpful"],
        total_tokens_in=200,
        total_tokens_out=150,
        total_cost_usd=0.005,
    )

    snapshot = SimSnapshot(
        run_id=run_id,
        turn=5,
        topic="Test conversation",
        agents={"Alice": agent},
        conversation=[
            {"speaker": "Alice", "content": "Hello!", "turn": 1},
        ],
        status="running",
        created_at=int(time.time()),
        total_turns=5,
    )

    # Save snapshot
    await db.save_snapshot(snapshot)

    # Retrieve snapshot
    retrieved = await db.get_snapshot(run_id, turn=5)
    assert retrieved is not None
    assert retrieved.run_id == run_id
    assert retrieved.turn == 5
    assert "Alice" in retrieved.agents
    assert retrieved.agents["Alice"].name == "Alice"
    assert retrieved.agents["Alice"].total_cost_usd == 0.005


@pytest.mark.asyncio
async def test_get_latest_snapshot(db):
    """Test retrieving the latest snapshot."""
    run_id = "test-run-latest"
    await db.create_run(
        run_id=run_id,
        topic="Test",
        cast=[{"name": "Alice", "persona": "Test"}],
    )

    # Save multiple snapshots
    for turn in [5, 10, 15]:
        snapshot = SimSnapshot(
            run_id=run_id,
            turn=turn,
            topic="Test",
            agents={},
            conversation=[],
            status="running",
            created_at=int(time.time()),
            total_turns=turn,
        )
        await db.save_snapshot(snapshot)

    # Get latest should return turn 15
    latest = await db.get_snapshot(run_id)
    assert latest is not None
    assert latest.turn == 15


@pytest.mark.asyncio
async def test_branching_runs(db):
    """Test creating a branched run from a parent."""
    parent_run_id = "parent-run"
    await db.create_run(
        run_id=parent_run_id,
        topic="Original conversation",
        cast=[{"name": "Alice", "persona": "Test"}],
    )

    # Create a branched run
    branch_run_id = "branch-run"
    await db.create_run(
        run_id=branch_run_id,
        topic="Branched conversation",
        cast=[{"name": "Alice", "persona": "Test"}],
        parent_run_id=parent_run_id,
        branch_turn=5,
    )

    # Verify branch metadata
    branch = await db.get_run(branch_run_id)
    assert branch["parent_run_id"] == parent_run_id
    assert branch["branch_turn"] == 5
