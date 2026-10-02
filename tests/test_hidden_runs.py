# SPDX-License-Identifier: Apache-2.0
"""Hiding a run from the Runs list, and showing it again.

The owner asked to hide individual conversations from the main page and to find them again later. The flag
lives on the run row so it holds on every device; what these tests pin is everything it must NOT do:

  * create a run. `UpdateItem` upserts, so an unconditional write to a missing or foreign run would put a
    phantom conversation in somebody's history — hiding a run that is not there would create one;
  * reach another owner's run. Same 404 as every other run route;
  * change anything else. Hiding is not deletion: the transcript, summary, stance, research record and cost
    are all where they were, and un-hiding returns the row to exactly what it was;
  * drop the run from the list the API serves. The client filters hidden runs itself, and needs them there
    to count them and to offer them back.

Every name and topic here is invented: the repository is public.
"""

import asyncio
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from matrix_studio.api.app import _run_summary, create_app
from tests.support import TEST_OWNER
from tests.test_api import REQUEST, _wait_complete, make_fake_run

CAST = [{"name": "Ada", "persona": "p", "goals": []}, {"name": "Bo", "persona": "p", "goals": []}]


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


async def _raw(db, run_id, owner=TEST_OWNER):
    """The stored item itself, not `get_run`'s view of it, which restores absent fields as None."""
    from matrix_studio.storage.dynamo import _run_sk, _user_pk

    got = await db._call(db._table("runs").get_item, Key={"pk": _user_pk(owner), "sk": _run_sk(run_id)})
    return got.get("Item")


async def test_hiding_sets_the_flag_and_showing_removes_it(db):
    await db.create_run(run_id="h1", topic="t", cast=CAST, name="amber-lantern", owner_sub=TEST_OWNER)
    original = await _raw(db, "h1")
    assert (await db.get_run("h1"))["hidden"] is None, "absent reads as None, like every optional field"

    assert await db.set_run_hidden("h1", True) is True
    assert (await db.get_run("h1"))["hidden"] is True
    assert {k: v for k, v in (await _raw(db, "h1")).items() if k != "hidden"} == original

    assert await db.set_run_hidden("h1", False) is True
    assert (await db.get_run("h1"))["hidden"] is None
    # REMOVE, not `hidden = false`: a run hidden and shown again is the row it was.
    assert await _raw(db, "h1") == original


async def test_setting_the_same_state_twice_is_a_no_op(db):
    await db.create_run(run_id="h1", topic="t", cast=CAST, owner_sub=TEST_OWNER)
    assert await db.set_run_hidden("h1", True) and await db.set_run_hidden("h1", True)
    assert (await db.get_run("h1"))["hidden"] is True
    assert await db.set_run_hidden("h1", False) and await db.set_run_hidden("h1", False)
    assert (await db.get_run("h1"))["hidden"] is None


async def test_hiding_a_missing_run_is_refused_not_invented(db):
    for hidden in (True, False):
        assert await db.set_run_hidden("ghost", hidden) is False
    assert await db.get_run("ghost") is None
    assert await _raw(db, "ghost") is None
    assert await db.list_runs() == []


async def test_hiding_cannot_reach_another_owners_run(db):
    await db.create_run(run_id="h1", topic="t", cast=CAST, name="amber-lantern", owner_sub=TEST_OWNER)
    assert await db.set_run_hidden("h1", True, owner_sub="sub-someone-else") is False
    # Nothing in the other partition, and the owner's run untouched.
    assert await db.list_runs(owner_sub="sub-someone-else") == []
    assert await _raw(db, "h1", owner="sub-someone-else") is None
    assert (await db.get_run("h1"))["hidden"] is None


async def test_the_list_still_returns_hidden_runs(db):
    await db.create_run(run_id="h1", topic="harbour tolls", cast=CAST, name="amber-lantern", owner_sub=TEST_OWNER)
    await db.create_run(run_id="h2", topic="ferry fares", cast=CAST, name="birch-signal", owner_sub=TEST_OWNER)
    await db.set_run_hidden("h1", True)
    listed = {r["id"]: r["hidden"] for r in await db.list_runs()}
    assert listed == {"h1": True, "h2": None}
    # The search the client falls back on past the list's cap finds hidden runs too; it filters them itself.
    assert [r["id"] for r in await db.list_runs(q="harbour")] == ["h1"]


def test_the_summary_says_hidden_as_a_boolean():
    row = {"id": "r1", "topic": "t"}
    assert _run_summary(row)["hidden"] is False
    assert _run_summary({**row, "hidden": None})["hidden"] is False
    assert _run_summary({**row, "hidden": True})["hidden"] is True


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(aws_backend, tmp_path, monkeypatch):
    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        base, name, n = "copper-kite", "copper-kite", 2
        while name_exists is not None and await name_exists(name):
            name, n = f"{base}-{n}", n + 1
        return {"name": name, "description": "A test simulation", "slug": name, "source": "llm"}

    monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.api.app.generate_run_name", fake_name)
    with TestClient(create_app(db_path=str(tmp_path / "test.db"))) as c:
        yield c


def _start(client, **over):
    """A finished run, with no auto-summary racing the reads below."""
    body = {**REQUEST, "summary": {"enabled": False}, **over}
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=2)):
        run_id = client.post("/api/runs", json=body).json()["run_id"]
        _wait_complete(client, run_id)
    return run_id


def _with_store(fn):
    """Run `fn(store)` against the app's storage, bound to the identity the app gives every request.

    `LOCAL_USER_SUB`, not `TEST_OWNER`: with no AUTH_MODE the app resolves every request to the local single
    user, so that is the partition the runs above were written to.
    """
    from matrix_studio.storage import Database
    from matrix_studio.tenancy import LOCAL_USER_SUB

    async def go():
        store = Database()
        await store.connect()
        try:
            return await fn(store.for_owner(LOCAL_USER_SUB))
        finally:
            await store.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


def _listed(client, run_id, **params):
    return next((r for r in client.get("/api/runs", params=params).json()["runs"] if r["run_id"] == run_id), None)


def test_a_run_is_hidden_and_shown_through_the_route(client):
    run_id = _start(client)
    assert client.get(f"/api/runs/{run_id}").json()["hidden"] is False
    assert _listed(client, run_id)["hidden"] is False

    r = client.post(f"/api/runs/{run_id}/hidden", json={"hidden": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"run_id": run_id, "hidden": True}
    assert client.get(f"/api/runs/{run_id}").json()["hidden"] is True
    # Still in the list: the client leaves it out, and needs it there to count it and offer it back.
    assert _listed(client, run_id)["hidden"] is True
    assert _listed(client, run_id, q="copper")["hidden"] is True, "the server-side search keeps it too"

    r = client.post(f"/api/runs/{run_id}/hidden", json={"hidden": False})
    assert r.status_code == 200, r.text
    assert r.json() == {"run_id": run_id, "hidden": False}
    assert client.get(f"/api/runs/{run_id}").json()["hidden"] is False
    assert _listed(client, run_id)["hidden"] is False


def test_a_run_can_be_named_by_its_codename(client):
    run_id = _start(client)
    r = client.post("/api/runs/copper-kite/hidden", json={"hidden": True})
    assert r.status_code == 200 and r.json()["run_id"] == run_id, "the answer names the run by its id"
    assert client.get(f"/api/runs/{run_id}").json()["hidden"] is True


def test_the_state_must_be_said(client):
    run_id = _start(client)
    for body in ({}, {"hidden": None}, {"hidden": "maybe"}):
        assert client.post(f"/api/runs/{run_id}/hidden", json=body).status_code == 422, body
    assert client.get(f"/api/runs/{run_id}").json()["hidden"] is False


def test_another_owner_cannot_hide_a_run(client):
    from matrix_studio.api import identity

    run_id = _start(client)
    client.app.dependency_overrides[identity.current_user] = lambda: "sub-someone-else"
    try:
        for ref in (run_id, "copper-kite"):
            assert client.post(f"/api/runs/{ref}/hidden", json={"hidden": True}).status_code == 404
        assert client.get("/api/runs").json()["runs"] == [], "and no phantom row in their own list"
    finally:
        client.app.dependency_overrides.pop(identity.current_user, None)
    assert client.get(f"/api/runs/{run_id}").json()["hidden"] is False


def test_hiding_a_missing_run_is_a_404_and_creates_nothing(client):
    for hidden in (True, False):
        assert client.post("/api/runs/no-such-run/hidden", json={"hidden": hidden}).status_code == 404
    assert client.get("/api/runs").json()["runs"] == []
    assert _with_store(lambda s: s.get_run("no-such-run")) is None


def test_a_row_that_goes_between_the_read_and_the_write_is_a_404(client):
    """The route reads the run, then writes: a run deleted in between must not be recreated as a phantom."""
    run_id = _start(client)

    async def gone(*_a, **_k):
        return False

    with patch("matrix_studio.storage.dynamo.DynamoStorage.set_run_hidden", gone):
        assert client.post(f"/api/runs/{run_id}/hidden", json={"hidden": True}).status_code == 404


def test_hiding_changes_nothing_else_about_the_run(client):
    run_id = _start(client)

    async def annotate(store):
        await store.set_run_stance(run_id, {"Ada": "support", "Bo": "holding"})
        await store.set_run_research(run_id, {"status": "found-nothing", "provider": "none", "cost_usd": 0.25})
        await store.save_summary(run_id, {"overview": "Two people agreed to disagree."}, cost_usd=0.01)

    _with_store(annotate)

    def everything():
        detail = client.get(f"/api/runs/{run_id}").json()
        events = client.get(f"/api/runs/{run_id}/events").json()["events"]
        row = _with_store(lambda s: s.get_run(run_id))
        return detail, events, row

    detail, events, row = everything()
    assert detail["stance"] and detail["research"] and detail["summary"]["generated"], "the fixture annotated it"
    assert detail["result"]["conversation"] and detail["cost"]["total"] > 0

    client.post(f"/api/runs/{run_id}/hidden", json={"hidden": True})
    hidden_detail, hidden_events, hidden_row = everything()
    assert {**hidden_detail, "hidden": False} == detail
    assert hidden_events == events
    assert {**hidden_row, "hidden": None} == row

    client.post(f"/api/runs/{run_id}/hidden", json={"hidden": False})
    assert everything() == (detail, events, row)


def test_a_hidden_member_still_appears_in_its_ensemble(client):
    from tests.test_api_ensembles import _create

    eid = _create(client).json()["ensemble_id"]
    members = client.get(f"/api/ensembles/{eid}").json()["members"]
    target = members[0]["run_id"]
    assert client.post(f"/api/runs/{target}/hidden", json={"hidden": True}).status_code == 200

    after = client.get(f"/api/ensembles/{eid}").json()["members"]
    assert [m["run_id"] for m in after] == [m["run_id"] for m in members]
    assert next(m for m in after if m["run_id"] == target)["run"]["hidden"] is True
    assert client.get(f"/api/runs/{target}").json()["ensemble_id"] == eid, "membership is untouched"
