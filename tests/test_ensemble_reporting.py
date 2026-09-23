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

        # Answers the clustering prompt too, so this fixture is the NORMAL shape rather than
        # the fallback one. Priced at 0.0 so the cost assertion stays about extraction and
        # synthesis.
        class Clustering(FakeModel):
            async def __call__(self, messages, **kwargs):
                if "Group the numbered claims" in messages[0]["content"]:
                    return {"content": json.dumps({"clusters": [
                        {"label": "labwork", "members": [0, 1]},
                    ]}), "cost_usd": 0.0, "tokens_in": 1, "tokens_out": 1,
                        "finish_reason": "stop"}
                return await super().__call__(messages, **kwargs)

        model = Clustering({
            "alpha": _extraction(("Ada", ["labwork"], []), unresolved=["who pays"]),
            "beta": _extraction(("Ada", ["labwork"], []), unresolved=["who pays"]),
        })
        return await ensemble_reporting.build(db, "ens", call=model)

    async def test_it_carries_its_own_caveats(self, report):
        # In the artefact, not only in a doc: whoever reads a count in a UI has no link to §8.2.
        text = " ".join(report["caveats"])
        assert "biased against merging" in text
        assert "check them before trusting a count" in text
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
        # Two extractions plus the clustering attempt. Clustering is NOT part of the synthesis:
        # it feeds the computed counts, so skipping the prose must not silently skip it.
        assert model.calls == 3
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


# --------------------------------------------------------------------------- #
# the lease
# --------------------------------------------------------------------------- #


class TestTheLeaseExpires:
    """A claim that never expired would strand the report permanently.

    The stranding is total, which is why this is not a nicety: on the automatic path there is
    exactly ONE trigger — the last member to finish — so if that attempt dies without running
    its failure handler (a Lambda timeout, an OOM, a deploy mid-build) nothing else ever tries.
    The ensemble sits at "building the report" for ever with no error to act on.
    """

    async def _ens(self, db):
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1}])

    async def test_a_live_claim_is_not_overtaken(self, db):
        await self._ens(db)
        assert await db.claim_ensemble_report("ens") is True
        assert await db.claim_ensemble_report("ens") is False

    async def test_an_expired_claim_is_reclaimable(self, db):
        await self._ens(db)
        assert await db.claim_ensemble_report("ens") is True
        # A zero lease makes every claim already expired, which is the same condition a
        # timed-out claimant leaves behind.
        assert await db.claim_ensemble_report("ens", lease_seconds=0) is True

    async def test_the_lease_outlives_the_finalise_timeout(self, db):
        # Otherwise a claimant still working could be overtaken and two callers would both pay
        # for an extraction pass and a synthesis. 15 minutes is the Lambda's timeout.
        from matrix_studio.storage.dynamo import REPORT_LEASE_SECONDS

        assert REPORT_LEASE_SECONDS > 15 * 60

    async def test_reclaiming_refreshes_the_lease(self, db):
        await self._ens(db)
        await db.claim_ensemble_report("ens")
        await db.claim_ensemble_report("ens", lease_seconds=0)
        # The second claimant now holds a fresh lease, so a third caller is refused.
        assert await db.claim_ensemble_report("ens") is False


# --------------------------------------------------------------------------- #
# metering
# --------------------------------------------------------------------------- #


class TestTheReportIsMetered:
    async def test_its_cost_counts_against_the_monthly_total(self, db):
        # Every member run's spend is recorded by `execute_slice`, so the fan-out is metered.
        # The report was not: one extraction per member plus a 20k-token synthesis, charged
        # nowhere. A 12-member ensemble could spend real money the cap never saw.
        before = await db.user_spend()
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta")
        model = FakeModel({"alpha": _extraction(("Ada", ["x"], [])),
                           "beta": _extraction(("Ada", ["x"], []))})

        report = await ensemble_reporting.generate(db, "ens", call=model)
        after = await db.user_spend()

        assert report["cost_usd"] == pytest.approx(0.07)
        assert after - before == pytest.approx(0.07)

    async def test_a_metering_failure_does_not_lose_the_report(self, db, monkeypatch):
        # The report is already paid for and stored by this point. Losing it because the
        # accounting failed would throw away the expensive thing to protect the cheap one.
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta")

        async def explode(*_a, **_k):
            raise RuntimeError("spend table gone")

        monkeypatch.setattr(db, "add_user_spend", explode)
        model = FakeModel({"alpha": _extraction(("Ada", ["x"], [])),
                           "beta": _extraction(("Ada", ["x"], []))})

        assert await ensemble_reporting.generate(db, "ens", call=model) is not None
        assert (await db.get_ensemble("ens"))["report_json"]


# --------------------------------------------------------------------------- #
# clustering, end to end
# --------------------------------------------------------------------------- #


class TestClusteredCounts:
    """The whole point of stage 1.5, measured against the bug it fixes.

    Before this, the first live 5-replicate ensemble counted 128 of 128 claims as unique: every
    row read 1/5 while the synthesis found four conclusions in 5 of 5. These tests use the same
    shape — one requirement worded differently in each run — and assert the count is right.
    """

    async def _three_runs_saying_the_same_thing(self, db):
        await _ensemble(db, [{"label": "base", "n": 3, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2},
                         {"run_id": "r3", "cell": "base", "index": 3}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta")
        await _member(db, "ens", "r3", "base", "m3", "gamma")
        # Three phrasings of ONE requirement. `_normalise` keys these as three claims.
        return {
            "alpha": _extraction(("Ada", ["a verified weight before approval"], [])),
            "beta": _extraction(("Ada", ["a confirmed weight is required first"], [])),
            "gamma": _extraction(("Ada", ["weight must be verified before we approve"], [])),
        }

    async def test_text_matching_produces_the_bug(self, db):
        # The baseline, asserted so the fix is measured against something real rather than
        # against a description of something real.
        by_marker = await self._three_runs_saying_the_same_thing(db)
        model = FakeModel(by_marker)

        report = await ensemble_reporting.build(db, "ens", call=model, cluster=False)

        assert report["clustered"] is False
        assert len(report["claims"]) == 3, "one requirement, counted three times"
        assert all(c["per_cell"]["base"]["tier"] == "rare" for c in report["claims"])

    async def test_clustering_counts_it_once_as_unanimous(self, db):
        by_marker = await self._three_runs_saying_the_same_thing(db)

        class WithClusters(FakeModel):
            async def __call__(self, messages, **kwargs):
                if "Group the numbered claims" in messages[0]["content"]:
                    return {"content": json.dumps({"clusters": [
                        {"label": "verified weight before approval", "members": [0, 1, 2]},
                    ]}), "cost_usd": 0.02, "tokens_in": 1, "tokens_out": 1,
                        "finish_reason": "stop"}
                return await super().__call__(messages, **kwargs)

        report = await ensemble_reporting.build(db, "ens", call=WithClusters(by_marker))

        assert report["clustered"] is True
        assert len(report["claims"]) == 1
        row = report["claims"][0]
        assert row["claim"] == "verified weight before approval"
        assert row["per_cell"]["base"] == {
            "held": 3, "of": 3, "tier": "unanimous", "runs": ["m1", "m2", "m3"],
        }

    async def test_a_merge_is_auditable(self, db):
        # A clustering nobody can inspect replaces one wrong number with a different wrong
        # number. Every row keeps the phrasings that were grouped and the runs they came from,
        # so a reader who disagrees with a merge can see it.
        by_marker = await self._three_runs_saying_the_same_thing(db)

        class WithClusters(FakeModel):
            async def __call__(self, messages, **kwargs):
                if "Group the numbered claims" in messages[0]["content"]:
                    return {"content": json.dumps({"clusters": [
                        {"label": "verified weight before approval", "members": [0, 1, 2]},
                    ]}), "cost_usd": 0.0, "tokens_in": 1, "tokens_out": 1}
                return await super().__call__(messages, **kwargs)

        report = await ensemble_reporting.build(db, "ens", call=WithClusters(by_marker))
        variants = report["claims"][0]["variants"]

        assert len(variants) == 3
        assert [v["runs"] for v in variants] == [["m2"], ["m1"], ["m3"]]
        assert "a confirmed weight is required first" in {v["text"] for v in variants}

    async def test_an_unsound_clustering_falls_back_and_says_so(self, db):
        # Dropping claims under-counts exactly like the bug being fixed, while looking fixed.
        # So it is rejected wholesale rather than partly trusted, and the caveat stops claiming
        # the counts are reliable.
        by_marker = await self._three_runs_saying_the_same_thing(db)

        class DropsOne(FakeModel):
            async def __call__(self, messages, **kwargs):
                if "Group the numbered claims" in messages[0]["content"]:
                    return {"content": json.dumps({"clusters": [
                        {"label": "weight", "members": [0, 1]},   # claim 2 dropped
                    ]}), "cost_usd": 0.0, "tokens_in": 1, "tokens_out": 1}
                return await super().__call__(messages, **kwargs)

        report = await ensemble_reporting.build(db, "ens", call=DropsOne(by_marker))

        assert report["clustered"] is False
        assert len(report["claims"]) == 3, "fell back to text matching, all three counted"
        assert "NOT RELIABLE" in report["caveats"][0]
        assert "rejected as unsound" in report["caveats"][0]

    async def test_the_caveat_tells_the_reader_which_mode_produced_the_counts(self, db):
        # The two modes are not "precise" and "less precise": text matching's numbers are
        # noise. A reader has to be able to tell which they are looking at.
        by_marker = await self._three_runs_saying_the_same_thing(db)

        class WithClusters(FakeModel):
            async def __call__(self, messages, **kwargs):
                if "Group the numbered claims" in messages[0]["content"]:
                    return {"content": json.dumps({"clusters": [
                        {"label": "weight", "members": [0, 1, 2]},
                    ]}), "cost_usd": 0.0, "tokens_in": 1, "tokens_out": 1}
                return await super().__call__(messages, **kwargs)

        clustered = await ensemble_reporting.build(db, "ens", call=WithClusters(by_marker))
        assert "check them before trusting a count" in clustered["caveats"][0]

        plain = await ensemble_reporting.build(db, "ens", call=FakeModel(by_marker), cluster=False)
        assert "Read the synthesis instead" in plain["caveats"][0]

    async def test_the_clustering_call_is_paid_for_once(self, db):
        by_marker = await self._three_runs_saying_the_same_thing(db)

        class WithClusters(FakeModel):
            async def __call__(self, messages, **kwargs):
                if "Group the numbered claims" in messages[0]["content"]:
                    return {"content": json.dumps({"clusters": [
                        {"label": "weight", "members": [0, 1, 2]},
                    ]}), "cost_usd": 0.03, "tokens_in": 1, "tokens_out": 1}
                return await super().__call__(messages, **kwargs)

        report = await ensemble_reporting.build(db, "ens", call=WithClusters(by_marker))
        # 3 extractions at 0.01, one synthesis at 0.05, one clustering at 0.03.
        assert report["cost_usd"] == pytest.approx(0.11)


# --------------------------------------------------------------------------- #
# dispatching to the report function
# --------------------------------------------------------------------------- #


class TestDispatch:
    """The report has its own function because it outgrew `finalise`.

    Measured at 12–19 minutes against Lambda's 15-minute ceiling — not because the work is
    large (the clustering reply is ~1,100 tokens) but because the model spends ~29,000 output
    tokens reasoning to produce it. `finalise` writes a run's terminal status and must not be
    able to time out behind commentary on that run.
    """

    async def _ens(self, db):
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta")

    async def test_no_function_configured_means_run_it_here(self, db, monkeypatch):
        # The local case, and not a misconfiguration: one long-lived process can await it.
        monkeypatch.delenv("ENSEMBLE_REPORT_FUNCTION", raising=False)
        await self._ens(db)
        assert await ensemble_reporting.dispatch(db, "ens") is False

    async def test_it_invokes_asynchronously_with_the_owner(self, db, monkeypatch):
        await self._ens(db)
        monkeypatch.setenv("ENSEMBLE_REPORT_FUNCTION", "matrix-studio-ensemble-report")
        calls = []

        class FakeLambda:
            def invoke(self, **kw):
                calls.append(kw)
                return {}

        monkeypatch.setattr("boto3.client", lambda *a, **k: FakeLambda())

        assert await ensemble_reporting.dispatch(db, "ens", force=True) is True
        assert len(calls) == 1
        # Event, not RequestResponse: a synchronous invoke would make the caller wait the full
        # report and reintroduce the timeout this move exists to remove.
        assert calls[0]["InvocationType"] == "Event"
        assert calls[0]["FunctionName"] == "matrix-studio-ensemble-report"
        payload = json.loads(calls[0]["Payload"].decode())
        assert payload == {"ensemble_id": "ens", "owner_sub": TEST_OWNER, "force": True}

    async def test_the_member_trigger_dispatches_instead_of_working(self, db, monkeypatch):
        await self._ens(db)
        monkeypatch.setenv("ENSEMBLE_REPORT_FUNCTION", "fn")
        calls = []
        monkeypatch.setattr(
            "boto3.client",
            lambda *a, **k: type("L", (), {"invoke": lambda _s, **kw: calls.append(kw) or {}})(),
        )

        async def explode(*_a, **_k):
            raise AssertionError("no model call may happen in the dispatching process")

        monkeypatch.setattr("matrix_studio.analysis._acompletion", explode)
        await ensemble_reporting.maybe_report_for_member(db, await db.get_run("r1"))

        assert len(calls) == 1
        # And nothing was claimed here: the claim belongs to whoever BUILDS it, or a dropped
        # invoke would leave a claim held by nobody for the length of its lease.
        assert (await db.get_ensemble("ens"))["report_claimed_at"] is None

    async def test_it_does_not_dispatch_while_a_member_is_running(self, db, monkeypatch):
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta", status="running")
        monkeypatch.setenv("ENSEMBLE_REPORT_FUNCTION", "fn")
        calls = []
        monkeypatch.setattr(
            "boto3.client",
            lambda *a, **k: type("L", (), {"invoke": lambda _s, **kw: calls.append(kw) or {}})(),
        )

        await ensemble_reporting.maybe_report_for_member(db, await db.get_run("r1"))
        assert calls == [], "a report over a running cell would count censoring as dissent"

    async def test_an_ownerless_ensemble_is_not_dispatched(self, db, monkeypatch):
        # The function assumes the tenant role per request, so with no identity to assume it
        # for there is nothing to dispatch to.
        monkeypatch.setenv("ENSEMBLE_REPORT_FUNCTION", "fn")
        assert await ensemble_reporting.dispatch(db, "missing-ensemble") is False


class TestTheExtractionsRunConcurrently:
    async def test_all_extractions_are_in_flight_at_once(self, db):
        # They were sequential only because the report shared an invocation with a member's
        # last turn. Wall-clock is now the binding constraint — 12–19 minutes against a
        # 15-minute ceiling — and the extractions are completely independent.
        await _ensemble(db, [{"label": "base", "n": 3, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2},
                         {"run_id": "r3", "cell": "base", "index": 3}])
        for rid, said in (("r1", "alpha"), ("r2", "beta"), ("r3", "gamma")):
            await _member(db, "ens", rid, "base", f"m-{rid}", said)

        import asyncio

        peak = {"now": 0, "max": 0}

        async def call(messages, model=None, temperature=0.4, max_tokens=None):
            if "Group the numbered claims" in messages[0]["content"]:
                return {"content": json.dumps({"clusters": [{"label": "x", "members": [0, 1, 2]}]}),
                        "cost_usd": 0.0, "tokens_in": 1, "tokens_out": 1}
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.05)
            peak["now"] -= 1
            return {"content": json.dumps(_extraction(("Ada", ["x"], []))),
                    "cost_usd": 0.01, "tokens_in": 1, "tokens_out": 1}

        await ensemble_reporting.build(db, "ens", call=call)
        assert peak["max"] == 3, f"extractions overlapped only {peak['max']} deep"


# --------------------------------------------------------------------------- #
# one keying for every section that counts
# --------------------------------------------------------------------------- #


class TestEverySectionSharesTheLabels:
    """The claim table, the per-persona view and the unresolved tally must agree.

    They did not, briefly: the table reported a unanimous finding while the persona section
    reported zero invariant demands — from the same extractions. A report that contradicts
    itself is worse than one that is merely coarse, because a reader has no way to choose.
    """

    async def _three_runs(self, db):
        await _ensemble(db, [{"label": "base", "n": 3, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2},
                         {"run_id": "r3", "cell": "base", "index": 3}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta")
        await _member(db, "ens", "r3", "base", "m3", "gamma")
        # One demand, one refusal and one open question — each worded differently per run.
        return {
            "alpha": {
                "personas": [{"name": "Ada", "final_position": "p",
                              "demands": ["a verified weight before approval"],
                              "refusals": ["will not accept a checkbox"], "concessions": []}],
                "outcome": "shipped", "unresolved": ["who pays for the labwork"],
            },
            "beta": {
                "personas": [{"name": "Ada", "final_position": "p",
                              "demands": ["a confirmed weight is required first"],
                              "refusals": ["refuses to take a checkbox as evidence"],
                              "concessions": []}],
                "outcome": "shipped", "unresolved": ["cost of the labwork is unsettled"],
            },
            "gamma": {
                "personas": [{"name": "Ada", "final_position": "p",
                              "demands": ["weight must be verified before we approve"],
                              "refusals": ["a checkbox is not acceptable to her"],
                              "concessions": []}],
                "outcome": "shipped", "unresolved": ["nobody owns the labwork bill"],
            },
        }

    def _clustering(self, by_marker):
        """Groups each kind into one cluster, numbered over the FULL harvest index space."""

        class WithClusters(FakeModel):
            async def __call__(self, messages, **kwargs):
                prompt = messages[0]["content"]
                if "Group the numbered claims" in prompt:
                    n = sum(1 for line in prompt.splitlines() if line[:1].isdigit())
                    kind = ("demand" if "[demand]" in prompt
                            else "refusal" if "[refusal]" in prompt else "unresolved")
                    return {"content": json.dumps({"clusters": [
                        {"label": f"the {kind}", "members": list(range(n))},
                    ]}), "cost_usd": 0.0, "tokens_in": 1, "tokens_out": 1,
                        "finish_reason": "stop"}
                return await super().__call__(messages, **kwargs)

        return WithClusters(by_marker)

    async def test_the_persona_section_finds_an_invariant_demand(self, db):
        # THE bug: a persona who demanded the same thing in all three runs, in three different
        # sentences, previously looked like one who had changed their mind three times.
        by_marker = await self._three_runs(db)
        report = await ensemble_reporting.build(db, "ens", call=self._clustering(by_marker))

        ada = report["per_persona"]["Ada"]
        assert ada["appears_in_runs"] == 3
        assert [d["claim"] for d in ada["invariant_demands"]] == ["the demand"]
        assert ada["invariant_demands"][0]["runs"] == ["m1", "m2", "m3"]
        assert ada["situational_demands"] == []

    async def test_the_persona_refusals_are_grouped_too(self, db):
        by_marker = await self._three_runs(db)
        report = await ensemble_reporting.build(db, "ens", call=self._clustering(by_marker))

        refusals = report["per_persona"]["Ada"]["refusals"]
        assert [r["claim"] for r in refusals] == ["the refusal"]
        assert refusals[0]["runs"] == ["m1", "m2", "m3"]

    async def test_the_same_open_question_counts_as_one(self, db):
        # The most useful thing this section can say — "all three runs left this open" — was
        # unsayable while every question counted 1.
        by_marker = await self._three_runs(db)
        report = await ensemble_reporting.build(db, "ens", call=self._clustering(by_marker))

        unresolved = report["agreements"]["unresolved_by_frequency"]
        assert len(unresolved) == 1
        assert unresolved[0]["count"] == 3
        assert unresolved[0]["runs"] == ["m1", "m2", "m3"]

    async def test_the_table_and_the_persona_view_do_not_contradict(self, db):
        # Same extractions, same labels: a claim the table calls unanimous must be invariant for
        # the persona who made it.
        by_marker = await self._three_runs(db)
        report = await ensemble_reporting.build(db, "ens", call=self._clustering(by_marker))

        unanimous = {
            c["claim"] for c in report["claims"]
            if c["per_cell"]["base"]["tier"] == "unanimous"
        }
        invariant = {d["claim"] for d in report["per_persona"]["Ada"]["invariant_demands"]}
        assert invariant <= unanimous, (
            f"the persona section claims {invariant - unanimous} is invariant while the table "
            "does not call it unanimous"
        )

    async def test_an_open_question_is_not_in_the_claim_table(self, db):
        # It is clustered in the same pass, but it is not a position anybody held.
        by_marker = await self._three_runs(db)
        report = await ensemble_reporting.build(db, "ens", call=self._clustering(by_marker))

        assert {c["kind"] for c in report["claims"]} == {"demand", "refusal"}
        assert "the unresolved" not in {c["claim"] for c in report["claims"]}

    async def test_a_rejected_clustering_leaves_every_section_on_text_matching(self, db):
        # Half the report on canonical labels and half on text matching would be the
        # contradiction this class exists to prevent, arrived at from the other direction.
        by_marker = await self._three_runs(db)

        class DropsOne(FakeModel):
            async def __call__(self, messages, **kwargs):
                if "Group the numbered claims" in messages[0]["content"]:
                    return {"content": json.dumps({"clusters": [
                        {"label": "partial", "members": [0]},
                    ]}), "cost_usd": 0.0, "tokens_in": 1, "tokens_out": 1}
                return await super().__call__(messages, **kwargs)

        report = await ensemble_reporting.build(db, "ens", call=DropsOne(by_marker))

        assert report["clustered"] is False
        assert report["per_persona"]["Ada"]["invariant_demands"] == []
        assert len(report["claims"]) == 6, "three demands and three refusals, ungrouped"


# --------------------------------------------------------------------------- #
# the invariant denominator
# --------------------------------------------------------------------------- #


class TestInvariantIsJudgedAgainstRunsThatNamedADemand:
    """Silence is not a retraction.

    Measured on the first live 5-replicate ensemble: Jordan was extracted in all five runs and had
    demands recorded in only two, so against a runs-appeared-in denominator no demand of his
    could EVER be invariant — however well the clustering worked. Three attempts at clustering
    recall could not have fixed that, because it is arithmetic, not recall.
    """

    async def _ensemble_where_one_persona_is_often_silent(self, db):
        await _ensemble(db, [{"label": "base", "n": 3, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2},
                         {"run_id": "r3", "cell": "base", "index": 3}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta")
        await _member(db, "ens", "r3", "base", "m3", "gamma")

        def run(dave_demands):
            return {
                "personas": [
                    {"name": "Ada", "final_position": "p", "demands": ["labwork first"],
                     "refusals": [], "concessions": []},
                    # Present in every run, but with demands recorded in only one of them.
                    {"name": "Jordan", "final_position": "p", "demands": dave_demands,
                     "refusals": [], "concessions": []},
                ],
                "outcome": "shipped", "unresolved": [],
            }

        return {
            "alpha": run(["a board case before he moves"]),
            "beta": run([]),
            "gamma": run([]),
        }

    def _identity_clustering(self, by_marker):
        """Groups each kind's claims by exact text, so clustering is not the variable here."""

        class C(FakeModel):
            async def __call__(self, messages, **kwargs):
                prompt = messages[0]["content"]
                if "Group the numbered claims" in prompt:
                    lines = [ln for ln in prompt.splitlines() if ln[:1].isdigit()]
                    by_text = {}
                    for ln in lines:
                        n, _, rest = ln.partition(".")
                        text = rest.split("] ", 1)[-1]
                        by_text.setdefault(text, []).append(int(n))
                    return {"content": json.dumps({"clusters": [
                        {"label": t, "members": m} for t, m in by_text.items()
                    ]}), "cost_usd": 0.0, "tokens_in": 1, "tokens_out": 1,
                        "finish_reason": "stop"}
                return await super().__call__(messages, **kwargs)

        return C(by_marker)

    async def test_a_persona_silent_in_most_runs_can_still_have_an_invariant_demand(self, db):
        by_marker = await self._ensemble_where_one_persona_is_often_silent(db)
        report = await ensemble_reporting.build(
            db, "ens", call=self._identity_clustering(by_marker),
        )

        jordan = report["per_persona"]["Jordan"]
        assert jordan["appears_in_runs"] == 3, "he was in every run"
        assert jordan["demanded_in_runs"] == 1, "but named a demand in only one"
        assert [d["claim"] for d in jordan["invariant_demands"]] == [
            "a board case before he moves"
        ], "his one demand held in every run he made one in"

    async def test_the_denominator_is_reported_so_the_claim_can_be_read(self, db):
        # "Invariant across 1 run" and "invariant across 3" are very different claims, and a
        # reader cannot tell them apart from the word `invariant` alone.
        by_marker = await self._ensemble_where_one_persona_is_often_silent(db)
        report = await ensemble_reporting.build(
            db, "ens", call=self._identity_clustering(by_marker),
        )

        ada = report["per_persona"]["Ada"]
        assert (ada["appears_in_runs"], ada["demanded_in_runs"]) == (3, 3)
        assert [d["claim"] for d in ada["invariant_demands"]] == ["labwork first"]

    async def test_a_demand_missing_from_a_run_that_had_others_is_still_situational(self, db):
        # The distinction the denominator must preserve: naming demand X in run 1 and demand Y in
        # run 2 is a persona whose demands VARY, and neither is invariant. Only silence is
        # excused, not substitution.
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta")
        by_marker = {
            "alpha": _extraction(("Ada", ["demand X"], [])),
            "beta": _extraction(("Ada", ["demand Y"], [])),
        }
        report = await ensemble_reporting.build(
            db, "ens", call=self._identity_clustering(by_marker),
        )

        ada = report["per_persona"]["Ada"]
        assert ada["demanded_in_runs"] == 2
        assert ada["invariant_demands"] == []
        assert {d["claim"] for d in ada["situational_demands"]} == {"demand X", "demand Y"}

    async def test_a_persona_who_never_demanded_anything_has_no_invariant_demands(self, db):
        await _ensemble(db, [{"label": "base", "n": 2, "overrides": {}}],
                        [{"run_id": "r1", "cell": "base", "index": 1},
                         {"run_id": "r2", "cell": "base", "index": 2}])
        await _member(db, "ens", "r1", "base", "m1", "alpha")
        await _member(db, "ens", "r2", "base", "m2", "beta")
        quiet = {
            "personas": [{"name": "Quiet", "final_position": "p", "demands": [],
                          "refusals": ["will not sign"], "concessions": []}],
            "outcome": "o", "unresolved": [],
        }
        report = await ensemble_reporting.build(
            db, "ens", call=self._identity_clustering({"alpha": quiet, "beta": quiet}),
        )

        q = report["per_persona"]["Quiet"]
        assert q["demanded_in_runs"] == 0
        assert q["invariant_demands"] == [], "an empty denominator is not a vacuous truth"
