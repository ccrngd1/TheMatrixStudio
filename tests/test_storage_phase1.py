# SPDX-License-Identifier: Apache-2.0
"""Tests for the Phase 1 additive storage methods (name/description/slug,
list/search, get-by-ref, get-events-after, stats)."""

import json
import tempfile
from pathlib import Path

import pytest

from matrix_studio.storage import Database
from matrix_studio.tenancy import LOCAL_USER_SUB


@pytest.fixture
async def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()
    Path(db_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_create_run_with_name_description_slug(db):
    await db.create_run(
        run_id="r1", topic="AI ethics",
        cast=[{"name": "Ada", "persona": "p"}],
        name="trusted-robot", description="An ethics debate", slug="trusted-robot",
    )
    run = await db.get_run("r1")
    assert run["name"] == "trusted-robot"
    assert run["description"] == "An ethics debate"
    assert run["slug"] == "trusted-robot"


@pytest.mark.asyncio
async def test_name_exists(db):
    assert not await db.name_exists("trusted-robot", owner_sub="u1")
    await db.create_run(run_id="r1", topic="t", cast=[{"name": "A", "persona": "p"}],
                        name="trusted-robot", owner_sub="u1")
    assert await db.name_exists("trusted-robot", owner_sub="u1")
    # Uniqueness is PER USER: the same codename is still free for someone else.
    assert not await db.name_exists("trusted-robot", owner_sub="u2")


@pytest.mark.asyncio
async def test_get_run_by_ref_id_or_name(db):
    await db.create_run(run_id="r1", topic="t", cast=[{"name": "A", "persona": "p"}],
                        name="summit-compass", owner_sub="u1")
    by_id = await db.get_run_by_ref("r1", owner_sub="u1")
    by_name = await db.get_run_by_ref("summit-compass", owner_sub="u1")
    assert by_id["id"] == "r1"
    assert by_name["id"] == "r1"
    assert await db.get_run_by_ref("nope", owner_sub="u1") is None
    # Another user's ref resolves to nothing by BOTH forms. The name form matters
    # most: run names come from a small generated vocabulary and are guessable, so
    # a name lookup that ignored the owner would be trivially exploitable.
    assert await db.get_run_by_ref("r1", owner_sub="u2") is None
    assert await db.get_run_by_ref("summit-compass", owner_sub="u2") is None


@pytest.mark.asyncio
async def test_list_and_search_runs(db):
    await db.create_run(run_id="r1", topic="AI ethics", cast=[{"name": "A", "persona": "p"}],
                        name="trusted-robot", description="ethics")
    await db.create_run(run_id="r2", topic="hiking trip", cast=[{"name": "B", "persona": "p"}],
                        name="summit-compass", description="outdoors")

    all_runs = await db.list_runs(owner_sub=LOCAL_USER_SUB)
    assert len(all_runs) == 2

    hits = await db.list_runs(q="trusted", owner_sub=LOCAL_USER_SUB)
    assert len(hits) == 1 and hits[0]["name"] == "trusted-robot"

    topic_hits = await db.list_runs(q="hiking", owner_sub=LOCAL_USER_SUB)
    assert len(topic_hits) == 1 and topic_hits[0]["name"] == "summit-compass"

    assert await db.list_runs(q="nomatch", owner_sub=LOCAL_USER_SUB) == []


@pytest.mark.asyncio
async def test_get_events_after_and_stats(db):
    await db.create_run(run_id="r1", topic="t", cast=[{"name": "A", "persona": "p"}])
    await db.append_event(run_id="r1", turn=0, seq=0, event_type="sim.started",
                          payload={"topic": "t"})
    await db.append_event(run_id="r1", turn=1, seq=1, event_type="agent.response",
                          agent_name="A", payload={"cost_usd": 0.002, "message": "hi"})
    await db.append_event(run_id="r1", turn=2, seq=2, event_type="agent.response",
                          agent_name="A", payload={"cost_usd": 0.003, "message": "yo"})

    after = await db.get_events_after("r1", after_seq=0)
    assert [e["seq"] for e in after] == [1, 2]

    stats = await db.get_run_stats("r1")
    assert stats["turn_count"] == 2
    assert abs(stats["total_cost_usd"] - 0.005) < 1e-9


@pytest.mark.asyncio
async def test_phase0_migration_adds_columns(tmp_path):
    """A pre-Phase-1 runs table (no description/slug) is migrated additively."""
    import aiosqlite

    db_path = str(tmp_path / "legacy.db")
    # Simulate an old Phase 0 schema: runs table WITHOUT description/slug.
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("""
            CREATE TABLE runs (
                id TEXT PRIMARY KEY, name TEXT, topic TEXT NOT NULL,
                cast_json TEXT NOT NULL, config_json TEXT, status TEXT,
                parent_run_id TEXT, branch_turn INTEGER,
                created_at INTEGER NOT NULL, completed_at INTEGER
            )
        """)
        await conn.execute(
            "INSERT INTO runs (id, topic, cast_json, status, created_at) VALUES "
            "('old1', 'legacy topic', '[]', 'complete', 1)"
        )
        await conn.commit()

    # Connecting via our Database must add the missing columns without error.
    database = Database(db_path)
    await database.connect()
    try:
        run = await database.get_run("old1")
        assert run["topic"] == "legacy topic"
        assert run["description"] is None  # new column, backfilled null
        assert run["slug"] is None
        # And new named runs still work on the migrated DB.
        await database.create_run(run_id="new1", topic="t",
                                  cast=[{"name": "A", "persona": "p"}], name="new-name")
        assert await database.name_exists("new-name", owner_sub=LOCAL_USER_SUB)
        # The legacy row was back-filled to the local user, so it is still visible
        # rather than orphaned by the tenancy migration. Losing a pre-tenancy run
        # to a NULL owner would be indistinguishable from data loss.
        assert run["owner_sub"] == LOCAL_USER_SUB
    finally:
        await database.close()
