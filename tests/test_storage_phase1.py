# SPDX-License-Identifier: Apache-2.0
"""Tests for the Phase 1 additive storage methods (name/description/slug,
list/search, get-by-ref, get-events-after, stats)."""

import json
import tempfile
from pathlib import Path

import pytest

from matrix_studio.storage import Database
from matrix_studio.tenancy import LOCAL_USER_SUB


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

    all_runs = await db.list_runs()
    assert len(all_runs) == 2

    hits = await db.list_runs(q="trusted")
    assert len(hits) == 1 and hits[0]["name"] == "trusted-robot"

    topic_hits = await db.list_runs(q="hiking")
    assert len(topic_hits) == 1 and topic_hits[0]["name"] == "summit-compass"

    assert await db.list_runs(q="nomatch") == []


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


# `test_phase0_migration_adds_columns` was removed here, deliberately rather than
# ported. It built a pre-Phase-1 SQLite `runs` table by hand and asserted that
# `connect()` added the missing `description`/`slug` columns. There is no SQLite
# schema to migrate any more — DynamoDB has no columns to add, and an item simply
# lacks an attribute — so the test could only have been rewritten into an assertion
# about `_row()` filling absent fields with None, which
# `test_absent_optional_fields_read_back_as_none` in test_dynamo_storage.py already
# makes, and makes better (it subscripts, as the real callers do).
#
# Reading a pre-migration `.db` file is now a job for `scripts/`, against
# `storage/database.py`, which is kept for exactly that.
