# SPDX-License-Identifier: Apache-2.0
"""
The launch-time cost forecast (`matrix_studio/forecast.py`).

What these pin is the forecast's honesty, not its arithmetic alone:

- **A part with no history is "not yet measured"**, and the total then says it is incomplete rather
  than quietly omitting the part — the failure that made two estimates 3–4× low in one week.
- **Runs from before cost attribution do not price the parts they never recorded.** Their voice cost
  is exact; their speaker-selection cost is absent, not zero, and treating it as zero would forecast
  that part as free.
- **What a turn is depends on the method.** Rounds methods produce a response per persona per turn.
- **History is the caller's own.** One owner's runs never price another owner's launch.
"""

import json

import pytest
from fastapi.testclient import TestClient

from matrix_studio import forecast as fc
from matrix_studio.api.app import create_app
from tests.support import TEST_OWNER

AFTER = fc.ATTRIBUTION_SINCE + 60
BEFORE = fc.ATTRIBUTION_SINCE - 86400
SONNET = "bedrock/global.anthropic.claude-sonnet-5"
OPUS = "bedrock/global.anthropic.claude-opus-5"
KW = dict(default_model=SONNET, default_max=20, avatars_default=False, avatar_price=0.08)


def _row(cost_per_response=0.02, responses=40, cast=6, created=BEFORE, config=None, kinds=None,
         status="complete"):
    config = {"max_messages": 40, **(config or {})}
    by_kind = {"agent.response": cost_per_response * responses}
    by_kind.update(kinds or {})
    return {
        "id": f"r{cost_per_response}-{responses}-{cast}-{created}",
        "status": status,
        "created_at": created,
        "config_json": json.dumps(config),
        "cast_json": json.dumps([{"name": f"p{i}"} for i in range(cast)]),
        "turn_count": responses,
        "cost_by_kind": by_kind,
    }


def _history(*rows, summaries=None):
    h = fc.History()
    for i, r in enumerate(rows):
        obs = fc.observe(r, (summaries or {}).get(i), 20)
        if obs:
            h.runs.append(obs)
    return h


def _req(cast=6, **config):
    return {"topic": "t", "cast": [{"name": f"p{i}"} for i in range(cast)],
            "config": {"max_messages": 40, **config}, "summary": {"enabled": False}}


def _part(f, name):
    return next(p for p in f["parts"] if p["part"] == name)


class TestResponses:
    def test_moderated_runs_one_response_per_turn(self):
        assert fc.response_bounds({"max_messages": 40}, 6, 20) == {"ceiling": 40, "moderated": 40}

    @pytest.mark.parametrize("method", ["simultaneous", "rotation"])
    def test_round_methods_run_everybody_each_turn(self, method):
        # Measured: simultaneous at 8 turns produced 46–52 responses from 6 personas.
        b = fc.response_bounds({"max_messages": 8, "selection": {"method": method}}, 6, 20)
        assert b == {"ceiling": 48, "moderated": 0}

    def test_hybrid_opens_in_rounds_then_moderates(self):
        # Measured: hybrid at 36 with two opening rounds produced 47 responses.
        b = fc.response_bounds(
            {"max_messages": 36, "selection": {"method": "hybrid", "hybrid_opening_rounds": 2}}, 6, 20)
        assert b == {"ceiling": 46, "moderated": 34}

    def test_closing_round_adds_one_blind_round(self):
        b = fc.response_bounds({"max_messages": 10, "selection": {"closing_round": True}}, 6, 20)
        assert b == {"ceiling": 16, "moderated": 10}

    def test_the_deployment_default_applies_when_unset(self):
        assert fc.response_bounds({}, 3, 20)["ceiling"] == 20


class TestObservations:
    def test_unfinished_and_costless_runs_teach_nothing(self):
        assert fc.observe(_row(status="failed"), None, 20) is None
        # An imported transcript has turns and no cost; it would price every future run at zero.
        assert fc.observe(_row(cost_per_response=0.0), None, 20) is None

    def test_a_legacy_run_has_no_moderation_cost_rather_than_a_zero_one(self):
        assert fc.observe(_row(created=BEFORE), None, 20).moderation_cost is None
        assert fc.observe(_row(created=AFTER), None, 20).moderation_cost == 0.0

    def test_the_conversation_counts_passes_and_reflections(self):
        obs = fc.observe(_row(kinds={"agent.passed": 0.1, "agent.reflected": 0.2}), None, 20)
        assert obs.conversation_cost == pytest.approx(0.02 * 40 + 0.3)


class TestForecastRun:
    def test_prices_from_the_range_comparable_runs_covered(self):
        h = _history(_row(0.020), _row(0.022), _row(0.025))
        f = fc.forecast_run(_req(), h, **KW)
        conv = _part(f, "conversation")
        assert conv["low"] == pytest.approx(40 * 0.020)
        assert conv["high"] == pytest.approx(40 * 0.025)
        assert "3 past claude-sonnet-5 run(s)" in conv["basis"]

    def test_a_model_nobody_has_run_is_not_yet_measured_and_the_total_says_so(self):
        h = _history(_row(), _row(), _row())
        f = fc.forecast_run(_req(model=OPUS), h, **KW)
        conv = _part(f, "conversation")
        assert conv["measured"] is False and conv["low"] is None
        assert "not yet measured" in conv["basis"]
        assert f["complete"] is False and "conversation" in f["unmeasured"]

    def test_short_runs_are_priced_from_short_runs(self):
        # Every response reads the transcript so far, so a 4-turn run costs less per response.
        h = _history(*[_row(0.024, 40)] * 3, *[_row(0.012, 4, config={"max_messages": 4})] * 3)
        f = fc.forecast_run(_req(max_messages=4), h, **KW)
        assert _part(f, "conversation")["high"] == pytest.approx(4 * 0.012)

    def test_moderation_is_priced_only_from_runs_that_recorded_it(self):
        legacy = _history(_row(), _row(), _row())
        part = _part(fc.forecast_run(_req(), legacy, **KW), "speaker selection and checks")
        assert part["measured"] is False
        full = _history(_row(created=AFTER, kinds={"speaker.selected": 0.04}))
        part = _part(fc.forecast_run(_req(), full, **KW), "speaker selection and checks")
        assert part["high"] == pytest.approx(40 * 0.001)
        assert "1 run(s)" in part["basis"]

    def test_a_part_priced_from_too_few_runs_is_called_thin(self):
        f = fc.forecast_run(_req(), _history(_row(0.02)), **KW)
        assert _part(f, "conversation")["based_on"] == 1
        assert "conversation" in f["thin"]
        f = fc.forecast_run(_req(), _history(_row(), _row(), _row()), **KW)
        assert "conversation" not in f["thin"]

    def test_no_moderation_part_when_nobody_is_selected(self):
        f = fc.forecast_run(_req(max_messages=8, selection={"method": "simultaneous"}), _history(_row()), **KW)
        assert all(p["part"] != "speaker selection and checks" for p in f["parts"])

    def test_avatars_are_a_fixed_price_per_persona(self):
        f = fc.forecast_run(_req(cast=6, generate_avatars=True), _history(_row()), **KW)
        assert _part(f, "avatars")["low"] == _part(f, "avatars")["high"] == pytest.approx(0.48)
        f = fc.forecast_run(_req(cast=6, generate_avatars=False), _history(_row()), **KW)
        assert all(p["part"] != "avatars" for p in f["parts"])

    def test_early_stopping_widens_the_low_end_from_history(self):
        conv = [_row(0.02, n, config={"selection": {"stop_when_converged": True}}) for n in (20, 30, 40)]
        f = fc.forecast_run(_req(selection={"stop_when_converged": True}), _history(*conv), **KW)
        assert f["responses"] == {"low": 20, "typical": 30, "high": 40}

    def test_summary_priced_by_the_summary_model(self):
        h = _history(_row(), _row(), summaries={0: 0.03, 1: 0.05})
        req = {**_req(), "summary": {"enabled": True}}
        s = _part(fc.forecast_run(req, h, **KW), "summary")
        assert (s["low"], s["high"]) == (0.03, 0.05)


class TestEnsembleParts:
    def test_combine_sums_like_parts_across_runs(self):
        one = fc.forecast_run(_req(), _history(_row(0.02), _row(0.02), _row(0.02)), **KW)
        out = fc.combine([one, one, one], [None])
        assert out["runs"] == 3
        assert _part(out, "conversation")["low"] == pytest.approx(3 * 40 * 0.02)
        assert _part(out, "conversation")["basis"].startswith("×3 runs")

    def test_one_unmeasured_member_makes_the_part_unmeasured(self):
        h = _history(_row(), _row(), _row())
        a = fc.forecast_run(_req(), h, **KW)
        b = fc.forecast_run(_req(model=OPUS), h, **KW)
        out = fc.combine([a, b], [])
        assert _part(out, "conversation")["measured"] is False and out["complete"] is False

    def test_research_is_counted_per_collection_it_would_build(self):
        req = _req(research={"enabled": True})
        req["cast"][0]["structured"] = {"viewpoints": ["x"]}
        req["cast"][1]["structured"] = {"viewpoints": ["y"]}
        assert fc.research_collections(req) == 3  # shared + two personas with viewpoints
        h = fc.History(research_per_collection=[0.03, 0.05])
        p = fc.research_part(req, h)
        assert (p["low"], p["high"]) == (pytest.approx(0.09), pytest.approx(0.15))
        assert fc.research_part(_req(), h) is None

    def test_report_is_per_member(self):
        p = fc.report_part(5, fc.History(report_per_member=[0.19, 0.22]))
        assert (p["low"], p["high"]) == (pytest.approx(0.95), pytest.approx(1.10))
        assert fc.report_part(5, fc.History())["measured"] is False


# --------------------------------------------------------------------------- #
# The routes
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(aws_backend, tmp_path):
    fc._CACHE.clear()
    from matrix_studio.api.identity import current_user

    app = create_app(db_path=str(tmp_path / "test.db"))
    app.dependency_overrides[current_user] = lambda: TEST_OWNER
    with TestClient(app) as c:
        yield c
    fc._CACHE.clear()


async def _seed(owner, n=3, cost=0.02):
    from matrix_studio.storage import Database
    from tests.support import TEST_DATA_BUCKET, TEST_TABLE_PREFIX

    store = Database(table_prefix=TEST_TABLE_PREFIX, bucket=TEST_DATA_BUCKET, region="us-east-1")
    await store.connect()
    try:
        o = store.for_owner(owner)
        for i in range(n):
            rid = f"seed-{owner[-4:]}-{i}"
            await o.create_run(run_id=rid, topic="t", cast=[{"name": "A"}, {"name": "B"}],
                               name=rid, config={"max_messages": 4})
            for t in range(1, 5):
                await o.append_event(rid, t, t, "agent.response", {"content": "x", "cost_usd": cost}, "A")
            await o.update_run_status(rid, "complete")
    finally:
        await store.close()


class TestRoutes:
    BODY = {"topic": "t", "cast": [{"name": "A", "persona": "a"}, {"name": "B", "persona": "b"}],
            "config": {"max_messages": 4, "generate_avatars": False}, "summary": {"enabled": False}}

    def test_a_run_is_priced_from_the_callers_own_history(self, client):
        import asyncio

        asyncio.run(_seed(TEST_OWNER, cost=0.02))
        asyncio.run(_seed("sub-someone-else-9999", cost=5.0))
        res = client.post("/api/runs/forecast", json=self.BODY)
        assert res.status_code == 200, res.text
        conv = _part(res.json(), "conversation")
        # 4 responses × $0.02 — never the other owner's $5 a response.
        assert conv["low"] == conv["high"] == pytest.approx(0.08)
        assert res.json()["responses"] == {"low": 4, "typical": 4, "high": 4}

    def test_an_empty_account_forecasts_nothing_as_measured(self, client):
        res = client.post("/api/runs/forecast", json=self.BODY).json()
        assert res["complete"] is False and "conversation" in res["unmeasured"]
        assert res["low"] == 0.0

    def test_an_ensemble_prices_every_planned_member_and_the_report(self, client):
        import asyncio

        asyncio.run(_seed(TEST_OWNER, cost=0.02))
        body = {**self.BODY, "cells": [{"label": "base", "n": 3}]}
        res = client.post("/api/ensembles/forecast", json=body)
        assert res.status_code == 200, res.text
        out = res.json()
        assert out["runs"] == 3
        assert _part(out, "conversation")["low"] == pytest.approx(3 * 0.08)
        # No report has ever been built in this account, so it is unmeasured — and said.
        assert _part(out, "ensemble report")["measured"] is False

    def test_a_refused_spec_is_a_422_with_the_reason(self, client):
        body = {**self.BODY, "cells": [{"label": "base", "n": 2}, {"label": "x", "n": 2,
                                                                     "overrides": {"max_messages": 9}}]}
        res = client.post("/api/ensembles/forecast", json=body)
        assert res.status_code == 422

    def test_nothing_is_created(self, client):
        client.post("/api/runs/forecast", json=self.BODY)
        assert client.get("/api/runs").json()["runs"] == []
