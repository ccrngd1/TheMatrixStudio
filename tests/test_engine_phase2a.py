# SPDX-License-Identifier: Apache-2.0
"""
Phase 2a engine tests — per-turn checkpointing + resume-from-state.

All LLM calls are MOCKED (the real env carries a Bedrock key). These verify the
ADDITIVE engine behavior:
  * a fresh run persists a full snapshot PER TURN (checkpoint count == turn
    count) plus emits an additive checkpoint.saved event,
  * the fresh-start Phase 0 result contract is unchanged,
  * resume_simulation seeds from provided state and generates only NEW turns
    forward, continuing seq/cost without re-running the seed turns.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio.engine import resume_simulation, run_simulation
from matrix_studio.state import AgentState
from matrix_studio.storage import Database


class MockLiteLLMResponse:
    def __init__(self, content: str, tokens_in: int = 100, tokens_out: int = 50):
        self.choices = [MagicMock(message=MagicMock(content=content))]
        self.usage = MagicMock(prompt_tokens=tokens_in, completion_tokens=tokens_out)
        self._hidden_params = {"response_cost": 0.001}


@pytest.fixture
async def test_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()
    Path(db_path).unlink(missing_ok=True)


def _scripted(turns: int):
    """A select+generate response pair per turn."""
    responses = []
    for i in range(turns):
        responses.append(MockLiteLLMResponse("Alice"))
        responses.append(MockLiteLLMResponse(f"Message {i + 1}"))
    return responses


@pytest.mark.asyncio
async def test_checkpoint_count_equals_turn_count(test_db):
    """A fresh run persists exactly one snapshot per turn (running turns 1..N-1
    replaced by the completion snapshot at N — all keyed on UNIQUE(run_id,turn),
    so distinct turns == distinct snapshots == turn count)."""
    request = {
        "topic": "Checkpointing",
        "cast": [{"name": "Alice", "persona": "Test", "goals": []}],
        "config": {"max_messages": 4, "generate_avatars": False},
    }
    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock:
        mock.side_effect = _scripted(4)
        result = await run_simulation(request, db=test_db)

    run_id = result["run_id"]
    assert result["total_turns"] == 4

    snapshots = await test_db.list_snapshots(run_id)
    turns = [s["turn"] for s in snapshots]
    assert turns == [1, 2, 3, 4]  # one checkpoint per turn
    assert len(snapshots) == result["total_turns"]

    # Turns 1..3 are running checkpoints; the final turn is the completion snap.
    statuses = {s["turn"]: s["status"] for s in snapshots}
    assert statuses[1] == "running"
    assert statuses[3] == "running"
    assert statuses[4] == "complete"


@pytest.mark.asyncio
async def test_checkpoint_saved_event_emitted_per_turn(test_db):
    """An additive checkpoint.saved {turn} event fires once per turn."""
    request = {
        "topic": "Events",
        "cast": [{"name": "Alice", "persona": "Test", "goals": []}],
        "config": {"max_messages": 3, "generate_avatars": False},
    }
    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock:
        mock.side_effect = _scripted(3)
        result = await run_simulation(request, db=test_db)

    events = await test_db.get_events(result["run_id"])
    checkpoint_turns = [
        e["turn"] for e in events if e["event_type"] == "checkpoint.saved"
    ]
    assert checkpoint_turns == [1, 2, 3]


@pytest.mark.asyncio
async def test_per_turn_snapshot_reconstructs_state(test_db):
    """The snapshot at turn N holds exactly the conversation up to turn N."""
    request = {
        "topic": "Reconstruct",
        "cast": [{"name": "Alice", "persona": "Test", "goals": []}],
        "config": {"max_messages": 3, "generate_avatars": False},
    }
    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock:
        mock.side_effect = _scripted(3)
        result = await run_simulation(request, db=test_db)

    snap2 = await test_db.get_snapshot(result["run_id"], turn=2)
    assert snap2 is not None
    assert snap2.status == "running"
    assert len(snap2.conversation) == 2
    assert snap2.conversation[-1]["turn"] == 2


@pytest.mark.asyncio
async def test_fresh_start_result_contract_unchanged(test_db):
    """Phase 0 result shape/values are unchanged by the additive checkpointing."""
    request = {
        "topic": "Contract",
        "cast": [{"name": "Alice", "persona": "Test", "goals": []}],
        "config": {"max_messages": 2, "generate_avatars": False},
    }
    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock:
        mock.side_effect = _scripted(2)
        result = await run_simulation(request, db=test_db)

    assert result["status"] == "complete"
    assert result["total_turns"] == 2
    assert len(result["conversation"]) == 2
    assert set(result.keys()) >= {
        "run_id",
        "status",
        "topic",
        "conversation",
        "agents",
        "total_turns",
        "total_cost_usd",
    }


@pytest.mark.asyncio
async def test_resume_generates_only_forward_turns(test_db):
    """resume_simulation seeds from turn N and generates only turns N+1..budget,
    carrying seq/cost forward without re-running the seed turns."""
    run_id = "branch-resume"
    await test_db.create_run(
        run_id=run_id, topic="Resumed", cast=[{"name": "Alice", "persona": "p"}]
    )
    # Seed as if 2 turns already happened (copied by the branch service).
    agent = AgentState(
        name="Alice", persona="p", goals=[], total_cost_usd=0.5, total_tokens_in=200
    )
    conversation = [
        {"speaker": "Alice", "content": "seed 1", "turn": 1},
        {"speaker": "Alice", "content": "seed 2", "turn": 2},
    ]

    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock:
        # Budget 4 → only turns 3 and 4 should be generated (2 select+gen pairs).
        mock.side_effect = _scripted(2)
        result = await resume_simulation(
            run_id=run_id,
            topic="Resumed",
            agents={"Alice": agent},
            conversation=conversation,
            from_turn=2,
            start_seq=10,
            max_messages=4,
            db=test_db,
        )

    # Two seed turns + two generated == 4 total; only 2 new pairs generated.
    assert result["total_turns"] == 4
    assert len(result["conversation"]) == 4
    assert mock.call_count == 4  # 2 turns * (select + generate)
    # Seed turns are preserved verbatim; new turns appended forward.
    assert result["conversation"][0]["content"] == "seed 1"
    assert result["conversation"][2]["turn"] == 3
    # Accumulated cost carried forward (not reset).
    assert result["agents"]["Alice"]["total_cost_usd"] > 0.5

    # New snapshots exist for the generated turns; seq continues past start_seq.
    snapshots = await test_db.list_snapshots(run_id)
    turns = [s["turn"] for s in snapshots]
    assert 3 in turns and 4 in turns
    assert await test_db.max_seq(run_id) >= 10


@pytest.mark.asyncio
async def test_resume_at_budget_generates_nothing(test_db):
    """Resuming with from_turn == budget generates zero new turns (guarded by
    the service's budget extension; the engine itself simply completes)."""
    run_id = "branch-atbudget"
    await test_db.create_run(
        run_id=run_id, topic="AtBudget", cast=[{"name": "Alice", "persona": "p"}]
    )
    agent = AgentState(name="Alice", persona="p", goals=[])
    conversation = [{"speaker": "Alice", "content": "x", "turn": 1}]

    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock:
        mock.side_effect = _scripted(0)
        result = await resume_simulation(
            run_id=run_id,
            topic="AtBudget",
            agents={"Alice": agent},
            conversation=conversation,
            from_turn=2,
            start_seq=5,
            max_messages=2,
            db=test_db,
        )
    assert result["total_turns"] == 2
    assert mock.call_count == 0
