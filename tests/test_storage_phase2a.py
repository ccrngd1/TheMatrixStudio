# SPDX-License-Identifier: Apache-2.0
"""
Phase 2a storage tests — snapshot listing/fetch by turn and the branch-support
helpers (copy_events_upto, max_seq, list_branches). No LLM involved.
"""

import tempfile
from pathlib import Path

import pytest

from matrix_studio.state import AgentState, SimSnapshot
from matrix_studio.storage import Database


@pytest.fixture
async def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()
    Path(db_path).unlink(missing_ok=True)


async def _seed_run(db, run_id, turns, status_final="complete"):
    await db.create_run(run_id=run_id, topic="T", cast=[{"name": "A", "persona": "p"}])
    for t in range(1, turns + 1):
        await db.append_event(
            run_id=run_id, turn=t, seq=t, event_type="agent.response",
            agent_name="A",
            payload={"speaker": "A", "message": f"m{t}", "cost_usd": 0.001},
        )
        status = status_final if t == turns else "running"
        await db.save_snapshot(
            SimSnapshot(
                run_id=run_id, turn=t, topic="T",
                agents={"A": AgentState(name="A", persona="p")},
                conversation=[{"speaker": "A", "content": f"m{t}", "turn": t}],
                status=status, created_at=0, total_turns=t,
            )
        )


@pytest.mark.asyncio
async def test_list_snapshots_by_turn(db):
    await _seed_run(db, "r1", 3)
    snaps = await db.list_snapshots("r1")
    assert [s["turn"] for s in snaps] == [1, 2, 3]
    assert snaps[0]["status"] == "running"
    assert snaps[-1]["status"] == "complete"


@pytest.mark.asyncio
async def test_get_snapshot_by_turn_and_missing(db):
    await _seed_run(db, "r1", 3)
    snap = await db.get_snapshot("r1", turn=2)
    assert snap is not None and snap.turn == 2
    assert len(snap.conversation) == 1
    # A turn with no checkpoint returns None (route maps this to 404).
    assert await db.get_snapshot("r1", turn=99) is None


@pytest.mark.asyncio
async def test_copy_events_upto_and_max_seq(db):
    await _seed_run(db, "parent", 5)
    # Destination run must exist (FK).
    await db.create_run(run_id="child", topic="T", cast=[{"name": "A", "persona": "p"}],
                        parent_run_id="parent", branch_turn=3)

    copied = await db.copy_events_upto("parent", "child", upto_turn=3)
    child_events = await db.get_events("child")
    assert copied == len(child_events)
    assert all(e["turn"] <= 3 for e in child_events)
    # seq preserved; max_seq reflects the highest copied seq.
    assert await db.max_seq("child") == max(e["seq"] for e in child_events)

    # Parent is untouched by the copy (immutability at the storage layer).
    parent_events = await db.get_events("parent")
    assert len(parent_events) == 5
    assert max(e["turn"] for e in parent_events) == 5


@pytest.mark.asyncio
async def test_max_seq_empty_run(db):
    await db.create_run(run_id="empty", topic="T", cast=[{"name": "A", "persona": "p"}])
    assert await db.max_seq("empty") == -1


@pytest.mark.asyncio
async def test_list_branches(db):
    await _seed_run(db, "parent", 4)
    await db.create_run(run_id="b1", topic="T", cast=[{"name": "A", "persona": "p"}],
                        name="child-one", parent_run_id="parent", branch_turn=2)
    await db.create_run(run_id="b2", topic="T", cast=[{"name": "A", "persona": "p"}],
                        name="child-two", parent_run_id="parent", branch_turn=3)

    branches = await db.list_branches("parent")
    ids = {b["run_id"] for b in branches}
    assert ids == {"b1", "b2"}
    by_id = {b["run_id"]: b for b in branches}
    assert by_id["b1"]["branch_turn"] == 2
    assert by_id["b2"]["name"] == "child-two"
    # A run with no children returns an empty list.
    assert await db.list_branches("b1") == []
