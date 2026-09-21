# SPDX-License-Identifier: Apache-2.0
"""
When an ensemble's report may be generated, who generates it, and what shape is stored.

Three refusals carry this module, and each one exists because breaking it produces a
confident wrong answer rather than an error:

  a running cell     A conclusion counted as absent from a run that has not reached it yet
                     reads as a dissent. docs/ENSEMBLE-CONVERSATIONS.md §3.4.
  one usable run     A single conversation under an "ensemble report" heading looks like
                     corroborated evidence and is one sample.
  a pooled count     §4. Every tier is computed within a cell against that cell's own
                     denominator, so `unanimous` in one cell and `absent` in another stays
                     visible as a method-dependent finding instead of averaging to noise.

The claim is the other half. Members finish concurrently, so two of them can both observe
"everything is settled"; without a durable claim both would pay for an extraction pass and a
20k-token synthesis and the loser would overwrite the winner.

Model calls go through the injected `call` seam, never the network.
"""

import json

import pytest

from matrix_studio import ensemble_reporting
from tests.support import TEST_OWNER

pytestmark = pytest.mark.asyncio

OTHER = "sub-other-9999"


def _extraction(*personas, outcome="We shipped it.", unresolved=()):
    return {
        "personas": [
            {
                "name": name,
                "final_position": f"{name} position",
                "demands": list(demands),
                "refusals": list(refusals),
                "concessions": [],
            }
            for name, demands, refusals in personas
        ],
        "outcome": outcome,
        "unresolved": list(unresolved),
    }


class FakeModel:
    """One `_acompletion`-shaped callable. Replies by prompt content, and counts calls.

    The extraction prompt carries the transcript, so each run is told apart by the text its
    members spoke — which is how a test can give two runs different positions.
    """

    def __init__(self, by_marker, synthesis="## Synthesis\nIt held."):
        self.by_marker = by_marker
        self.synthesis = synthesis
        self.calls = 0

    async def __call__(self, messages, model=None, temperature=0.4, max_tokens=None):
        self.calls += 1
        prompt = messages[0]["content"]
        for marker, payload in self.by_marker.items():
            if marker in prompt:
                return {"content": json.dumps(payload), "tokens_in": 10,
                        "tokens_out": 5, "cost_usd": 0.01, "finish_reason": "stop"}
        return {"content": self.synthesis, "tokens_in": 10, "tokens_out": 5,
                "cost_usd": 0.05, "finish_reason": "stop"}


async def _member(db, ensemble_id, run_id, cell, name, said, status="complete"):
    await db.create_run(
        run_id=run_id, topic="t", cast=[], name=name,
        ensemble_id=ensemble_id, ensemble_cell=cell,
    )
    await db.append_event(
        run_id=run_id, turn=1, seq=0, event_type="agent.response",
        agent_name="Ada", payload={"speaker": "Ada", "message": said},
    )
    await db.update_run_status(run_id, status)


async def _ensemble(db, spec, members):
    await db.create_ensemble(
        ensemble_id="ens", topic="renewal", spec=spec, members=members,
        base_config={"max_messages": 4}, name="renewal",
    )


# --------------------------------------------------------------------------- #
# the refusals
# --------------------------------------------------------------------------- #


class TestItRefusesToMislead:
    async def test_a_running_member_blocks_the_report(self, db):
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta", status="running")

        with pytest.raises(ValueError, match="still running"):
            await ensemble_reporting.build(db, "ens", call=FakeModel({}))

    async def test_one_usable_run_is_refused(self, db):
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        # r2 was never created, so only one run can be extracted.

        model = FakeModel({"alpha": _extraction(("Ada", ["labwork"], []))})
        with pytest.raises(ValueError, match="minimum"):
            await ensemble_reporting.build(db, "ens", call=model)

    async def test_the_minimum_is_about_usable_not_present(self, db):
        # Both runs exist; one extraction comes back unreadable. Still one sample.
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta")

        class Unreadable(FakeModel):
            async def __call__(self, messages, **kwargs):
                if "beta" in messages[0]["content"]:
                    return {"content": "not json at all", "cost_usd": 0.0,
                            "tokens_in": 1, "tokens_out": 1}
                return await super().__call__(messages, **kwargs)

        model = Unreadable({"alpha": _extraction(("Ada", ["labwork"], []))})
        with pytest.raises(ValueError, match="minimum"):
            await ensemble_reporting.build(db, "ens", call=model)

    async def test_a_missing_ensemble_is_refused(self, db):
        with pytest.raises(ValueError, match="does not exist"):
            await ensemble_reporting.build(db, "nope", call=FakeModel({}))


# --------------------------------------------------------------------------- #
# per-cell tiers
# --------------------------------------------------------------------------- #


class TestCellsAreNeverPooled:
    async def test_a_method_dependent_claim_stays_visible(self, db):
        # The §4 case: a claim held by every run in one cell and none in the other. Pooled it
        # would read 2/4 — "split, weak". Per cell it is a strong, method-dependent finding.
        await _ensemble(
            db,
            [{"label": "base", "n": 2, "overrides": {}},
             {"label": "hybrid", "n": 2,
              "overrides": {"selection.method": "hybrid"}}],
            [{"run_id": "b1", "cell": "base", "index": 1},
             {"run_id": "b2", "cell": "base", "index": 2},
             {"run_id": "h1", "cell": "hybrid", "index": 1},
             {"run_id": "h2", "cell": "hybrid", "index": 2}],
        )
        await _member(db, "ens", "b1", "base", "b-one", "alpha1")
        await _member(db, "ens", "b2", "base", "b-two", "alpha2")
        await _member(db, "ens", "h1", "hybrid", "h-one", "beta1")
        await _member(db, "ens", "h2", "hybrid", "h-two", "beta2")

        model = FakeModel({
            "alpha1": _extraction(("Ada", ["labwork required"], [])),
            "alpha2": _extraction(("Ada", ["labwork required"], [])),
            "beta1": _extraction(("Ada", ["video required"], [])),
            "beta2": _extraction(("Ada", ["video required"], [])),
        })
        report = await ensemble_reporting.build(db, "ens", call=model)

        claims = {c["claim"]: c["per_cell"] for c in report["claims"]}
        assert claims["labwork required"]["base"]["tier"] == "unanimous"
        assert claims["labwork required"]["hybrid"]["tier"] == "absent"
        assert claims["video required"]["base"]["tier"] == "absent"
        assert claims["video required"]["hybrid"]["tier"] == "unanimous"

    async def test_the_denominator_is_the_cell_not_the_ensemble(self, db):
        await _ensemble(
            db,
            [{"label": "base", "n": 3, "overrides": {}},
             {"label": "hybrid", "n": 2,
              "overrides": {"selection.method": "hybrid"}}],
            [{"run_id": "b1", "cell": "base", "index": 1},
             {"run_id": "b2", "cell": "base", "index": 2},
             {"run_id": "b3", "cell": "base", "index": 3},
             {"run_id": "h1", "cell": "hybrid", "index": 1},
             {"run_id": "h2", "cell": "hybrid", "index": 2}],
        )
        for rid, said in (("b1", "a1"), ("b2", "a2"), ("b3", "a3")):
            await _member(db, "ens", rid, "base", f"b-{rid}", said)
        for rid, said in (("h1", "z1"), ("h2", "z2")):
            await _member(db, "ens", rid, "hybrid", f"h-{rid}", said)

        shared = _extraction(("Ada", ["shared demand"], []))
        model = FakeModel({m: shared for m in ("a1", "a2", "a3", "z1", "z2")})
        report = await ensemble_reporting.build(db, "ens", call=model)

        per_cell = report["claims"][0]["per_cell"]
        assert per_cell["base"] == {
            "held": 3, "of": 3, "tier": "unanimous",
            "runs": ["b-b1", "b-b2", "b-b3"],
        }
        assert per_cell["hybrid"]["of"] == 2

    async def test_a_cell_that_produced_nothing_is_null_not_zero(self, db):
        # An empty cell has no opinion. Reporting 0-of-N would let a reader conclude that
        # cell rejected the claim.
        await _ensemble(
            db,
            [{"label": "base", "n": 2, "overrides": {}},
             {"label": "hybrid", "n": 2,
              "overrides": {"selection.method": "hybrid"}}],
            [{"run_id": "b1", "cell": "base", "index": 1},
             {"run_id": "b2", "cell": "base", "index": 2},
             {"run_id": "h1", "cell": "hybrid", "index": 1},
             {"run_id": "h2", "cell": "hybrid", "index": 2}],
        )
        await _member(db, "ens", "b1", "base", "b-one", "alpha1")
        await _member(db, "ens", "b2", "base", "b-two", "alpha2")
        # The hybrid cell's runs were never created.

        model = FakeModel({
            "alpha1": _extraction(("Ada", ["labwork"], [])),
            "alpha2": _extraction(("Ada", ["labwork"], [])),
        })
        report = await ensemble_reporting.build(db, "ens", call=model)

        assert report["claims"][0]["per_cell"]["hybrid"] is None
        assert [c["cell"] for c in report["cells"]] == ["base", "hybrid"]
        assert [m["cell"] for m in report["missing_members"]] == ["hybrid", "hybrid"]

    async def test_refusals_are_tiered_alongside_demands(self, db):
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta")

        model = FakeModel({
            "alpha": _extraction(("Ada", [], ["will not accept a checkbox"])),
            "beta": _extraction(("Ada", [], ["will not accept a checkbox"])),
        })
        report = await ensemble_reporting.build(db, "ens", call=model)

        row = report["claims"][0]
        assert row["kind"] == "refusal"
        assert row["per_cell"]["base"]["tier"] == "unanimous"


# --------------------------------------------------------------------------- #
# the report's shape
# --------------------------------------------------------------------------- #


class TestTheStoredShape:
    @pytest.fixture
    async def report(self, db):
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta")
        model = FakeModel({
            "alpha": _extraction(("Ada", ["labwork"], []), unresolved=["who pays"]),
            "beta": _extraction(("Ada", ["labwork"], []), unresolved=["who pays"]),
        })
        return await ensemble_reporting.build(db, "ens", call=model)

    async def test_it_carries_its_own_caveats(self, report):
        # In the artefact, not only in a doc: whoever reads "1 of 5" in a UI has no link
        # to §8.2, and the counter under-merges.
        text = " ".join(report["caveats"])
        assert "FLOOR" in text
        assert "unanimous / split / rare" in text

    async def test_the_cost_covers_every_call(self, report):
        # Two extractions at 0.01 plus one synthesis at 0.05.
        assert report["cost_usd"] == pytest.approx(0.07)

    async def test_the_synthesis_and_the_computed_sections_are_both_present(self, report):
        assert report["synthesis"].startswith("## Synthesis")
        assert "Ada" in report["per_persona"]
        assert report["agreements"]["unresolved_by_frequency"][0]["count"] == 2

    async def test_it_is_json_serialisable(self, report):
        # It is stored as JSON on the parent row, so anything unserialisable here is a
        # write failure at the worst moment — after every model call has been paid for.
        assert json.loads(json.dumps(report))["ensemble_id"] == "ens"

    async def test_synthesis_can_be_skipped(self, db):
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta")
        model = FakeModel({"alpha": _extraction(("Ada", ["x"], [])),
                           "beta": _extraction(("Ada", ["x"], []))})

        report = await ensemble_reporting.build(db, "ens", call=model, synthesise=False)
        assert report["synthesis"] == ""
        assert model.calls == 2
        assert report["claims"], "the computed sections do not need the synthesis"


# --------------------------------------------------------------------------- #
# the claim
# --------------------------------------------------------------------------- #


class TestTheClaim:
    async def _two_members(self, db):
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta")
        return FakeModel({"alpha": _extraction(("Ada", ["x"], [])),
                          "beta": _extraction(("Ada", ["x"], []))})

    async def test_only_the_first_caller_generates(self, db):
        model = await self._two_members(db)

        first = await ensemble_reporting.generate(db, "ens", call=model)
        assert first is not None
        calls_after_first = model.calls

        second = await ensemble_reporting.generate(db, "ens", call=model)
        assert second is None, "the second caller must not pay for a second report"
        assert model.calls == calls_after_first

    async def test_the_report_is_stored_with_its_cost_and_status(self, db):
        model = await self._two_members(db)
        await ensemble_reporting.generate(db, "ens", call=model)

        row = await db.get_ensemble("ens")
        assert json.loads(row["report_json"])["ensemble_id"] == "ens"
        assert row["report_cost_usd"] == pytest.approx(0.07)
        assert row["status"] == "complete"
        assert row["completed_at"] > 0
        assert row["report_error"] is None

    async def test_force_regenerates(self, db):
        model = await self._two_members(db)
        await ensemble_reporting.generate(db, "ens", call=model)
        before = model.calls

        again = await ensemble_reporting.generate(db, "ens", call=model, force=True)
        assert again is not None
        assert model.calls > before

    async def test_a_failure_is_recorded_rather_than_raised(self, db):
        # The runs are already paid for and intact; the report is commentary. A caller gets
        # None and the reason lands on the row, so "not generated yet" and "refused because
        # a cell was still running" stay distinguishable.
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta", status="running")

        out = await ensemble_reporting.generate(db, "ens", call=FakeModel({}))
        assert out is None
        row = await db.get_ensemble("ens")
        assert "still running" in row["report_error"]
        assert row["report_json"] is None

    async def test_a_successful_retry_clears_the_earlier_error(self, db):
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta", status="running")
        await ensemble_reporting.generate(db, "ens", call=FakeModel({}))
        assert (await db.get_ensemble("ens"))["report_error"]

        # The straggler finishes, and a forced retry succeeds.
        await db.update_run_status("r2", "complete")
        model = FakeModel({"alpha": _extraction(("Ada", ["x"], [])),
                           "beta": _extraction(("Ada", ["x"], []))})
        assert await ensemble_reporting.generate(db, "ens", call=model, force=True)

        row = await db.get_ensemble("ens")
        assert row["report_error"] is None, (
            "a stale error beside a good report would say the report cannot be trusted"
        )
        assert row["report_json"]

    async def test_claiming_a_missing_ensemble_is_false_not_an_error(self, db):
        assert await db.claim_ensemble_report("nope") is False


# --------------------------------------------------------------------------- #
# the trigger
# --------------------------------------------------------------------------- #


class TestTheMemberTrigger:
    async def test_the_last_member_to_finish_triggers_it(self, db, monkeypatch):
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta", status="running")

        model = FakeModel({"alpha": _extraction(("Ada", ["x"], [])),
                           "beta": _extraction(("Ada", ["x"], []))})
        monkeypatch.setattr("matrix_studio.analysis._acompletion", model)

        # r1 finishing is not enough — r2 is still running.
        await ensemble_reporting.maybe_report_for_member(db, await db.get_run("r1"))
        assert (await db.get_ensemble("ens"))["report_json"] is None
        assert model.calls == 0, "not a single call may be paid for while a cell is running"

        await db.update_run_status("r2", "complete")
        await ensemble_reporting.maybe_report_for_member(db, await db.get_run("r2"))
        assert (await db.get_ensemble("ens"))["report_json"]

    async def test_a_failed_member_still_counts_as_finished(self, db, monkeypatch):
        # 'Settled' is not 'succeeded'. Waiting for a failed run to finish would leave an
        # ensemble with one bad member permanently unreportable.
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta", status="failed")

        model = FakeModel({"alpha": _extraction(("Ada", ["x"], [])),
                           "beta": _extraction(("Ada", ["x"], []))})
        monkeypatch.setattr("matrix_studio.analysis._acompletion", model)

        await ensemble_reporting.maybe_report_for_member(db, await db.get_run("r1"))
        assert (await db.get_ensemble("ens"))["report_json"]

    async def test_a_standalone_run_triggers_nothing(self, db):
        await db.create_run(run_id="solo", topic="t", cast=[], name="solo")
        await ensemble_reporting.maybe_report_for_member(db, await db.get_run("solo"))
        # No exception, no ensemble, nothing to assert beyond that it did not try.

    async def test_it_never_raises_when_the_report_fails(self, db, monkeypatch):
        # A run must not fail because commentary on it did.
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta")

        async def explode(*_a, **_k):
            raise RuntimeError("bedrock is down")

        monkeypatch.setattr("matrix_studio.analysis._acompletion", explode)
        await ensemble_reporting.maybe_report_for_member(db, await db.get_run("r1"))

        row = await db.get_ensemble("ens")
        assert "bedrock is down" in (row["report_error"] or "")


# --------------------------------------------------------------------------- #
# tenancy
# --------------------------------------------------------------------------- #


class TestTenancy:
    async def test_another_owner_cannot_claim_it(self, db):
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1}])
        assert await db.claim_ensemble_report("ens", owner_sub=OTHER) is False
        # And the real owner's claim is still available.
        assert await db.claim_ensemble_report("ens", owner_sub=TEST_OWNER) is True
