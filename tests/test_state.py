# SPDX-License-Identifier: Apache-2.0
"""Tests for state models - verifying schema versioning and serialization."""

import json
import time

import pytest

from matrix_studio.state import AgentState, MemoryItem, SimSnapshot


def test_memory_item_has_versioning():
    """Test that MemoryItem includes type and schema_version fields."""
    memory = MemoryItem(
        timestamp=int(time.time()),
        content="Remember this important fact",
        importance=0.8,
        tags=["fact", "important"],
    )

    assert memory.type == "MemoryItem"
    assert memory.schema_version == "1.0.0"


def test_agent_state_has_versioning():
    """Test that AgentState includes type and schema_version fields."""
    agent = AgentState(
        name="Alice",
        persona="Friendly and helpful",
        goals=["Help others", "Learn new things"],
    )

    assert agent.type == "AgentState"
    assert agent.schema_version == "1.0.0"


def test_sim_snapshot_has_versioning():
    """Test that SimSnapshot includes type and schema_version fields."""
    snapshot = SimSnapshot(
        run_id="test-123",
        turn=5,
        topic="Test topic",
        agents={},
        status="running",
        created_at=int(time.time()),
        total_turns=5,
    )

    assert snapshot.type == "SimSnapshot"
    assert snapshot.schema_version == "1.0.0"


def test_agent_state_serialization():
    """Test that AgentState can be serialized and deserialized."""
    agent = AgentState(
        name="Bob",
        persona="Curious researcher",
        goals=["Discover new things", "Ask questions"],
        total_tokens_in=1000,
        total_tokens_out=500,
        total_cost_usd=0.05,
    )

    # Serialize to JSON
    json_str = agent.model_dump_json()
    data = json.loads(json_str)

    # Verify key fields are present
    assert data["type"] == "AgentState"
    assert data["schema_version"] == "1.0.0"
    assert data["name"] == "Bob"
    assert data["total_cost_usd"] == 0.05

    # Deserialize back
    restored = AgentState.model_validate_json(json_str)
    assert restored.name == agent.name
    assert restored.persona == agent.persona
    assert restored.total_cost_usd == agent.total_cost_usd


def test_sim_snapshot_serialization():
    """Test that SimSnapshot can be serialized and deserialized."""
    agent1 = AgentState(name="Alice", persona="Helper", goals=["Help"])
    agent2 = AgentState(name="Bob", persona="Learner", goals=["Learn"])

    snapshot = SimSnapshot(
        run_id="test-456",
        turn=10,
        topic="Collaborative learning",
        agents={"Alice": agent1, "Bob": agent2},
        conversation=[
            {"speaker": "Alice", "content": "Hello Bob", "turn": 1},
            {"speaker": "Bob", "content": "Hi Alice", "turn": 2},
        ],
        status="complete",
        created_at=int(time.time()),
        completed_at=int(time.time()),
        total_turns=10,
    )

    # Serialize
    json_str = snapshot.model_dump_json()
    data = json.loads(json_str)

    assert data["type"] == "SimSnapshot"
    assert data["run_id"] == "test-456"
    assert "Alice" in data["agents"]
    assert "Bob" in data["agents"]

    # Deserialize
    restored = SimSnapshot.model_validate_json(json_str)
    assert restored.run_id == snapshot.run_id
    assert restored.turn == snapshot.turn
    assert len(restored.agents) == 2
    assert restored.agents["Alice"].name == "Alice"
    assert restored.agents["Bob"].name == "Bob"


def test_memory_item_with_metadata():
    """Test MemoryItem with custom metadata."""
    memory = MemoryItem(
        timestamp=int(time.time()),
        content="Complex memory with metadata",
        importance=0.9,
        tags=["event", "significant"],
        metadata={"location": "office", "mood": "excited", "participants": ["Alice", "Bob"]},
    )

    # Serialize and deserialize
    json_str = memory.model_dump_json()
    restored = MemoryItem.model_validate_json(json_str)

    assert restored.content == memory.content
    assert restored.metadata["location"] == "office"
    assert "Alice" in restored.metadata["participants"]


def test_agent_state_default_values():
    """Test that AgentState has sensible defaults."""
    agent = AgentState(name="Test", persona="Test persona")

    assert agent.memory_stream == []
    assert agent.goals == []
    assert agent.relationships == {}
    assert agent.conversation_history == []
    assert agent.total_tokens_in == 0
    assert agent.total_tokens_out == 0
    assert agent.total_cost_usd == 0.0
    assert agent.portrait is None


def test_sim_snapshot_with_error():
    """Test SimSnapshot can store error information."""
    snapshot = SimSnapshot(
        run_id="failed-run",
        turn=3,
        topic="Failed simulation",
        agents={},
        status="failed",
        created_at=int(time.time()),
        total_turns=3,
        error_message="Connection timeout",
    )

    assert snapshot.status == "failed"
    assert snapshot.error_message == "Connection timeout"

    # Verify it serializes correctly
    json_str = snapshot.model_dump_json()
    restored = SimSnapshot.model_validate_json(json_str)
    assert restored.error_message == "Connection timeout"


def test_schema_version_for_migration():
    """Test that schema_version field enables future migrations."""
    # Simulate loading old version 0.9.0 data
    old_data = {
        "type": "AgentState",
        "schema_version": "0.9.0",  # Old version
        "name": "OldAgent",
        "persona": "Legacy format",
        "memory_stream": [],
        "goals": [],
        "relationships": {},
        "conversation_history": [],
        "total_tokens_in": 0,
        "total_tokens_out": 0,
        "total_cost_usd": 0.0,
        "portrait": None,
    }

    # Current code can still load it (forward compatibility)
    agent = AgentState.model_validate(old_data)
    assert agent.name == "OldAgent"
    assert agent.schema_version == "0.9.0"  # Preserves old version

    # In a real migration, we'd check this version and upgrade
    # For now, we just verify the field is accessible
