# SPDX-License-Identifier: Apache-2.0
"""
Resume-in-place (error recovery) tests.

An interrupted/failed run can be RESUMED forward in place: it keeps its run
id/codename, trims the dangling tail past its last checkpoint, and continues
generating from that checkpoint. This is distinct from a branch (which forks a
new run to protect a completed, canonical timeline) and is only allowed for
non-completed runs.

LLM calls are mocked at the litellm seam; no live/billable calls.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio import branching
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


class _Resp:
    def __init__(self, content, tokens_in=10, tokens_out=5):
        self.choices = [MagicMock(message=MagicMock(content=content))]
        self.usage = MagicMock(prompt_tokens=tokens_in, completion_tokens=tokens_out)
        self._hidden_params = {"response_cost": 0.001}


def _always(names):
    """Odd calls select a speaker; even calls emit a message. Never runs dry."""
    def factory(*args, **kwargs):
        factory.n += 1
        if factory.n % 2 == 1:
            return _Resp(names[(factory.n // 2) % len(names)])
        return _Resp(f"reply {factory.n}")
    factory.n = 0
    return factory


async def _seed_interrupted(db, run_id="orphan", complete_turns=2, max_messages=4):
    """
    Build an interrupted run with ``complete_turns`` finished turns (each with an
    agent.response + a per-turn checkpoint) plus a DANGLING next turn (a
    speaker.selected with no response) and a sim.interrupted marker — exactly the
    orphaned-mid-turn shape a killed server leaves behind.
    """
    cast = [
        {"name": "Ada", "persona": "An ethicist", "goals": ["probe"]},
        {"name": "Ben", "persona": "An engineer", "goals": ["ship"]},
    ]
    await db.create_run(
        run_id=run_id, topic="AI ethics", cast=cast,
        config={"max_messages": max_messages, "generate_avatars": False},
    )
    seq = 0
    for t in range(1, complete_turns + 1):
        speaker = cast[(t - 1) % 2]["name"]
        await db.append_event(
            run_id=run_id, turn=t, seq=seq, event_type="speaker.selected",
            agent_name=speaker, payload={"speaker": speaker},
        )
        seq += 1
        await db.append_event(
            run_id=run_id, turn=t, seq=seq, event_type="agent.response",
            agent_name=speaker,
            payload={"speaker": speaker, "message": f"m{t}",
                     "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.001},
        )
        seq += 1
        await db.append_event(
            run_id=run_id, turn=t, seq=seq, event_type="checkpoint.saved",
            payload={"turn": t},
        )
        seq += 1
        await db.save_snapshot(SimSnapshot(
            run_id=run_id, turn=t, topic="AI ethics",
            agents={c["name"]: AgentState(name=c["name"], persona=c["persona"]) for c in cast},
            conversation=[{"speaker": cast[(i) % 2]["name"], "content": f"m{i+1}", "turn": i + 1}
                          for i in range(t)],
            status="running", created_at=0, total_turns=t,
        ))
    # Dangling next turn: a speaker was selected, then the process was killed.
    dangling_turn = complete_turns + 1
    await db.append_event(
        run_id=run_id, turn=dangling_turn, seq=seq, event_type="speaker.selected",
        agent_name="Ada", payload={"speaker": "Ada"},
    )
    seq += 1
    await db.append_event(
        run_id=run_id, turn=dangling_turn, seq=seq, event_type="sim.interrupted",
        payload={"reason": "server restarted", "at_turn": complete_turns},
    )
    await db.update_run_status(run_id, "interrupted")
    return run_id


async def test_truncate_after_turn_trims_events_and_snapshots(db):
    await _seed_interrupted(db, complete_turns=2)
    removed = await db.truncate_after_turn("orphan", 2)
    assert removed == 2  # dangling speaker.selected + sim.interrupted
    events = await db.get_events("orphan")
    assert max(e["turn"] for e in events) == 2
    assert not [e for e in events if e["event_type"] == "sim.interrupted"]
    assert await db.last_checkpoint_turn("orphan") == 2


async def test_last_checkpoint_turn(db):
    await _seed_interrupted(db, complete_turns=3)
    assert await db.last_checkpoint_turn("orphan") == 3
    await db.create_run(run_id="empty", topic="t", cast=[{"name": "A", "persona": "p"}])
    assert await db.last_checkpoint_turn("empty") is None


async def test_resume_continues_in_place_and_completes(db):
    await _seed_interrupted(db, complete_turns=2, max_messages=4)
    run = await db.get_run("orphan")

    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_always(["Ada", "Ben"]),
    ):
        result = await branching.resume_run_in_place(db, run)

    # Same run id (in place); it finished forward to the configured budget.
    assert result.get("status") == "complete"
    after = await db.get_run("orphan")
    assert after["status"] == "complete"
    stats = await db.get_run_stats("orphan")
    assert stats["turn_count"] == 4  # 2 pre-existing + 2 generated

    # The dangling tail was trimmed and no phantom interrupt marker remains
    # mid-stream; the log ends on a terminal completion.
    events = await db.get_events("orphan")
    assert not [e for e in events if e["event_type"] == "sim.interrupted"]
    assert events[-1]["event_type"] in ("sim.completed", "sim.failed")
    assert events[-1]["event_type"] == "sim.completed"


async def test_resume_from_zero_when_no_checkpoint(db):
    # Interrupted before any turn landed: no checkpoint, no events.
    await db.create_run(
        run_id="early", topic="AI ethics",
        cast=[{"name": "Ada", "persona": "e", "goals": []},
              {"name": "Ben", "persona": "eng", "goals": []}],
        config={"max_messages": 3, "generate_avatars": False},
    )
    await db.update_run_status("early", "interrupted")
    run = await db.get_run("early")

    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_always(["Ada", "Ben"]),
    ):
        await branching.resume_run_in_place(db, run)

    after = await db.get_run("early")
    assert after["status"] == "complete"
    assert (await db.get_run_stats("early"))["turn_count"] == 3


async def test_resume_rejects_completed_run_via_manager(db):
    from matrix_studio.api.manager import RunManager
    await db.create_run(run_id="done", topic="t", cast=[{"name": "A", "persona": "p"}])
    await db.update_run_status("done", "complete")
    mgr = RunManager(db)
    run = await db.get_run("done")
    with pytest.raises(ValueError):
        await mgr.resume_run(run)
    # Untouched.
    assert (await db.get_run("done"))["status"] == "complete"
