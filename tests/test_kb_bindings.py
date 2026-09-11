# SPDX-License-Identifier: Apache-2.0
"""Phase 6 step 4: bindings — which KBs a turn may search, and the 422 at creation.

Two levels, generalising Phase 5's `persona_name = ? OR persona_name IS NULL`:
run-level is the cast-wide case, persona-level is "hers alone". The effective scope is
their union, INTERSECTED with what the caller may read.

Validation happens twice on purpose. The 422 at creation is feedback; the query-time
re-check is the boundary, because a grant can be revoked after a run exists.
"""

import json

import pytest

from matrix_studio import bindings
from tests.support import TEST_OWNER

pytestmark = pytest.mark.asyncio

OTHER = "someone-else-sub"


@pytest.fixture
def client(db, monkeypatch):
    """A TestClient whose app shares the `db` fixture's mocked tables.

    Same `TABLE_PREFIX` (set by `aws_backend`), so the app's own `Database()` connects to
    the same moto state — which is what lets a test create a KB directly and then assert
    the route sees it.

    `TURN_LOOP_ARN` is set and `start_execution` stubbed, so `POST /api/runs` takes the
    Phase 5 path: write the row synchronously, start nothing, return. No engine and no
    LLM, and it exercises the code that actually ships rather than the local
    background-task path.
    """
    from fastapi.testclient import TestClient

    from matrix_studio import orchestration
    from matrix_studio.api.app import create_app

    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        return {"name": "bound-run", "description": "d", "slug": "bound-run",
                "source": "llm"}

    monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
    monkeypatch.setenv("TURN_LOOP_ARN", "arn:aws:states:us-east-1:1:stateMachine:sm")

    async def no_execution(*_a, **_k):
        return None

    monkeypatch.setattr(orchestration, "start_execution", no_execution)

    app = create_app()
    # Make the route's identity the SAME owner the `db` fixture is bound to.
    #
    # Without this the app resolves `local-single-user` while the fixture creates KBs
    # under TEST_OWNER, so every binding looks unreadable and the "accepted" tests fail
    # while the "rejected" ones pass for the wrong reason. That is the same fixture
    # mismatch the Phase 2 tenancy suite hit, and it is only visible because there is a
    # non-vacuity test asserting a GOOD binding is accepted.
    #
    # Overriding the dependency is the documented way — `identity.py` says tests override
    # it rather than forge a header, because a header production honoured would be an
    # authorisation bypass and one it ignored would mean the negative tests exercise a
    # path that does not ship.
    from matrix_studio.api.identity import current_groups, current_user

    app.dependency_overrides[current_user] = lambda: TEST_OWNER
    app.dependency_overrides[current_groups] = lambda: []
    with TestClient(app) as c:
        yield c


def _run(*, run_kbs=None, cast=None):
    """A run row shaped as storage returns one — config and cast as JSON text."""
    return {
        "id": "r1",
        "config_json": json.dumps({"knowledge_bases": run_kbs or []}),
        "cast_json": json.dumps(cast or []),
    }


# --------------------------------------------------------------------------- #
# bound_kbs — reading the two levels
# --------------------------------------------------------------------------- #


async def test_run_level_bindings_apply_to_every_persona():
    """The cast-wide case. §8b notes this is now CHEAP — stored once and bound, rather
    than copied per persona, which is what made cast-wide documents WILL NOT IMPLEMENT
    under the per-run model."""
    run = _run(run_kbs=["kb-shared"], cast=[{"name": "Ada"}, {"name": "Bo"}])
    assert bindings.bound_kbs(run, "Ada") == ["kb-shared"]
    assert bindings.bound_kbs(run, "Bo") == ["kb-shared"]


async def test_persona_bindings_are_private_to_that_persona():
    run = _run(cast=[
        {"name": "Ada", "knowledge_bases": ["kb-hers"]},
        {"name": "Bo", "knowledge_bases": ["kb-his"]},
    ])
    assert bindings.bound_kbs(run, "Ada") == ["kb-hers"]
    assert bindings.bound_kbs(run, "Bo") == ["kb-his"]


async def test_the_scope_is_the_union_of_both_levels():
    run = _run(
        run_kbs=["kb-shared"],
        cast=[{"name": "Ada", "knowledge_bases": ["kb-hers"]}],
    )
    assert bindings.bound_kbs(run, "Ada") == ["kb-shared", "kb-hers"]


async def test_a_kb_bound_at_both_levels_is_queried_once():
    """The fan-out would otherwise pay for it twice, and the merge would rank the same
    passage against itself."""
    run = _run(
        run_kbs=["kb-both"],
        cast=[{"name": "Ada", "knowledge_bases": ["kb-both", "kb-hers"]}],
    )
    assert bindings.bound_kbs(run, "Ada") == ["kb-both", "kb-hers"]


async def test_run_level_comes_first_and_the_order_is_stable():
    """Arbitrary but deterministic. A stable order is what makes a retrieval regression
    reproducible when the fan-out is trimmed to k."""
    run = _run(run_kbs=["a", "b"], cast=[{"name": "Ada", "knowledge_bases": ["c"]}])
    assert bindings.bound_kbs(run, "Ada") == ["a", "b", "c"]
    assert bindings.bound_kbs(run, "Ada") == bindings.bound_kbs(run, "Ada")


async def test_no_persona_returns_only_the_cast_wide_bindings():
    """The right answer for an operator listing: what EVERY persona can see, without
    attributing one persona's private collection to the whole cast."""
    run = _run(run_kbs=["kb-shared"], cast=[{"name": "Ada", "knowledge_bases": ["kb-hers"]}])
    assert bindings.bound_kbs(run, None) == ["kb-shared"]


async def test_an_unknown_persona_gets_the_cast_wide_bindings_only():
    run = _run(run_kbs=["kb-shared"], cast=[{"name": "Ada", "knowledge_bases": ["kb-hers"]}])
    assert bindings.bound_kbs(run, "Nobody") == ["kb-shared"]


async def test_a_run_with_no_bindings_binds_nothing():
    """Pre-Phase-6 runs have no `knowledge_bases` key at all and must keep working."""
    assert bindings.bound_kbs({"id": "r", "config_json": "{}", "cast_json": "[]"}) == []
    assert bindings.bound_kbs({"id": "r"}) == []
    assert bindings.bound_kbs({"id": "r", "config_json": "not json"}) == []


async def test_blank_and_non_string_bindings_are_ignored():
    """A trailing empty string from a form field must not become a KB id, which would
    query an index named `prefix-kb-` and report a failure every turn."""
    run = _run(run_kbs=["kb-real", "", "   ", None])
    assert bindings.bound_kbs(run, None) == ["kb-real"]


# --------------------------------------------------------------------------- #
# searchable_for_turn — the intersection, re-resolved per turn
# --------------------------------------------------------------------------- #


async def test_a_turn_searches_only_what_the_caller_may_read(db):
    mine = await db.create_knowledge_base("mine")
    theirs = await db.create_knowledge_base("theirs", owner_sub=OTHER)
    run = _run(run_kbs=[mine["id"], theirs["id"]], cast=[{"name": "Ada"}])
    got = await bindings.searchable_for_turn(db, run, "Ada", TEST_OWNER)
    assert got == [mine["id"]]


async def test_the_intersection_is_re_resolved_every_turn(db):
    """§8b's requirement. A cached list is exactly the stale binding it rules out."""
    shared = await db.create_knowledge_base("shared", owner_sub=OTHER)
    await db.grant_kb(shared["id"], user=TEST_OWNER)
    run = _run(run_kbs=[shared["id"]], cast=[{"name": "Ada"}])

    assert await bindings.searchable_for_turn(db, run, "Ada", TEST_OWNER) == [shared["id"]]
    await db.revoke_kb(shared["id"], user=TEST_OWNER)
    assert await bindings.searchable_for_turn(db, run, "Ada", TEST_OWNER) == [], (
        "the run's config is unchanged, so only a per-turn check can exclude it"
    )


async def test_a_group_grant_reaches_a_turn(db):
    shared = await db.create_knowledge_base("sre", owner_sub=OTHER)
    await db.grant_kb(shared["id"], group="sre")
    run = _run(run_kbs=[shared["id"]], cast=[{"name": "Ada"}])
    assert await bindings.searchable_for_turn(db, run, "Ada", TEST_OWNER) == []
    assert await bindings.searchable_for_turn(
        db, run, "Ada", TEST_OWNER, groups=["sre"]
    ) == [shared["id"]]


async def test_nothing_bound_means_no_grant_lookup_at_all(db, monkeypatch):
    """A retrieval-off or pre-Phase-6 run must not pay for an authorisation call."""
    calls = []
    monkeypatch.setattr(
        db, "searchable_kbs",
        lambda *a, **k: calls.append(a) or [],
    )
    assert await bindings.searchable_for_turn(db, _run(), "Ada", TEST_OWNER) == []
    assert calls == []


# --------------------------------------------------------------------------- #
# Validation at creation
# --------------------------------------------------------------------------- #


async def test_declared_kbs_covers_both_levels_in_one_pass():
    """Checking the run level while forgetting the cast is the shape of bug that lets a
    persona bind a collection nobody verified."""
    request = {
        "config": {"knowledge_bases": ["kb-run"]},
        "cast": [
            {"name": "Ada", "knowledge_bases": ["kb-ada"]},
            {"name": "Bo", "knowledge_bases": ["kb-bo", "kb-run"]},
        ],
    }
    assert bindings.declared_kbs(request) == ["kb-run", "kb-ada", "kb-bo"]


async def test_declared_kbs_of_a_request_with_no_bindings_is_empty():
    assert bindings.declared_kbs({"cast": [{"name": "Ada"}]}) == []
    assert bindings.declared_kbs({}) == []


async def test_unreadable_bindings_names_what_is_wrong(db):
    mine = await db.create_knowledge_base("mine")
    theirs = await db.create_knowledge_base("theirs", owner_sub=OTHER)
    bad = await bindings.unreadable_bindings(
        db, [mine["id"], theirs["id"], "no-such-kb"], TEST_OWNER
    )
    assert set(bad) == {theirs["id"], "no-such-kb"}


async def test_unreadable_bindings_fails_closed(db, monkeypatch):
    """A transient fault must refuse the run, not create one whose bindings were never
    checked — the same direction `searchable_kbs` fails in, for the same reason."""
    async def boom(*_a, **_k):
        raise RuntimeError("dynamodb is having a day")

    monkeypatch.setattr(db, "may_read_kb", boom)
    assert await bindings.unreadable_bindings(db, ["kb-x"], TEST_OWNER) == ["kb-x"]


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #


async def test_creating_a_run_bound_to_someone_elses_kb_is_a_422(db, client):
    theirs = await db.create_knowledge_base("theirs", owner_sub=OTHER)
    response = client.post("/api/runs", json={
        "topic": "t",
        "cast": [{"name": "Ada", "persona": "p"}],
        "config": {"knowledge_bases": [theirs["id"]], "max_messages": 1},
    })
    assert response.status_code == 422, response.text
    assert theirs["id"] in response.json()["detail"]


async def test_the_422_also_catches_a_persona_level_binding(db, client):
    theirs = await db.create_knowledge_base("theirs", owner_sub=OTHER)
    response = client.post("/api/runs", json={
        "topic": "t",
        "cast": [{"name": "Ada", "persona": "p", "knowledge_bases": [theirs["id"]]}],
        "config": {"max_messages": 1},
    })
    assert response.status_code == 422, response.text
    assert theirs["id"] in response.json()["detail"]


async def test_a_run_bound_to_a_readable_kb_is_accepted(db, client):
    """Non-vacuity: without this, a route that rejected every binding would pass above."""
    mine = await db.create_knowledge_base("mine")
    response = client.post("/api/runs", json={
        "topic": "t",
        "cast": [{"name": "Ada", "persona": "p", "knowledge_bases": [mine["id"]]}],
        "config": {"max_messages": 1},
    })
    assert response.status_code == 201, response.text


async def test_a_run_with_no_bindings_is_unaffected(db, client):
    """Every pre-Phase-6 client sends no `knowledge_bases` and must keep working."""
    response = client.post("/api/runs", json={
        "topic": "t",
        "cast": [{"name": "Ada", "persona": "p"}],
        "config": {"max_messages": 1},
    })
    assert response.status_code == 201, response.text


async def test_bindings_survive_into_the_stored_run(db, client):
    """Declared on the request model, because an undeclared field is silently DROPPED —
    the lesson `documents` and `structured` both carry."""
    mine = await db.create_knowledge_base("mine")
    response = client.post("/api/runs", json={
        "topic": "t",
        "cast": [{"name": "Ada", "persona": "p", "knowledge_bases": [mine["id"]]}],
        "config": {"knowledge_bases": [mine["id"]], "max_messages": 1},
    })
    assert response.status_code == 201, response.text
    run = await db.get_run(response.json()["run_id"])
    assert json.loads(run["config_json"])["knowledge_bases"] == [mine["id"]]
    assert json.loads(run["cast_json"])[0]["knowledge_bases"] == [mine["id"]]
