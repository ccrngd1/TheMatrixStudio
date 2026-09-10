# SPDX-License-Identifier: Apache-2.0
"""
Phase 0.2: every run belongs to one user, and no route serves another user's.

The whole point of this file is the *completeness* of the coverage, not the
cleverness of any one case. One missed route is the entire vulnerability: an
attacker does not need `GET /api/runs/{ref}` to leak the transcript if
`GET /api/runs/{ref}/events` will do it, and the event log is the source of
truth. So the central test is table-driven over every route that takes a run
``ref`` or a ``thread_id``, and it is paired with a guard that walks the app's
own route table and FAILS if a route exists that the table does not cover.

That guard is what makes this hold up over time. A hand-written list of negative
tests is correct on the day it is written and silently incomplete the moment
someone adds a route — and the new route would be exactly the one nobody thought
to test.

Two properties are asserted for every route, and the second one is what stops the
suite from passing vacuously:

  1. user B gets **404**, and
  2. user A gets **anything but 404**.

Without (2), a typo in a path, a wrong HTTP method, or a missing fixture would
produce a 404 for both users and the test would pass while proving nothing. That
failure mode has bitten this project before (a test named for vector mode that
only ever exercised hybrid), so it is guarded rather than trusted.

**404, not 403.** "Not yours" and "does not exist" are deliberately
indistinguishable. Run refs can be memorable names drawn from a small generated
vocabulary, so a 403 would be an oracle for "does a run called `trusted-robot`
exist under some other account" — answerable by guessing.
"""

import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from tests.support import TEST_OWNER
from fastapi.testclient import TestClient

from matrix_studio.api.app import create_app
from matrix_studio.api.identity import current_user, current_user_ws
from matrix_studio.state import AgentState, SimSnapshot
from matrix_studio.storage import Database
from matrix_studio.tenancy import LOCAL_USER_SUB


@pytest.fixture(autouse=True)
def _storage_backend(aws_backend):
    """Every test in this file builds the FastAPI app.

    The app's lifespan connects to DynamoDB, so without a mocked account it reaches
    real AWS — which surfaces as `ExpiredTokenException` on a `Scan` and reads like a
    credentials problem rather than a missing fixture. Autouse and explicit here
    rather than hidden in `conftest.py`, so the dependency is visible in the file that
    has it.
    """

USER_A = "sub-aaaa-1111"
USER_B = "sub-bbbb-2222"


# --------------------------------------------------------------------------- #
# Identity resolution
# --------------------------------------------------------------------------- #


def _scope_request(event: Optional[Dict[str, Any]]):
    """A stand-in connection carrying (or not carrying) an API Gateway event."""

    class _Conn:
        scope = {} if event is None else {"aws.event": event}

    return _Conn()


def _authorizer_event(claims: Dict[str, Any]) -> Dict[str, Any]:
    """An HTTP API (payload v2.0) event with JWT authorizer claims."""
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}


def test_claims_are_read_from_an_http_api_authorizer():
    from matrix_studio.api.identity import claims_from_scope

    claims = claims_from_scope(_scope_request(_authorizer_event({"sub": "s1"})))
    assert claims == {"sub": "s1"}


def test_claims_are_read_from_a_rest_api_authorizer():
    """REST APIs put claims one level higher than HTTP APIs.

    Accepting both shapes rather than picking one: guessing wrong would mean the
    claims are simply not found, and the fallback in single-user mode would then
    attribute every request to the local identity — an authentication bypass
    caused by a payload-version mismatch.
    """
    from matrix_studio.api.identity import claims_from_scope

    event = {"requestContext": {"authorizer": {"claims": {"sub": "s2"}}}}
    assert claims_from_scope(_scope_request(event)) == {"sub": "s2"}


def test_no_authorizer_means_no_claims():
    from matrix_studio.api.identity import claims_from_scope

    assert claims_from_scope(_scope_request(None)) is None
    assert claims_from_scope(_scope_request({"requestContext": {}})) is None


@pytest.mark.asyncio
async def test_single_user_mode_yields_the_local_identity():
    assert await current_user(_scope_request(None)) == LOCAL_USER_SUB


@pytest.mark.asyncio
async def test_jwt_mode_refuses_an_unauthenticated_request(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setenv("AUTH_MODE", "jwt")
    with pytest.raises(HTTPException) as exc:
        await current_user(_scope_request(None))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verified_claims_win_even_in_single_user_mode():
    """The setting decides the FALLBACK, never overrides a real identity.

    A deployment that grows an authorizer but keeps the default setting must start
    attributing runs correctly immediately. The alternative — honouring
    `single-user` first — would merge every user's history into one bucket while
    looking like it worked, which is the worst possible failure here because the
    data damage accumulates silently.
    """
    sub = await current_user(_scope_request(_authorizer_event({"sub": "real-sub"})))
    assert sub == "real-sub"


@pytest.mark.asyncio
async def test_an_authorizer_with_no_subject_is_a_401_not_a_fallback():
    """A broken authorizer must not be treated as an anonymous user.

    Falling back to the local identity here would grant access *because* the auth
    configuration was broken.
    """
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await current_user(_scope_request(_authorizer_event({"sub": "  "})))
    assert exc.value.status_code == 401


def test_an_unrecognised_auth_mode_is_refused_at_startup(monkeypatch):
    """`AUTH_MODE=JWT` must not land on the permissive branch.

    A capitalisation typo would otherwise be a whole-system authorisation bypass
    that nothing reports.
    """
    from matrix_studio.settings import Settings

    monkeypatch.setenv("AUTH_MODE", "JWT")
    with pytest.raises(ValueError, match="auth_mode must be one of"):
        Settings()


def test_the_websocket_dependency_resolves_the_same_identity():
    """The stream carries the transcript, so it cannot be the one unscoped route."""
    import asyncio

    assert asyncio.run(current_user_ws(_scope_request(None))) == LOCAL_USER_SUB


# --------------------------------------------------------------------------- #
# Storage scoping
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_two_users_can_hold_the_same_run_name(db):
    """The per-user unique index, end to end.

    With the old global index the second insert raises IntegrityError — and the
    collision would be with a row the second user cannot see, so the error would
    be unexplainable from their side as well as wrong.
    """
    await db.create_run(run_id="a1", topic="t", cast=[], name="trusted-robot",
                        owner_sub=USER_A)
    await db.create_run(run_id="b1", topic="t", cast=[], name="trusted-robot",
                        owner_sub=USER_B)

    assert (await db.get_run_by_ref("trusted-robot", owner_sub=USER_A))["id"] == "a1"
    assert (await db.get_run_by_ref("trusted-robot", owner_sub=USER_B))["id"] == "b1"


@pytest.mark.asyncio
async def test_one_user_still_cannot_reuse_their_own_run_name(db):
    """Per-user uniqueness must still be uniqueness, or refs stop resolving.

    `get_run_by_ref` accepts a name, so two runs sharing one under the same owner
    would make that ref ambiguous — whichever the marker item happened to point at
    would win, and the other run would become unreachable by name.

    The exception type changed with the backend: SQLite raised
    `aiosqlite.IntegrityError` from a unique index, and DynamoDB has no unique
    constraint beyond the primary key, so uniqueness is a conditional `NAME#{name}`
    marker written in the same transaction as the run. `DuplicateNameError` exists
    precisely so callers can still detect this — `manager.create_run` and
    `branching.create_branch_run` both recover from it by appending a suffix rather
    than failing.
    """
    from matrix_studio.storage import DuplicateNameError

    await db.create_run(run_id="a1", topic="t", cast=[], name="trusted-robot",
                        owner_sub=USER_A)
    with pytest.raises(DuplicateNameError, match="named 'trusted-robot'"):
        await db.create_run(run_id="a2", topic="t", cast=[], name="trusted-robot",
                            owner_sub=USER_A)


# Two tests were removed here, both of which asserted a SQLite MECHANISM rather than
# a property, and neither of which has an analogue on DynamoDB.
#
# `test_the_old_global_name_index_is_dropped` read `sqlite_master` to prove the
# migration DROPped the old global unique index — because `CREATE INDEX IF NOT EXISTS`
# is satisfied by adding the per-user one, so a surviving global index would defeat it
# silently. There is no index now: per-user uniqueness is a `NAME#{name}` marker item
# under `USER#{sub}`, and there is no global equivalent that could survive. The
# PROPERTY it protected is still asserted, by
# `test_two_users_can_hold_the_same_run_name` above and
# `test_one_user_still_cannot_reuse_their_own_run_name` below.
#
# `test_a_pre_tenancy_row_is_adopted_by_the_local_user` built a pre-tenancy SQLite
# `runs` table by hand and asserted that `connect()` back-filled `owner_sub` to the
# local user, so an existing install did not lose its history to a NULL owner. There is
# no connect-time migration to test: DynamoDB tables start empty, and an existing
# SQLite file is not read by the application at all.
#
# **That leaves a real gap, named here rather than quietly dropped:** the 38 runs in an
# existing `data/matrix_studio.db` are no longer reachable through the app. Moving them
# is a migration script — read with the retained `storage/database.py`, write with
# `DynamoStorage` — and it does not exist yet. It is a product decision whether those
# runs are worth carrying; this comment is here so the decision is deliberate rather
# than discovered.


@pytest.mark.asyncio
async def test_list_runs_search_does_not_leak_across_owners(db):
    """The `q` filter is where an AND/OR precedence slip would leak everything.

    `WHERE owner_sub = ? AND a LIKE ? OR b LIKE ? OR c LIKE ?` parses as
    `(owner AND a) OR b OR c` — so a topic match alone would satisfy it for every
    tenant. This asserts the parenthesisation rather than assuming it.
    """
    await db.create_run(run_id="a1", topic="shared topic", cast=[],
                        name="a-run", description="alpha", owner_sub=USER_A)
    await db.create_run(run_id="b1", topic="shared topic", cast=[],
                        name="b-run", description="beta", owner_sub=USER_B)

    for q in (None, "shared", "run", "a-run"):
        rows = await db.list_runs(q=q, owner_sub=USER_A)
        assert {r["id"] for r in rows} <= {"a1"}, f"leak for q={q!r}"


@pytest.mark.asyncio
async def test_lineage_reads_stay_inside_the_tenant(db):
    """A branch inherits its parent's owner, so the tree walk must too.

    In a correct system this filter is unreachable — which is why it is worth a
    test. The tree query is the only one that starts at an authorised row and
    recurses to rows nobody checked, so it should not depend on an invariant
    maintained in `branching.py`. Here the invariant is deliberately violated to
    prove the query does not rely on it.
    """
    await db.create_run(run_id="root", topic="t", cast=[], name="root-run",
                        owner_sub=USER_A)
    # A child that should never exist: another tenant's run claiming A's parent.
    await db.create_run(run_id="stolen", topic="t", cast=[], name="stolen-run",
                        parent_run_id="root", branch_turn=1, owner_sub=USER_B)

    branches = await db.list_branches("root", owner_sub=USER_A)
    assert branches == []
    tree = await db.get_run_tree("root", owner_sub=USER_A)
    assert set(tree["nodes"]) == {"root"}


# --------------------------------------------------------------------------- #
# Route authorisation — the table, and the guard that keeps it complete
# --------------------------------------------------------------------------- #


@pytest.fixture
def app_and_db(tmp_path, monkeypatch):
    """An app over a DB seeded with one fully-populated run owned by USER_A.

    "Fully populated" matters: every route must return something other than 404
    for the owner, otherwise the negative assertions could pass for the wrong
    reason.
    """
    import asyncio

    # The avatar route reads a blob, and blobs live under DATA_DIR.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db_path = str(tmp_path / "tenancy.db")
    app = create_app(db_path)

    async def seed() -> str:
        # Bound to USER_A, not to the suite's default owner.
        #
        # The run row alone is not enough: its EVENTS, SNAPSHOTS and DOCUMENTS live in
        # the owner's partition too. Creating the run with an explicit
        # `owner_sub=USER_A` while the store stayed bound elsewhere put the run in one
        # partition and everything about it in another — so six routes 404'd for their
        # own owner, and the negative assertions below would have passed for entirely
        # the wrong reason. The non-vacuity check is what caught it.
        database = Database().for_owner(USER_A)
        await database.connect()
        try:
            await database.create_run(
                run_id="run-a", topic="a topic",
                cast=[{"name": "Dana", "persona": "p", "goals": []}],
                name="alpha-run", description="A's run",
                config={"cognition": {"enabled": True}},
            )
            await database.append_event(
                run_id="run-a", turn=0, seq=0, event_type="sim.started",
                payload={"topic": "a topic", "agent_count": 1},
            )
            await database.append_event(
                run_id="run-a", turn=1, seq=1, event_type="speaker.selected",
                agent_name="Dana", payload={"speaker": "Dana"},
            )
            await database.append_event(
                run_id="run-a", turn=1, seq=2, event_type="agent.response",
                agent_name="Dana",
                payload={
                    "speaker": "Dana", "message": "A point about egress.",
                    "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.001,
                    # Present so the /trace route has a rationale to return.
                    "rationale": "because it follows",
                },
            )
            await database.append_event(
                run_id="run-a", turn=1, seq=3, event_type="sim.completed",
                payload={"total_turns": 1, "total_cost_usd": 0.001},
            )
            # A real stored avatar, so the avatar route has something to serve.
            # Without it that route 404s for the owner too and its negative case
            # would prove nothing.
            from matrix_studio import blobs

            key = blobs.put(b"\x89PNG\r\n\x1a\nfake", namespace="avatars",
                            suffix="png")
            agent = AgentState(name="Dana", persona="p", goals=[],
                               portrait_key=key)
            snapshot = SimSnapshot(
                run_id="run-a", turn=1, topic="a topic", agents={"Dana": agent},
                conversation=[{"speaker": "Dana", "content": "A point about "
                               "egress.", "turn": 1}],
                status="complete", created_at=1, completed_at=1, total_turns=1,
            )
            # One call: the turn comes off the snapshot, so this IS the
            # turn-1 checkpoint as well as the completion snapshot.
            await database.save_snapshot(snapshot)
            await database.update_run_status("run-a", "complete", 1)
            doc_id = await database.add_document(
                run_id="run-a", title="bg.md",
                chunks=["Egress inspection provides auditable evidence."],
                persona_name="Dana",
            )
            await database.create_thread(
                thread_id="thread-a", run_id="run-a", target="analyst",
            )
            return doc_id, "thread-a"
        finally:
            await database.close()

    doc_id, thread_id = asyncio.run(seed())
    return app, doc_id, thread_id


def _routes(app) -> List[Tuple[str, str]]:
    """Every (method, path) in the app that is addressed by a run or thread id."""
    out = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if "{ref}" not in path and "{thread_id}" not in path:
            continue
        for method in sorted(getattr(route, "methods", None) or {"WEBSOCKET"}):
            out.append((method, path))
    return out


def _cases(doc_id: str, thread_id: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Path substitutions and request bodies, keyed by (method, path template).

    Keyed by the template so the completeness guard below can diff this against
    the app's real route table — a route with no entry here is a test failure, not
    an omission that goes unnoticed.
    """
    def case(url, **kw):
        return {"url": url, **kw}

    return {
        ("GET", "/api/runs/{ref}"): case("/api/runs/alpha-run"),
        ("GET", "/api/runs/{ref}/setup"): case("/api/runs/alpha-run/setup"),
        ("GET", "/api/runs/{ref}/events"): case("/api/runs/alpha-run/events"),
        ("GET", "/api/runs/{ref}/snapshots"): case("/api/runs/alpha-run/snapshots"),
        ("GET", "/api/runs/{ref}/snapshots/{turn}"):
            case("/api/runs/alpha-run/snapshots/1"),
        ("GET", "/api/runs/{ref}/agents/{name}/dossier"):
            case("/api/runs/alpha-run/agents/Dana/dossier"),
        ("POST", "/api/runs/{ref}/agents/{name}/regenerate-avatar"):
            case("/api/runs/alpha-run/agents/Dana/regenerate-avatar"),
        ("GET", "/api/runs/{ref}/agents/{name}/avatar"):
            case("/api/runs/alpha-run/agents/Dana/avatar"),
        ("GET", "/api/runs/{ref}/turns/{turn}/structured"):
            case("/api/runs/alpha-run/turns/1/structured?opt_in=true"),
        ("GET", "/api/runs/{ref}/pending-threads"):
            case("/api/runs/alpha-run/pending-threads"),
        ("GET", "/api/runs/{ref}/turns/{turn}/trace"):
            case("/api/runs/alpha-run/turns/1/trace"),
        ("POST", "/api/runs/{ref}/branch"):
            case("/api/runs/alpha-run/branch", json={"from_turn": 1}),
        ("GET", "/api/runs/{ref}/tree"): case("/api/runs/alpha-run/tree"),
        ("POST", "/api/runs/{ref}/resume"):
            case("/api/runs/alpha-run/resume", json={}),
        ("POST", "/api/runs/{ref}/stop"): case("/api/runs/alpha-run/stop"),
        ("GET", "/api/runs/{ref}/summary"): case("/api/runs/alpha-run/summary"),
        ("POST", "/api/runs/{ref}/summary"):
            case("/api/runs/alpha-run/summary", json={}),
        ("GET", "/api/runs/{ref}/threads"): case("/api/runs/alpha-run/threads"),
        ("POST", "/api/runs/{ref}/threads"):
            case("/api/runs/alpha-run/threads", json={"target": "analyst"}),
        ("GET", "/api/threads/{thread_id}"): case(f"/api/threads/{thread_id}"),
        ("POST", "/api/threads/{thread_id}/messages"):
            case(f"/api/threads/{thread_id}/messages", json={"content": "hi"}),
        ("GET", "/api/runs/{ref}/documents"): case("/api/runs/alpha-run/documents"),
        ("POST", "/api/runs/{ref}/documents"):
            case("/api/runs/alpha-run/documents",
                 json={"title": "t.md", "text": "some background text"}),
        ("DELETE", "/api/runs/{ref}/documents/{document_id}"):
            case(f"/api/runs/alpha-run/documents/{doc_id}"),
        ("POST", "/api/runs/{ref}/documents/reindex"):
            case("/api/runs/alpha-run/documents/reindex"),
        ("POST", "/api/runs/{ref}/documents/embed"):
            case("/api/runs/alpha-run/documents/embed"),
        ("GET", "/api/runs/{ref}/documents/search"):
            case("/api/runs/alpha-run/documents/search?q=egress"),
        ("WEBSOCKET", "/api/runs/{ref}/stream"):
            case("/api/runs/alpha-run/stream"),
    }


def test_every_ref_route_has_a_cross_tenant_case(app_and_db):
    """The guard that keeps the table below honest as routes are added.

    A hand-maintained list of negative tests is complete on the day it is written
    and silently incomplete forever after. This fails the build when a new
    ``{ref}`` route appears without a case — and the new route is precisely the one
    nobody would have thought to add.
    """
    app, doc_id, thread_id = app_and_db
    declared = set(_cases(doc_id, thread_id))
    actual = set(_routes(app))
    assert actual - declared == set(), (
        "routes with no cross-tenant test: "
        f"{sorted(actual - declared)}"
    )
    assert declared - actual == set(), (
        f"cases for routes that no longer exist: {sorted(declared - actual)}"
    )


def _client(app, sub: str) -> TestClient:
    """A client that authenticates as ``sub``.

    Overriding the dependency rather than sending a header: production honours no
    "act as this user" header, and testing through one would exercise a path that
    does not ship.
    """
    app.dependency_overrides[current_user] = lambda: sub
    app.dependency_overrides[current_user_ws] = lambda: sub
    return TestClient(app)


@pytest.mark.parametrize("method,path", _routes(create_app(":memory:")))
def test_user_b_cannot_reach_user_as_run(app_and_db, method, path, monkeypatch):
    """One negative case per route, plus the non-vacuity check.

    Asserting A gets a non-404 on the same URL is what stops this from passing for
    the wrong reason: a path typo, a wrong method, or an unseeded fixture would
    404 for both users and look like a pass.
    """
    app, doc_id, thread_id = app_and_db
    case = _cases(doc_id, thread_id)[(method, path)]
    url, body = case["url"], case.get("json")

    # Avatar regeneration would otherwise reach an image model. Stubbed to
    # "unavailable", which the route reports as 502 — still not a 404, so the
    # non-vacuity assertion below stays meaningful.
    async def _no_avatar(*_a, **_k):
        return None

    monkeypatch.setattr("matrix_studio.avatar.generate_avatar", _no_avatar)

    def call(client, u):
        if method == "WEBSOCKET":
            # A WS route cannot answer 404; it accepts and then sends an error
            # frame. Treat that frame as the equivalent so one table covers both.
            with client.websocket_connect(u) as ws:
                first = ws.receive_json()
            return 404 if first.get("event_type") == "error" else 200
        return client.request(method, u, json=body).status_code

    with _client(app, USER_B) as client_b:
        assert call(client_b, url) == 404, f"{method} {path} leaked to another user"

    with _client(app, USER_A) as client_a:
        status = call(client_a, url)
    assert status != 404, (
        f"{method} {path} 404s for its OWNER too, so the negative case above "
        "proves nothing — fix the fixture or the case, do not delete the test"
    )


def test_the_uuid_form_of_a_ref_is_scoped_too(app_and_db):
    """Both ref forms, because only one of them is guessable — and both must fail.

    The table above uses the memorable name. The id path is a separate SQL branch
    (`id = ? OR name = ?`), so it needs its own assertion.
    """
    app, _doc, _thread = app_and_db
    with _client(app, USER_B) as client:
        assert client.get("/api/runs/run-a").status_code == 404
    with _client(app, USER_A) as client:
        assert client.get("/api/runs/run-a").status_code == 200


def test_the_history_list_shows_only_the_callers_runs(app_and_db):
    app, _doc, _thread = app_and_db
    with _client(app, USER_B) as client:
        assert client.get("/api/runs").json()["runs"] == []
    with _client(app, USER_A) as client:
        runs = client.get("/api/runs").json()["runs"]
    assert [r["name"] for r in runs] == ["alpha-run"]


def test_a_created_run_belongs_to_its_creator(app_and_db):
    """The one place ownership is established, checked through the real route.

    Asserted via visibility rather than by reading the column, because visibility
    is the property that matters and it exercises the whole path.
    """
    import time

    from tests.test_api import make_fake_run

    app, _doc, _thread = app_and_db
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "matrix_studio.api.manager.run_simulation", make_fake_run(turns=1)
        )
        with _client(app, USER_B) as client:
            created = client.post(
                "/api/runs",
                json={"topic": "B's topic",
                      "cast": [{"name": "Bee", "persona": "p"}]},
            )
            assert created.status_code == 201
            ref = created.json()["run_id"]

            # The row is written by the background task, so the POST returning is
            # not proof it exists yet. Poll rather than sleep a fixed amount.
            deadline = time.time() + 5
            while time.time() < deadline:
                if client.get(f"/api/runs/{ref}").status_code == 200:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("the run never became visible to its creator")

        with _client(app, USER_A) as client:
            assert client.get(f"/api/runs/{ref}").status_code == 404
