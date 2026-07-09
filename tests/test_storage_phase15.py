# SPDX-License-Identifier: Apache-2.0
"""
Tests for the Phase 1.5 additive storage tables (summaries, threads,
thread_messages) and the read-only invariant at the storage layer: creating
summaries/threads/messages must NOT touch runs/events/snapshots.
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


async def _seed_completed_run(db, run_id="r1"):
    await db.create_run(
        run_id=run_id, topic="pet food policy",
        cast=[{"name": "Ada", "persona": "ethicist"}, {"name": "Ben", "persona": "engineer"}],
        name="trusted-robot",
    )
    await db.append_event(run_id, turn=0, seq=0, event_type="sim.started", payload={"topic": "t"})
    await db.append_event(run_id, turn=1, seq=1, event_type="agent.response",
                          agent_name="Ada", payload={"cost_usd": 0.002, "message": "hi"})
    await db.append_event(run_id, turn=1, seq=2, event_type="sim.completed",
                          payload={"total_cost_usd": 0.002})
    agents = {"Ada": AgentState(name="Ada", persona="ethicist", total_cost_usd=0.002),
              "Ben": AgentState(name="Ben", persona="engineer")}
    snap = SimSnapshot(run_id=run_id, turn=1, topic="pet food policy", agents=agents,
                       conversation=[{"speaker": "Ada", "content": "hi", "turn": 1}],
                       status="complete", created_at=0, completed_at=0, total_turns=1)
    await db.save_snapshot(snap)
    await db.update_run_status(run_id, "complete", 1)


@pytest.mark.asyncio
async def test_summary_generated_and_imported_coexist(db):
    await _seed_completed_run(db)
    await db.save_summary("r1", payload={"overview": "gen"}, kind="generated",
                          tokens_in=10, tokens_out=5, cost_usd=0.003)
    await db.save_summary("r1", payload={"overview": "orig"}, kind="imported")
    rows = await db.get_summaries("r1")
    by_kind = {r["kind"]: r for r in rows}
    assert by_kind["generated"]["payload"]["overview"] == "gen"
    assert by_kind["imported"]["payload"]["overview"] == "orig"


@pytest.mark.asyncio
async def test_regenerate_returns_latest_generated_not_imported(db):
    await _seed_completed_run(db)
    await db.save_summary("r1", payload={"overview": "orig"}, kind="imported")
    await db.save_summary("r1", payload={"overview": "v1"}, kind="generated")
    await db.save_summary("r1", payload={"overview": "v2"}, kind="generated")
    rows = await db.get_summaries("r1")
    by_kind = {r["kind"]: r for r in rows}
    # Latest generated wins; imported original is untouched.
    assert by_kind["generated"]["payload"]["overview"] == "v2"
    assert by_kind["imported"]["payload"]["overview"] == "orig"


@pytest.mark.asyncio
async def test_summary_instructions_persist_and_round_trip(db):
    """The effective analyst-role instructions persist and round-trip so the
    regenerate UI can prefill the prompt that created a summary."""
    await _seed_completed_run(db)
    custom = "You are a snarky debate coach. Roast the weakest argument."
    saved = await db.save_summary(
        "r1", payload={"overview": "gen"}, kind="generated",
        tokens_in=10, tokens_out=5, cost_usd=0.003, instructions=custom,
    )
    assert saved["instructions"] == custom
    rows = await db.get_summaries("r1")
    by_kind = {r["kind"]: r for r in rows}
    assert by_kind["generated"]["instructions"] == custom


@pytest.mark.asyncio
async def test_summary_default_instructions_persist_as_null(db):
    """Omitting instructions (the default framing) persists NULL so the UI
    knows to fall back to default_instructions."""
    await _seed_completed_run(db)
    await db.save_summary("r1", payload={"overview": "gen"}, kind="generated")
    rows = await db.get_summaries("r1")
    by_kind = {r["kind"]: r for r in rows}
    assert by_kind["generated"]["instructions"] is None


@pytest.mark.asyncio
async def test_thread_lifecycle_and_messages(db):
    await _seed_completed_run(db)
    thread = await db.create_thread("t1", "r1", target="analyst")
    assert thread["mode"] == "aside"
    await db.add_thread_message("t1", role="user", speaker="user", content="q1")
    await db.add_thread_message("t1", role="target", speaker="analyst", content="a1",
                                tokens_in=100, tokens_out=20, cost_usd=0.005)
    msgs = await db.get_thread_messages("t1")
    assert [m["role"] for m in msgs] == ["user", "target"]
    assert await db.thread_cost("t1") == pytest.approx(0.005)

    threads = await db.list_threads("r1")
    assert threads[0]["message_count"] == 2
    assert threads[0]["total_cost_usd"] == pytest.approx(0.005)


@pytest.mark.asyncio
async def test_threads_are_isolated(db):
    await _seed_completed_run(db)
    await db.create_thread("t1", "r1", target="analyst")
    await db.create_thread("t2", "r1", target="persona", persona_name="Ada")
    await db.add_thread_message("t1", role="user", speaker="user", content="in t1")
    await db.add_thread_message("t2", role="user", speaker="user", content="in t2")
    t1_msgs = await db.get_thread_messages("t1")
    t2_msgs = await db.get_thread_messages("t2")
    assert [m["content"] for m in t1_msgs] == ["in t1"]
    assert [m["content"] for m in t2_msgs] == ["in t2"]


@pytest.mark.asyncio
async def test_resolve_model_skips_stale_imported_model():
    """Imported runs may record an EOL model; fresh analysis uses the current
    default instead of forwarding the stale (possibly dead) one."""
    from matrix_studio import service

    imported = {"config_json": '{"imported": true, "model": "bedrock/anthropic.claude-3-5-haiku-20241022-v1:0"}'}
    assert service.resolve_model(imported) is None  # -> current settings default

    native = {"config_json": '{"model": "bedrock/some-current-model"}'}
    assert service.resolve_model(native) == "bedrock/some-current-model"

    # An explicit override always wins.
    assert service.resolve_model(imported, override="bedrock/x") == "bedrock/x"

    # A BRANCH/resumed run inherits its parent's (possibly stale/EOL) config
    # model even though it generated with the current settings model. Fresh
    # analysis must decline the inherited model and use the current default.
    branch = {
        "parent_run_id": "parent-123",
        "config_json": '{"model": "bedrock/anthropic.claude-3-5-haiku-20241022-v1:0"}',
    }
    assert service.resolve_model(branch) is None  # -> current settings default
    # Override still wins for a branch.
    assert service.resolve_model(branch, override="bedrock/y") == "bedrock/y"
    # A root run with no parent still forwards its own configured model.
    assert service.resolve_model(native) == "bedrock/some-current-model"


@pytest.mark.asyncio
async def test_readonly_invariant_events_snapshot_cost_unchanged(db):
    """
    The whole point: summaries + aside threads/messages must NOT alter the
    canonical run. Diff events, snapshot, and run cost before vs after.
    """
    await _seed_completed_run(db)

    events_before = await db.get_events("r1")
    snap_before = (await db.get_snapshot("r1")).model_dump_json()
    stats_before = await db.get_run_stats("r1")

    # Perform a full spread of Phase 1.5 activity.
    await db.save_summary("r1", payload={"overview": "gen"}, kind="generated",
                          tokens_in=100, tokens_out=50, cost_usd=0.5)
    await db.save_summary("r1", payload={"overview": "orig"}, kind="imported")
    await db.create_thread("t1", "r1", target="analyst")
    await db.add_thread_message("t1", role="user", speaker="user", content="q")
    await db.add_thread_message("t1", role="target", speaker="analyst", content="a",
                                tokens_in=999, tokens_out=999, cost_usd=9.99)

    events_after = await db.get_events("r1")
    snap_after = (await db.get_snapshot("r1")).model_dump_json()
    stats_after = await db.get_run_stats("r1")

    # Canonical event log unchanged (count + rows identical).
    assert events_after == events_before
    # No aside/summary event types leaked into the canonical stream.
    assert not any(
        e["event_type"].startswith(("summary", "aside", "thread")) for e in events_after
    )
    # Snapshot byte-identical.
    assert snap_after == snap_before
    # Run's recorded cost unchanged despite large analysis costs.
    assert stats_after["total_cost_usd"] == pytest.approx(stats_before["total_cost_usd"])
    assert stats_after["turn_count"] == stats_before["turn_count"]
