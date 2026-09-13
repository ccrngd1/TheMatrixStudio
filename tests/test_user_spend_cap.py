# SPDX-License-Identifier: Apache-2.0
"""
Per-user monthly spend caps — §7's rollout prerequisite.

"A company will require this before rollout", because a per-run cap bounds one
conversation and nothing bounds a user starting fifty of them.

Four properties, and the last two are where a cost control actually fails:

  1. **Refuse to START** (§7: "cheaper and clearer than stopping one mid-way"), and
     check again between turns so a long or resumed run cannot outlive its cap.
  2. **One decision point.** `over_monthly_cap` is called from both, like `_slice_filter`
     and `may_read_kb`. Two call sites that each compute "is this user over budget" will
     eventually compute it differently.
  3. **The DELTA is charged, not the total.** The slice receives the run's cumulative
     cost and charges the difference. Charging the total would bill a 30-turn run ~465×
     what it cost — and the run would still complete, so nothing would look wrong until
     the cap refused everything.
  4. **The counter is atomic.** Two of a user's runs generating concurrently must both
     count; a read-modify-write loses one, which is the entire reason for `ADD`.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from matrix_studio import orchestration
from matrix_studio.settings import Settings
from tests.support import TEST_OWNER

pytestmark = pytest.mark.asyncio

OTHER = "sub-other-spender-5555"


# --------------------------------------------------------------------------- #
# The counter
# --------------------------------------------------------------------------- #


class TestTheCounter:
    async def test_an_unrecorded_month_reads_as_zero(self, db):
        """A user's first run of the month has no item yet, which is the common case
        rather than an error."""
        assert await db.user_spend() == 0.0

    async def test_spend_accumulates(self, db):
        await db.add_user_spend(0.25)
        await db.add_user_spend(0.10)
        assert await db.user_spend() == pytest.approx(0.35)

    async def test_concurrent_increments_do_not_lose_any(self, db):
        """`ADD` is atomic; a read-modify-write would lose all but one.

        This is the property the counter exists for: a user's two runs generate at the
        same time in two Lambdas, and both slices charge their own turn.

        Worth knowing what this does and does not catch, because a first attempt at
        mutating it was wrong. `SET cost_usd = if_not_exists(cost_usd, 0) + :c` is ALSO
        atomic — one UpdateExpression, evaluated server-side — so swapping `ADD` for it is
        an equivalent implementation and this test correctly ignores it. What it does catch
        is a genuine read-modify-write: a `GetItem`, an addition in Python, a `PutItem`.
        That mutant loses 19 of 20 increments and fails here.
        """
        await asyncio.gather(*(db.add_user_spend(0.01) for _ in range(20)))
        assert await db.user_spend() == pytest.approx(0.20)

    async def test_one_users_spend_is_not_anothers(self, db):
        await db.add_user_spend(1.0)
        await db.add_user_spend(5.0, owner_sub=OTHER)
        assert await db.user_spend() == pytest.approx(1.0)
        assert await db.user_spend(owner_sub=OTHER) == pytest.approx(5.0)

    async def test_months_are_separate_and_keyed_in_UTC(self, db):
        """A local-time boundary would double-count or skip depending on the offset."""
        await db.add_user_spend(3.0, month="2026-01")
        await db.add_user_spend(4.0, month="2026-02")
        assert await db.user_spend(month="2026-01") == pytest.approx(3.0)
        assert await db.user_spend(month="2026-02") == pytest.approx(4.0)
        # The default month is this one, in UTC.
        assert db._spend_sk() == f"SPEND#{datetime.now(timezone.utc):%Y-%m}"

    async def test_a_zero_amount_is_a_no_op_not_an_error(self, db):
        """A slice that generated nothing — a stop landing between turns, a retry after a
        completed slice — has no spend to record."""
        await db.add_user_spend(1.0)
        assert await db.add_user_spend(0.0) == pytest.approx(1.0)
        assert await db.add_user_spend(-5.0) == pytest.approx(1.0)

    async def test_it_lives_under_the_users_own_partition(self, db):
        """So `dynamodb:LeadingKeys` covers it with no policy change — §3's boundary
        reaches it for free, which is the reason for the key."""
        await db.add_user_spend(1.0)
        got = await db._call(
            db._table("runs").get_item,
            Key={"pk": f"USER#{TEST_OWNER}", "sk": db._spend_sk()},
        )
        assert got.get("Item"), "the spend item is not in the user's partition"


# --------------------------------------------------------------------------- #
# The decision
# --------------------------------------------------------------------------- #


class TestTheDecision:
    async def test_no_cap_configured_means_no_read_at_all(self, db, monkeypatch):
        """A disabled cap must cost nothing, which is what makes it safe to default off."""
        reads = {"n": 0}
        real = db.user_spend

        async def counting(**kwargs):
            reads["n"] += 1
            return await real(**kwargs)

        monkeypatch.setattr(db, "user_spend", counting)
        _settings(monkeypatch, cap=0.0)
        assert await orchestration.over_monthly_cap(db, TEST_OWNER) is None
        assert reads["n"] == 0

    async def test_under_the_cap_is_allowed(self, db, monkeypatch):
        _settings(monkeypatch, cap=10.0)
        await db.add_user_spend(9.99)
        assert await orchestration.over_monthly_cap(db, TEST_OWNER) is None

    async def test_at_the_cap_is_refused(self, db, monkeypatch):
        """`>=`, not `>`: a cap of $10 means ten dollars is the limit, not the first
        cent past it."""
        _settings(monkeypatch, cap=10.0)
        await db.add_user_spend(10.0)
        over = await orchestration.over_monthly_cap(db, TEST_OWNER)
        assert over and over["spent"] == pytest.approx(10.0) and over["cap"] == 10.0

    async def test_a_failed_read_FAILS_CLOSED(self, db, monkeypatch):
        """Refusing rather than allowing an unmetered run.

        The read is a GetItem on the user's own partition with the same credentials every
        other read uses, so a failure here means the run's own reads are failing too —
        failing closed adds no outage mode, while failing open lets a cap silently stop
        applying, which is the failure that costs money.
        """
        _settings(monkeypatch, cap=10.0)

        async def boom(**kwargs):
            raise RuntimeError("DynamoDB is having a day")

        monkeypatch.setattr(db, "user_spend", boom)
        over = await orchestration.over_monthly_cap(db, TEST_OWNER)
        assert over is not None, "a failed spend read allowed an unmetered run"
        assert over["spent"] is None
        assert "day" in over["error"]

    async def test_a_group_cap_overrides_the_default(self, db, monkeypatch):
        _settings(monkeypatch, cap=10.0, table='{"trial": 1}')
        await db.add_user_spend(2.0)
        assert await orchestration.over_monthly_cap(db, TEST_OWNER, ["trial"]) is not None
        # The same spend is fine for a user in no group, under the $10 default.
        assert await orchestration.over_monthly_cap(db, TEST_OWNER, []) is None

    async def test_the_highest_group_cap_wins(self, db, monkeypatch):
        """Being added to a more generous tier must not leave someone held to a lower one
        they also belong to. Lowest-wins would make group membership subtractive."""
        _settings(monkeypatch, cap=10.0, table='{"trial": 1, "staff": 100}')
        await db.add_user_spend(50.0)
        assert (
            await orchestration.over_monthly_cap(db, TEST_OWNER, ["trial", "staff"])
            is None
        )


# --------------------------------------------------------------------------- #
# Recording a slice
# --------------------------------------------------------------------------- #


class TestRecordingASlice:
    async def test_the_DELTA_is_charged_not_the_total(self, db):
        """Charging the total would bill a 30-turn run ~465× its cost — and the run would
        still complete, so nothing looks wrong until the cap refuses everything."""
        await orchestration.record_spend(db, TEST_OWNER, before=0.0, after=0.10)
        await orchestration.record_spend(db, TEST_OWNER, before=0.10, after=0.25)
        await orchestration.record_spend(db, TEST_OWNER, before=0.25, after=0.30)
        # The run cost 0.30 in total; that is what the user is charged.
        assert await db.user_spend() == pytest.approx(0.30)

    async def test_a_slice_that_cost_nothing_charges_nothing(self, db):
        await orchestration.record_spend(db, TEST_OWNER, before=0.5, after=0.5)
        assert await db.user_spend() == 0.0

    async def test_a_negative_delta_never_credits(self, db):
        """A cumulative total that went DOWN is a bug somewhere upstream, and refunding
        against it would let that bug erase real spend."""
        await db.add_user_spend(1.0)
        await orchestration.record_spend(db, TEST_OWNER, before=5.0, after=1.0)
        assert await db.user_spend() == pytest.approx(1.0)

    async def test_a_failure_to_record_does_not_raise(self, db, monkeypatch):
        """A run must not fail because its accounting did. The cap's job is to refuse the
        NEXT thing, and a missed increment delays that rather than breaking this."""
        async def boom(*args, **kwargs):
            raise RuntimeError("throttled")

        monkeypatch.setattr(db, "add_user_spend", boom)
        await orchestration.record_spend(db, TEST_OWNER, before=0.0, after=1.0)


# --------------------------------------------------------------------------- #
# Groups on the run row, which the turn loop has no token for
# --------------------------------------------------------------------------- #


class TestGroupsFromTheRun:
    def test_groups_are_read_from_the_run_row(self):
        import json

        assert orchestration.groups_of({"groups_json": json.dumps(["a", "b"])}) == ["a", "b"]

    def test_malformed_groups_read_as_none(self):
        """Fail closed on the CAP side: no groups means the flat default applies, which is
        the stricter of the two when a group has a higher cap."""
        assert orchestration.groups_of({"groups_json": "not json"}) == []
        assert orchestration.groups_of({"groups_json": '{"not": "a list"}'}) == []
        assert orchestration.groups_of({}) == []
        assert orchestration.groups_of({"groups_json": '[null, "", "  ", "real"]'}) == ["real"]


def _settings(monkeypatch, *, cap: float, table: str = ""):
    """Force a settings singleton with the cap under test."""
    forced = Settings(max_user_monthly_cost_usd=cap, user_spend_caps_json=table)
    monkeypatch.setattr("matrix_studio.settings.get_settings", lambda: forced)


# --------------------------------------------------------------------------- #
# Enforcement at the two points
# --------------------------------------------------------------------------- #


class TestRunCreationIsRefused:
    """§7: refuse to START, "cheaper and clearer than stopping one mid-way"."""

    @pytest.fixture
    def client(self, aws_backend, monkeypatch):
        from fastapi.testclient import TestClient

        from matrix_studio.api import identity
        from matrix_studio.api.app import create_app

        monkeypatch.setenv("AUTH_MODE", "single-user")
        monkeypatch.setattr(identity, "LOCAL_USER_SUB", TEST_OWNER, raising=False)

        async def fake_name(topic, cast_names=None, model=None, name_exists=None):
            return {"name": "capped-run", "description": "d", "slug": "capped-run",
                    "source": "llm"}

        monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
        with TestClient(create_app()) as c:
            yield c

    BODY = {
        "topic": "Does the cap hold?",
        "cast": [{"name": "Ada", "persona": "an engineer", "goals": ["g"]}],
        "config": {"max_messages": 1},
    }

    async def test_over_the_cap_is_402_with_the_numbers(self, client, db, monkeypatch):
        """402, not 403: the caller MAY do this and will be able to again next month, so
        it is a budget answer rather than a permissions one. And 429 would invite a
        retry in a second."""
        _settings(monkeypatch, cap=5.0)
        await db.add_user_spend(6.0)

        response = client.post("/api/runs", json=dict(self.BODY, name="over"))
        assert response.status_code == 402, response.text
        detail = response.json()["detail"]
        assert "6.00" in detail and "5.00" in detail
        assert "next month" in detail

    async def test_under_the_cap_starts_normally(self, client, db, monkeypatch):
        """The positive half, so the refusal above is not passing vacuously."""
        _settings(monkeypatch, cap=5.0)
        await db.add_user_spend(1.0)
        response = client.post("/api/runs", json=dict(self.BODY, name="under"))
        assert response.status_code == 201, response.text

    async def test_a_failed_spend_read_refuses_and_says_why(self, client, db, monkeypatch):
        _settings(monkeypatch, cap=5.0)
        from matrix_studio.storage.dynamo import DynamoStorage

        async def boom(self, **kwargs):
            raise RuntimeError("throttled")

        monkeypatch.setattr(DynamoStorage, "user_spend", boom)
        response = client.post("/api/runs", json=dict(self.BODY, name="unreadable"))
        assert response.status_code == 402
        assert "unmetered" in response.json()["detail"]

    async def test_the_feature_off_costs_no_read(self, client, db, monkeypatch):
        """Default off, so a single-user install is unaffected."""
        _settings(monkeypatch, cap=0.0)
        await db.add_user_spend(1000.0)
        response = client.post("/api/runs", json=dict(self.BODY, name="uncapped"))
        assert response.status_code == 201, response.text


class TestTheTurnLoopCaps:
    """A long or resumed run cannot outlive its cap.

    The turn already generated is kept — the same guarantee the stop check gives, and for
    the same reason: it was paid for and persisted, and throwing it away wastes the money
    the cap exists to save.
    """

    async def test_a_run_is_capped_between_turns(self, db, monkeypatch):
        from matrix_studio.state import SimSnapshot

        _settings(monkeypatch, cap=1.0)
        await db.create_run(run_id="r-cap", topic="t", cast=[{"name": "Ada"}],
                            name="r-cap", config={"max_messages": 10})
        await db.save_snapshot(
            SimSnapshot(run_id="r-cap", turn=3, topic="t", agents={}, conversation=[],
                        status="running", total_turns=3, created_at=1)
        )
        await db.add_user_spend(2.0)

        out = await orchestration.cap_now(
            db, "r-cap", turn=3,
            reason={"spent": 2.0, "cap": 1.0, "groups": []},
        )
        assert out["status"] == "capped"
        assert out["done"] is True

        run = await db.get_run("r-cap")
        assert run["status"] == "capped"
        # The turn that was already generated is still there.
        assert (await db.last_checkpoint_turn("r-cap")) == 3

    async def test_the_capped_event_says_WHICH_cap(self, db, monkeypatch):
        """Otherwise an operator sees `capped` and goes looking for a per-run limit that
        was never reached."""
        from matrix_studio.state import SimSnapshot

        await db.create_run(run_id="r-cap2", topic="t", cast=[{"name": "Ada"}],
                            name="r-cap2", config={"max_messages": 10})
        await db.save_snapshot(
            SimSnapshot(run_id="r-cap2", turn=1, topic="t", agents={}, conversation=[],
                        status="running", total_turns=1, created_at=1)
        )
        await orchestration.cap_now(
            db, "r-cap2", turn=1, reason={"spent": 12.5, "cap": 10.0, "groups": ["trial"]},
        )
        events = await db.get_events("r-cap2")
        capped = [e for e in events if e["event_type"] == "sim.capped"]
        assert len(capped) == 1
        import json

        payload = json.loads(capped[0]["payload"]) if isinstance(
            capped[0]["payload"], str
        ) else capped[0]["payload"]
        assert payload["scope"] == "user-monthly"
        assert payload["cap_usd"] == 10.0
        assert payload["user_spend_usd"] == 12.5

    async def test_capped_uses_the_SAME_terminal_status_as_the_per_run_cap(self):
        """A third status would need handling in the frontend's terminal list, the resume
        rules, the summary skip and every export — and the two are the same fact to a
        reader: the run stopped because of money."""
        assert "capped" in orchestration.TERMINAL_STATUSES
