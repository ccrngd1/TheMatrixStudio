# SPDX-License-Identifier: Apache-2.0
"""
Startup stale-run sweep (post-Phase-2a hardening).

When the server dies/restarts mid-generation, the runner never reaches its
terminal status update, so the run is left in ``status="running"`` forever with
no failure marker. On a fresh process no run can hold a live background task, so
the sweep marks every lingering "running" run as terminal ``interrupted`` and
records a ``sim.interrupted`` event. These tests exercise that logic directly
(no LLM, no live server) plus the supporting DB helpers.
"""

import tempfile
from pathlib import Path

import json

import pytest

from matrix_studio.api.app import sweep_stale_running_runs
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


async def _seed(db, run_id, status, turns=0):
    await db.create_run(run_id=run_id, topic="T", cast=[{"name": "A", "persona": "p"}])
    for t in range(1, turns + 1):
        await db.append_event(
            run_id=run_id, turn=t, seq=t, event_type="agent.response",
            agent_name="A", payload={"speaker": "A", "message": f"m{t}"},
        )
    await db.update_run_status(run_id, status)


async def test_sweep_marks_orphaned_running_as_interrupted(db):
    await _seed(db, "orphan", "running", turns=3)

    swept = await sweep_stale_running_runs(db)

    assert swept == ["orphan"]
    run = await db.get_run("orphan")
    assert run["status"] == "interrupted"
    assert run["completed_at"] is not None


async def test_sweep_appends_interrupted_event_at_last_turn(db):
    await _seed(db, "orphan", "running", turns=3)
    seq_before = await db.max_seq("orphan")

    await sweep_stale_running_runs(db)

    events = await db.get_events("orphan")
    interrupted = [e for e in events if e["event_type"] == "sim.interrupted"]
    assert len(interrupted) == 1
    evt = interrupted[0]
    payload = json.loads(evt["payload"])
    # Recorded at the last observed turn, with a monotonic seq after the rest.
    assert evt["turn"] == 3
    assert evt["seq"] == seq_before + 1
    assert payload["at_turn"] == 3
    assert "restart" in payload["reason"].lower()


async def test_sweep_leaves_terminal_runs_untouched(db):
    await _seed(db, "done", "complete", turns=2)
    await _seed(db, "boom", "failed", turns=1)

    swept = await sweep_stale_running_runs(db)

    assert swept == []
    assert (await db.get_run("done"))["status"] == "complete"
    assert (await db.get_run("boom"))["status"] == "failed"
    # No spurious interruption events added anywhere.
    for rid in ("done", "boom"):
        evs = await db.get_events(rid)
        assert not [e for e in evs if e["event_type"] == "sim.interrupted"]


async def test_sweep_handles_running_run_with_no_events(db):
    # A run that died before emitting any event: turn 0, seq starts at 0.
    await _seed(db, "empty", "running", turns=0)

    swept = await sweep_stale_running_runs(db)

    assert swept == ["empty"]
    assert (await db.get_run("empty"))["status"] == "interrupted"
    events = await db.get_events("empty")
    interrupted = [e for e in events if e["event_type"] == "sim.interrupted"]
    assert len(interrupted) == 1
    assert interrupted[0]["turn"] == 0
    assert interrupted[0]["seq"] == 0


async def test_sweep_is_idempotent(db):
    await _seed(db, "orphan", "running", turns=2)

    first = await sweep_stale_running_runs(db)
    second = await sweep_stale_running_runs(db)

    assert first == ["orphan"]
    assert second == []  # already interrupted; nothing left to sweep
    events = await db.get_events("orphan")
    assert len([e for e in events if e["event_type"] == "sim.interrupted"]) == 1


async def test_last_event_turn_and_last_event_at_helpers(db):
    await _seed(db, "r", "running", turns=4)
    assert await db.last_event_turn("r") == 4
    stats = await db.get_run_stats("r")
    assert stats["last_event_at"] is not None
    # A run with no events reports turn 0 and null recency.
    await _seed(db, "e", "running", turns=0)
    assert await db.last_event_turn("e") == 0
    assert (await db.get_run_stats("e"))["last_event_at"] is None


def test_lifespan_startup_sweeps_orphaned_running_run():
    """
    End-to-end: seed a DB with a lingering "running" run, then boot the real app
    (TestClient enters the lifespan, which runs the sweep) and confirm the run
    is surfaced as "interrupted" over the API — exactly what happens to a run
    orphaned by a server restart mid-generation (the azure-vector test case).
    """
    import asyncio
    import tempfile
    from fastapi.testclient import TestClient
    from matrix_studio.api.app import create_app

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    async def _seed_db():
        d = Database(db_path)
        await d.connect()
        await _seed(d, "azure-vector", "running", turns=5)
        await d.close()

    asyncio.run(_seed_db())

    app = create_app(db_path=db_path)
    with TestClient(app) as client:  # entering the context runs lifespan
        resp = client.get("/api/runs/azure-vector")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "interrupted"
        assert body["completed_at"] is not None

    Path(db_path).unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# The sweep must be switchable off for multi-process deployments
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_sweep_can_be_disabled_and_then_leaves_runs_alone(
    tmp_path, monkeypatch
):
    """`STARTUP_SWEEP=false` is a correctness requirement on Lambda, not a tuning knob.

    The sweep's premise is "this is the only process, so nothing can still be
    running". Under Lambda that is false: several sandboxes serve the same data,
    and two concurrent cold starts would each conclude the other's in-flight run
    was orphaned and mark it `interrupted`. That is one request terminating
    another user's live conversation — which is why this is asserted through the
    real app lifespan rather than by calling the sweep function directly. The bug
    would be in the wiring, not in the sweep.
    """
    from fastapi.testclient import TestClient

    from matrix_studio.api.app import create_app

    db_path = str(tmp_path / "sweep.db")
    seeded = Database(db_path)
    await seeded.connect()
    await seeded.create_run(
        run_id="live", topic="T", cast=[{"name": "A", "persona": "p"}]
    )
    await seeded.update_run_status("live", "running")
    await seeded.close()

    monkeypatch.setenv("STARTUP_SWEEP", "false")
    with TestClient(create_app(db_path)):
        pass  # entering and leaving the context runs the full lifespan

    after = Database(db_path)
    await after.connect()
    try:
        run = await after.get_run("live")
        assert run["status"] == "running", (
            "the sweep ran despite STARTUP_SWEEP=false — on Lambda this would "
            "terminate a live run belonging to a concurrent request"
        )
    finally:
        await after.close()


@pytest.mark.asyncio
async def test_the_sweep_still_runs_by_default(tmp_path):
    """The opt-out must not have become the default.

    Paired with the test above deliberately: a change that disabled the sweep
    everywhere would satisfy that one and break this one, and the single-server
    case is where the sweep is genuinely needed — without it a crashed run reports
    as live forever.
    """
    from fastapi.testclient import TestClient

    from matrix_studio.api.app import create_app

    db_path = str(tmp_path / "sweep-default.db")
    seeded = Database(db_path)
    await seeded.connect()
    await seeded.create_run(
        run_id="orphan", topic="T", cast=[{"name": "A", "persona": "p"}]
    )
    await seeded.update_run_status("orphan", "running")
    await seeded.close()

    with TestClient(create_app(db_path)):
        pass

    after = Database(db_path)
    await after.connect()
    try:
        assert (await after.get_run("orphan"))["status"] == "interrupted"
    finally:
        await after.close()
