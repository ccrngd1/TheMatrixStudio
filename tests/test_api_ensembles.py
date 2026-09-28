# SPDX-License-Identifier: Apache-2.0
"""
Fanning one brief out into N runs: the parent row, the members, and what the route refuses.

Two things here are load-bearing rather than incidental, and both come from
docs/ENSEMBLE-CONVERSATIONS.md:

**The parent is written before any member.** §8 — an interrupted fan-out must leave a parent
that knows it is short, not a set of orphan runs that each look like a conversation somebody
started by hand. Driven by `TestAPartialFanOut`, at the manager level: on the local path a
member's *creation* cannot fail through HTTP (the engine runs in a background task, so a
raising engine is logged rather than returned), and forcing it through the route would test
the fake instead of the fan-out.

**Cells are never flattened.** §4 — each member carries its cell label on the row, because in
the default replicates-only ensemble every member's config is identical and the label cannot
be recovered from the config. `report_ready` is the other half: a report generated while a
cell is still running would count a conclusion as absent from a run that had not reached it
yet, which is the censoring of §3.4.

The engine is faked throughout, as everywhere else in the API tests: the real environment
carries a Bedrock key and a real fan-out would make five billable runs.
"""

import asyncio
import json
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from matrix_studio.api.app import create_app
from tests.test_api import make_fake_run, REQUEST


@pytest.fixture(autouse=True)
def _storage_backend(aws_backend):
    """The app's lifespan connects to DynamoDB; without this it reaches real AWS."""


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        base = "trusted-robot"
        name = base
        n = 2
        while name_exists is not None and await name_exists(name):
            name = f"{base}-{n}"
            n += 1
        return {"name": name, "description": "A test simulation", "slug": name,
                "source": "llm"}

    monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.api.app.generate_run_name", fake_name)

    app = create_app(db_path=str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def _body(**overrides):
    body = {
        "topic": REQUEST["topic"],
        "cast": REQUEST["cast"],
        "name": "renewal",
        "config": {"max_messages": 4},
    }
    body.update(overrides)
    return body


def _create(client, turns=2, **overrides):
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=turns)):
        res = client.post("/api/ensembles", json=_body(**overrides))
        if res.status_code == 201:
            _settle(client, res.json()["ensemble_id"])
    return res


def _settle(client, ensemble_id, tries=200):
    """Wait until every member has reached a terminal status.

    Members run as background asyncio tasks on the local path, so a status read straight
    after the POST catches them mid-flight. Polling `report_ready` rather than sleeping a
    fixed time, since that is the same predicate the aggregator will gate on.
    """
    for _ in range(tries):
        out = client.get(f"/api/ensembles/{ensemble_id}").json()
        if out.get("report_ready"):
            return out
        time.sleep(0.02)
    return client.get(f"/api/ensembles/{ensemble_id}").json()


def _wait_report(client, ensemble_id, tries=300):
    """Wait for the stored report, which lands strictly AFTER settlement.

    `report_ready` only says the aggregator MAY run. The last member flips its own status and
    then generates, in that order, so there is a window where every member is settled and no
    report exists yet. Anything asserting on the automatic path has to wait for the report
    itself rather than for readiness.
    """
    for _ in range(tries):
        out = client.get(f"/api/ensembles/{ensemble_id}").json()
        if out.get("has_report") or out.get("report_error"):
            return out
        time.sleep(0.02)
    return client.get(f"/api/ensembles/{ensemble_id}").json()


def _run(client, run_id):
    """The run detail body, which is flat — no `run` wrapper."""
    res = client.get(f"/api/runs/{run_id}")
    assert res.status_code == 200, res.text
    return res.json()


def _member_config(client, run_id):
    return _run(client, run_id).get("config") or {}


# --------------------------------------------------------------------------- #
# the default shape
# --------------------------------------------------------------------------- #


class TestTheDefaultIsReplicates:
    def test_omitting_cells_gives_one_cell_and_nothing_varied(self, client):
        res = _create(client)
        assert res.status_code == 201, res.text
        out = res.json()

        assert out["spec"] == [{"label": "base", "n": 5, "overrides": {}}]
        assert len(out["members"]) == 5
        assert out["status"] == "running"

    def test_members_are_real_runs_carrying_their_cell(self, client):
        out = _create(client).json()
        detail = client.get(f"/api/ensembles/{out['ensemble_id']}").json()

        assert [m["cell"] for m in detail["members"]] == ["base"] * 5
        assert [m["index"] for m in detail["members"]] == [1, 2, 3, 4, 5]
        for member in detail["members"]:
            assert _run(client, member["run_id"])["run_id"] == member["run_id"]

    def test_the_run_list_says_which_ensemble_and_group_a_run_belongs_to(self, client):
        # The history list nests members under their ensemble; without these fields every
        # replicate lands among the individual conversations.
        out = _create(client).json()
        listed = {r["run_id"]: r for r in client.get("/api/runs").json()["runs"]}
        for m in out["members"]:
            assert listed[m["run_id"]]["ensemble_id"] == out["ensemble_id"]
            assert listed[m["run_id"]]["ensemble_cell"] == "base"

    def test_every_member_config_is_identical(self, client):
        # The premise of the default mode. If the configs differed, the ensemble would be
        # measuring a config difference and calling it sampling variance.
        out = _create(client).json()
        configs = {
            json.dumps(_member_config(client, m["run_id"]), sort_keys=True)
            for m in out["members"]
        }
        assert len(configs) == 1

    def test_members_are_not_branches(self, client):
        # `parent_run_id` means 'forked from', and the lineage walk follows it. Members are
        # siblings; using that field would render them as a branch tree.
        out = _create(client).json()
        for member in out["members"]:
            assert _run(client, member["run_id"])["parent_run_id"] is None

    def test_an_ensemble_does_not_appear_in_the_run_list(self, client):
        # The parent has no turns to generate. A row that looked like a run would
        # eventually be resumed or swept as stale.
        out = _create(client).json()
        runs = client.get("/api/runs").json()["runs"]
        assert out["ensemble_id"] not in {r["run_id"] for r in runs}
        assert len(runs) == 5

    def test_member_names_say_which_cell_they_are_in(self, client):
        out = _create(client).json()
        assert [m["name"] for m in out["members"]] == [
            f"renewal-base{i}" for i in range(1, 6)
        ]


# --------------------------------------------------------------------------- #
# cells
# --------------------------------------------------------------------------- #


class TestCells:
    CELLS = [
        {"label": "base", "n": 2},
        {"label": "hybrid", "n": 2, "overrides": {"selection.method": "hybrid"}},
    ]

    def test_a_hybrid_cell_diverges_only_where_declared(self, client):
        res = _create(client, cells=self.CELLS)
        assert res.status_code == 201, res.text
        detail = client.get(f"/api/ensembles/{res.json()['ensemble_id']}").json()

        by_cell: dict = {}
        for member in detail["members"]:
            by_cell.setdefault(member["cell"], []).append(
                _member_config(client, member["run_id"])
            )

        assert all(c["selection"]["method"] == "hybrid" for c in by_cell["hybrid"])
        assert all("selection" not in c for c in by_cell["base"])
        # Held fixed across both cells, which is what makes the comparison mean anything.
        assert all(c["max_messages"] == 4 for cs in by_cell.values() for c in cs)

    def test_cells_are_counted_per_cell_never_pooled(self, client):
        res = _create(
            client,
            cells=[
                {"label": "base", "n": 2},
                {"label": "hybrid", "n": 3, "overrides": {"selection.method": "hybrid"}},
            ],
        )
        detail = client.get(f"/api/ensembles/{res.json()['ensemble_id']}").json()
        assert detail["cells"] == [
            {"cell": "base", "declared": 2, "complete": 2, "settled": 2},
            {"cell": "hybrid", "declared": 3, "complete": 3, "settled": 3},
        ]

    def test_the_spec_and_base_config_are_stored_not_derived(self, client):
        # A cell has to appear in the report even if every one of its runs failed, so the
        # spec is stored rather than recomputed from the members.
        res = _create(client, cells=[{"label": "base", "n": 2, "overrides": {}}])
        detail = client.get(f"/api/ensembles/{res.json()['ensemble_id']}").json()
        assert detail["spec"] == [{"label": "base", "n": 2, "overrides": {}}]
        # The MATERIALISED base config, which is what the members were built from —
        # including the defaults Pydantic filled in. Storing what the user typed instead
        # would make the report's "held fixed" section a claim about the request rather
        # than about the runs.
        assert detail["base_config"]["max_messages"] == 4
        assert detail["base_config"]["knowledge_bases"] == []


# --------------------------------------------------------------------------- #
# what the route refuses
# --------------------------------------------------------------------------- #


class TestRefusals:
    def test_varying_turn_count_is_422_with_the_reason(self, client):
        res = _create(
            client,
            cells=[
                {"label": "long", "n": 2},
                {"label": "short", "n": 2, "overrides": {"max_messages": 8}},
            ],
        )
        assert res.status_code == 422
        # The reason is the useful part — "invalid" would teach nothing.
        assert "censoring" in res.json()["detail"]

    def test_a_cell_of_one_is_422(self, client):
        res = _create(client, cells=[{"label": "base", "n": 1}])
        assert res.status_code == 422
        assert "within-cell variance" in res.json()["detail"]

    def test_persona_jitter_is_422(self, client):
        res = _create(
            client,
            cells=[
                {"label": "base", "n": 2},
                {"label": "blunt", "n": 2,
                 "overrides": {"personas.dismissal_rule": "blunt"}},
            ],
        )
        assert res.status_code == 422
        assert "measuring instrument" in res.json()["detail"]

    def test_no_cast_is_422_as_it_is_for_a_single_run(self, client):
        res = _create(client, cast=[])
        assert res.status_code == 422
        assert "persona" in res.json()["detail"]

    def test_nothing_is_created_when_the_spec_is_refused(self, client):
        # The spec is checked before the parent row is written, so a refusal leaves no
        # ensemble and no runs behind.
        _create(client, cells=[{"label": "base", "n": 1}])
        assert client.get("/api/ensembles").json()["ensembles"] == []
        assert client.get("/api/runs").json()["runs"] == []

    def test_an_unknown_ensemble_is_404(self, client):
        assert client.get("/api/ensembles/nope").status_code == 404

    def test_research_on_a_SINGLE_run_is_accepted(self, client):
        """The positive half. Without it the refusal above could be passing because the
        research config is rejected everywhere, which would be a different bug."""
        with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=1)):
            res = client.post(
                "/api/runs",
                json={**REQUEST, "config": {"max_messages": 2,
                                            "research": {"enabled": True}}},
            )
        assert res.status_code == 201, res.text


# --------------------------------------------------------------------------- #
# report readiness
# --------------------------------------------------------------------------- #


class TestReportReadiness:
    def test_ready_once_every_member_is_settled(self, client):
        out = _create(client).json()
        detail = client.get(f"/api/ensembles/{out['ensemble_id']}").json()
        assert detail["report_ready"] is True
        assert detail["has_report"] is False
        assert detail["report"] is None

    def test_not_ready_while_a_member_is_still_generating(self, client):
        # A report over a running cell would count a conclusion as absent from a run that
        # simply had not reached it — §3.4's censoring, arrived at by impatience.
        with patch(
            "matrix_studio.api.manager.run_simulation", make_fake_run(turns=2, delay=1.0)
        ):
            res = client.post("/api/ensembles", json=_body())
            detail = client.get(f"/api/ensembles/{res.json()['ensemble_id']}").json()

        assert detail["report_ready"] is False
        assert detail["cells"][0]["settled"] < detail["cells"][0]["declared"]

    def test_a_failed_member_counts_as_settled(self, client):
        # 'Settled' is not 'succeeded'. A failed run will never finish, so waiting for it
        # would mean an ensemble with one bad member is never reportable at all.
        with patch(
            "matrix_studio.api.manager.run_simulation", make_fake_run(turns=1, fail=True)
        ):
            res = client.post("/api/ensembles", json=_body())
            detail = _settle(client, res.json()["ensemble_id"])

        assert detail["report_ready"] is True
        assert detail["cells"][0]["settled"] == 5
        assert detail["cells"][0]["complete"] == 0


# --------------------------------------------------------------------------- #
# listing, and the checks a fan-out owes
# --------------------------------------------------------------------------- #


class TestListingAndPreflight:
    def test_listed_with_the_report_fields_present(self, client):
        _create(client, name="first")
        _create(client, name="second")
        rows = client.get("/api/ensembles").json()["ensembles"]

        assert {r["name"] for r in rows} == {"first", "second"}
        # `has_report` lets a list view say whether one exists; the fields are always present
        # so a client never has to distinguish absent-key from null.
        assert all("has_report" in r and "report" in r for r in rows)
        assert all("report_error" in r for r in rows)

    def test_the_cost_cap_is_checked_before_a_fan_out(self, client, monkeypatch):
        # The worst place to skip the cap: it starts five runs, not one. Shared with the
        # single-run route via `_preflight` so the two cannot drift.
        async def over(*_a, **_k):
            return {"spent": 12.0, "cap": 10.0}

        monkeypatch.setattr("matrix_studio.orchestration.over_monthly_cap", over)
        res = _create(client)
        assert res.status_code == 402
        assert client.get("/api/ensembles").json()["ensembles"] == []
        assert client.get("/api/runs").json()["runs"] == []


# --------------------------------------------------------------------------- #
# a fan-out that only partly succeeds
# --------------------------------------------------------------------------- #


class TestAPartialFanOut:
    """The parent row is authoritative about membership.

    Driven against `RunManager.create_ensemble` directly, because the case is a member whose
    *creation* fails, and on the local path creation cannot fail through the route — the
    engine runs in a background task, so a raising engine is logged and the run row still
    exists. Patching `create_run` is the only honest way to reach the branch.
    """

    @pytest.fixture
    def manager(self, db, monkeypatch):
        from matrix_studio.api.manager import RunManager

        async def fake_name(topic, cast_names=None, model=None, name_exists=None):
            return {"name": "ens", "description": "d", "slug": "ens", "source": "llm"}

        monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
        return RunManager(db)

    @pytest.mark.asyncio
    async def test_a_member_that_failed_to_create_is_reported_not_dropped(
        self, manager, db, monkeypatch
    ):
        # The case §3.4 is about: a cell of 5 that produced 3 runs must not report as 3 of
        # 3. The aggregator has to see that two are missing, or it will read a conclusion
        # those runs never reached as one they declined.
        from matrix_studio import ensemble_spec

        calls = {"n": 0}

        async def flaky(request, *, owner_sub, groups=None, run_id=None, **kwargs):
            calls["n"] += 1
            if calls["n"] in (2, 4):
                raise RuntimeError("member refused")
            await db.create_run(
                run_id=run_id, topic=request["topic"], cast=request["cast"],
                name=request.get("name"), config=request.get("config"),
                ensemble_id=kwargs.get("ensemble_id"),
                ensemble_cell=kwargs.get("ensemble_cell"),
            )
            return {"run_id": run_id, "name": request.get("name")}

        monkeypatch.setattr(manager, "create_run", flaky)

        out = await manager.create_ensemble(
            {"topic": "t", "cast": [{"name": "A", "persona": "p", "goals": ["g"]}],
             "config": {"max_messages": 2}, "name": "ens"},
            ensemble_spec.replicates(n=5),
            owner_sub=db._owner_sub,
        )

        assert len(out["members"]) == 3
        assert len(out["failed"]) == 2
        assert out["status"] == "running", (
            "Three paid-for conversations must not be thrown away because the fourth "
            "failed."
        )

        # The parent still declares five, which is the whole point.
        members = await db.list_ensemble_members(out["ensemble_id"])
        assert len(members) == 5
        assert sum(1 for m in members if m["run"] is None) == 2
        assert [m["index"] for m in members] == [1, 2, 3, 4, 5]

    @pytest.mark.asyncio
    async def test_an_ensemble_whose_every_member_failed_is_marked_failed(
        self, manager, db, monkeypatch
    ):
        from matrix_studio import ensemble_spec

        async def always_fails(*_a, **_k):
            raise RuntimeError("no")

        monkeypatch.setattr(manager, "create_run", always_fails)

        out = await manager.create_ensemble(
            {"topic": "t", "cast": [{"name": "A", "persona": "p", "goals": ["g"]}],
             "name": "ens"},
            ensemble_spec.replicates(n=2),
            owner_sub=db._owner_sub,
        )
        assert out["status"] == "failed"
        row = await db.get_ensemble(out["ensemble_id"])
        assert row["status"] == "failed"


# --------------------------------------------------------------------------- #
# the launch stagger
# --------------------------------------------------------------------------- #


class TestTheStagger:
    """Read at call time, not bound at import.

    Pinned because the import-time version failed silently in both directions: the
    test-mode branch never fired (`_MSS_TEST_MODE` is set by a fixture that runs long
    after import, so the suite paid a second per member — a flat 5.05 s per fan-out, which
    is how it was found), and on Lambda the value froze into the sandbox so the
    environment variable could not change it without a redeploy. A silent 5x slowdown is
    the kind of regression nothing else here would catch.
    """

    def test_tests_pay_nothing(self, monkeypatch):
        from matrix_studio.api import manager

        monkeypatch.setenv("_MSS_TEST_MODE", "1")
        assert manager._ensemble_stagger() == 0.0

    def test_the_default_applies_outside_tests(self, monkeypatch):
        from matrix_studio.api import manager

        monkeypatch.delenv("_MSS_TEST_MODE", raising=False)
        monkeypatch.delenv("ENSEMBLE_STAGGER_SECONDS", raising=False)
        assert manager._ensemble_stagger() == manager.ENSEMBLE_STAGGER_SECONDS

    def test_the_environment_overrides_it_without_a_redeploy(self, monkeypatch):
        from matrix_studio.api import manager

        monkeypatch.delenv("_MSS_TEST_MODE", raising=False)
        monkeypatch.setenv("ENSEMBLE_STAGGER_SECONDS", "0.25")
        assert manager._ensemble_stagger() == 0.25

    def test_nonsense_falls_back_rather_than_raising(self, monkeypatch):
        # Read on the create path, so a typo in an environment variable must not turn
        # every fan-out into a 500.
        from matrix_studio.api import manager

        monkeypatch.delenv("_MSS_TEST_MODE", raising=False)
        monkeypatch.setenv("ENSEMBLE_STAGGER_SECONDS", "soon")
        assert manager._ensemble_stagger() == manager.ENSEMBLE_STAGGER_SECONDS

    def test_a_negative_stagger_is_clamped(self, monkeypatch):
        from matrix_studio.api import manager

        monkeypatch.delenv("_MSS_TEST_MODE", raising=False)
        monkeypatch.setenv("ENSEMBLE_STAGGER_SECONDS", "-5")
        assert manager._ensemble_stagger() == 0.0


# --------------------------------------------------------------------------- #
# the report route
# --------------------------------------------------------------------------- #


class TestTheReportRoute:
    """`POST /api/ensembles/{id}/report` — the retry path, not the normal one.

    Normally the last member to finish generates the report. This route exists for a retry
    after a failure and for ensembles that finished before the feature existed. The
    extractions here come from the suite-wide analysis mock, so the report's CONTENT is
    meaningless — what these assert is the route's contract.
    """

    def test_the_report_appears_without_being_asked_for(self, client):
        # The automatic path: no POST anywhere in this test.
        out = _create(client).json()
        detail = _wait_report(client, out["ensemble_id"])

        assert detail["has_report"] is True, detail.get("report_error")
        assert detail["report"]["ensemble_id"] == out["ensemble_id"]
        assert [c["cell"] for c in detail["report"]["cells"]] == ["base"]
        assert detail["report_cost_usd"] > 0
        assert detail["report_error"] is None

    def test_a_second_request_does_not_pay_again(self, client):
        out = _create(client).json()
        before = _wait_report(client, out["ensemble_id"])

        res = client.post(f"/api/ensembles/{out['ensemble_id']}/report")
        assert res.status_code == 200
        assert res.json()["claimed_by_another"] is True
        after = client.get(f"/api/ensembles/{out['ensemble_id']}").json()
        assert after["report_cost_usd"] == before["report_cost_usd"]

    def test_force_regenerates_and_returns_the_detail(self, client):
        out = _create(client).json()
        _wait_report(client, out["ensemble_id"])
        res = client.post(f"/api/ensembles/{out['ensemble_id']}/report?force=true")

        assert res.status_code == 200, res.text
        body = res.json()
        assert body["has_report"] is True
        assert body["ensemble_id"] == out["ensemble_id"]
        assert body["members"], "the detail shape, not a bare report"

    def test_a_refusal_is_409_with_the_reason(self, client):
        # A cell still running. 409 rather than 500: the caller can act on it — wait.
        with patch(
            "matrix_studio.api.manager.run_simulation", make_fake_run(turns=2, delay=1.0)
        ):
            created = client.post("/api/ensembles", json=_body()).json()
            res = client.post(
                f"/api/ensembles/{created['ensemble_id']}/report?force=true"
            )
            assert res.status_code == 409
            assert "still running" in res.json()["detail"]

    def test_an_unknown_ensemble_is_404(self, client):
        assert client.post("/api/ensembles/nope/report").status_code == 404


# --------------------------------------------------------------------------- #
# Research: once for the whole fan-out (PERSONA-RESEARCH.md §6)
# --------------------------------------------------------------------------- #


class TestAnEnsembleResearchesOnce:
    """§6 is the whole point of these: live search per member would give replicates DIFFERENT
    inputs, and `ENSEMBLE-CONVERSATIONS.md` §2 rests on the opposite — same config, same brief,
    so divergence is evidence about the brief. Independent searches make divergence
    unattributable, which destroys the only thing an ensemble is for.

    So the assertions are about identity, not about search: one pass, one record on the PARENT,
    and every member bound to the SAME collections. A count of searches is not directly
    observable; "all five members read one corpus" is, and it is the property that matters.
    """

    @pytest.fixture
    def researched(self, client, monkeypatch):
        """Fan out two members with research on, with the searching faked.

        The pass itself is faked rather than mocked out entirely, so the path that allocates
        targets, records on the parent and fans out is the real one.
        """
        from matrix_studio import research_state

        calls = []

        async def fake_definition(db, *, topic, cast, settings, owner_sub, label="", experts=()):
            calls.append({"topic": topic, "cast": [c.get("name") for c in cast],
                          "label": label})
            return research_state._record(
                research_state.RESEARCHED, batch="b1", provider="fake", cost_usd=0.25,
                scopes=[{"scope": "shared", "documents": 4, "controlling": 1,
                         "kb_id": settings.target_for(None)}],
            )

        monkeypatch.setattr(research_state, "research_definition", fake_definition)
        res = _create(client, config={"research": {"enabled": True}}, cells=[
            {"label": "base", "n": 2},
        ])
        assert res.status_code == 201, res.text
        return res.json(), calls

    def test_the_pass_runs_exactly_once_for_the_whole_fan_out(self, researched):
        _body, calls = researched
        assert len(calls) == 1, f"research ran {len(calls)} times; §6 requires once"
        assert calls[0]["label"].startswith("ensemble ")

    def test_every_member_binds_the_SAME_collections(self, client, researched):
        # The property §6 exists to guarantee. Two members reading different corpora are not
        # replicates, and any difference between them would be uninterpretable.
        body, _calls = researched
        detail = client.get(f"/api/ensembles/{body['ensemble_id']}").json()
        members = [m for m in detail["members"] if m.get("run")]
        assert len(members) == 2, detail["members"]
        bound = [
            _member_config(client, m["run_id"]).get("knowledge_bases") or []
            for m in members
        ]
        assert bound[0] == bound[1], f"members bound different collections: {bound}"
        assert bound[0], "members were bound no collections, so research reached no turn"

    def test_the_record_is_on_the_PARENT_not_copied_per_member(self, client, researched):
        # A copy per member would say N passes happened, which is the one thing that must not
        # be true. The parent is where "one pass for N members" can be stated.
        body, _calls = researched
        detail = client.get(f"/api/ensembles/{body['ensemble_id']}").json()
        assert detail["research"]["status"] == "researched"
        assert detail["research"]["cost_usd"] == 0.25
        for m in [m for m in detail["members"] if m.get("run")]:
            run = client.get(f"/api/runs/{m['run_id']}").json()
            # A member SKIPS its own pass, and says so rather than staying silent.
            assert run["research"] is None or run["research"]["status"] == "skipped"

    def test_a_member_never_researches_for_itself(self, client, researched):
        from matrix_studio import research_state

        body, _calls = researched
        detail = client.get(f"/api/ensembles/{body['ensemble_id']}").json()
        run_id = next(m["run_id"] for m in detail["members"] if m.get("run"))
        # Asserted at the state's own entry point, not only through the API: this guard is what
        # stops a member's Research state from searching again on a redeploy or a resume.
        from matrix_studio.storage import Database
        from matrix_studio.tenancy import LOCAL_USER_SUB

        async def check():
            store = Database(); await store.connect()
            try:
                db = store.for_owner(LOCAL_USER_SUB)
                return await research_state.run_research(
                    db, run_id, owner_sub=LOCAL_USER_SUB,
                )
            finally:
                await store.close()

        record = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(check())
        assert record["status"] == "skipped"
        assert "replicates stay replicates" in record["error"]


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


class TestExport:
    def test_an_ensemble_exports_as_markdown_and_html(self, client):
        eid = _create(client).json()["ensemble_id"]
        for fmt, media in (("md", "text/markdown"), ("html", "text/html")):
            r = client.get(f"/api/ensembles/{eid}/export", params={"format": fmt})
            assert r.status_code == 200, r.text
            assert r.headers["content-type"].startswith(media)
            assert r.headers["content-disposition"].startswith("attachment;")
            assert "renewal" in r.text

    def test_a_run_exports_as_markdown_and_html(self, client):
        eid = _create(client).json()["ensemble_id"]
        run_id = client.get(f"/api/ensembles/{eid}").json()["members"][0]["run_id"]
        for fmt in ("md", "html"):
            r = client.get(f"/api/runs/{run_id}/export", params={"format": fmt})
            assert r.status_code == 200, r.text
            assert f'.{fmt}"' in r.headers["content-disposition"]

    def test_pdf_is_refused_with_how_to_get_one(self, client):
        # PDF is the browser printing the HTML export; the refusal says so rather than "invalid".
        eid = _create(client).json()["ensemble_id"]
        r = client.get(f"/api/ensembles/{eid}/export", params={"format": "pdf"})
        assert r.status_code == 422
        assert "printed by the browser" in r.json()["detail"]

    def test_another_user_cannot_export_an_ensemble(self, client):
        """Not a `{ref}` route, so the tenancy registry does not cover it — asserted here."""
        from matrix_studio.api import identity

        eid = _create(client).json()["ensemble_id"]
        client.app.dependency_overrides[identity.current_user] = lambda: "sub-someone-else"
        try:
            assert client.get(f"/api/ensembles/{eid}/export").status_code == 404
        finally:
            client.app.dependency_overrides.pop(identity.current_user, None)


class TestBrief:
    def test_an_ensemble_brief_renders_in_both_formats(self, client):
        eid = _create(client).json()["ensemble_id"]
        for fmt, media in (("md", "text/markdown"), ("html", "text/html")):
            r = client.get(f"/api/ensembles/{eid}/brief", params={"format": fmt})
            assert r.status_code == 200, r.text
            assert r.headers["content-type"].startswith(media)
            assert "Decision brief" in r.text

    def test_a_run_brief_says_not_yet_measured(self, client):
        eid = _create(client).json()["ensemble_id"]
        run_id = client.get(f"/api/ensembles/{eid}").json()["members"][0]["run_id"]
        r = client.get(f"/api/runs/{run_id}/brief", params={"format": "md"})
        assert r.status_code == 200 and "not yet measured" in r.text

    def test_another_user_cannot_read_an_ensemble_brief(self, client):
        from matrix_studio.api import identity

        eid = _create(client).json()["ensemble_id"]
        client.app.dependency_overrides[identity.current_user] = lambda: "sub-someone-else"
        try:
            assert client.get(f"/api/ensembles/{eid}/brief").status_code == 404
        finally:
            client.app.dependency_overrides.pop(identity.current_user, None)
