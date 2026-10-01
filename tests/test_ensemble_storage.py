# SPDX-License-Identifier: Apache-2.0
"""
The ensemble parent row: its key, its isolation, and the report size limit.

Against `moto`, like the rest of the storage layer. The emphasis is on the three things
that are not round-trips:

  * the `sk` prefix, which is what keeps a parent row out of `list_runs` and out of the
    stale-run sweep. A parent has no turns to generate, and a row that looked like a run
    would eventually be handed to the turn loop.
  * `list_ensemble_members` driven by the parent's declaration rather than by scanning runs
    for a back-pointer, so a member that was never created is reported as missing instead
    of silently reducing the denominator.
  * `MAX_REPORT_BYTES`, refused at write time with the size, because the alternative is
    `ValidationException: Item size has exceeded the maximum allowed size`, which names no
    field.
"""

import json

import pytest

from matrix_studio.storage.dynamo import MAX_REPORT_BYTES, _ensemble_sk
from tests.support import TEST_OWNER

pytestmark = pytest.mark.asyncio

OTHER = "sub-other-9999"

SPEC = [{"label": "base", "n": 2, "overrides": {}}]
MEMBERS = [
    {"run_id": "run-1", "cell": "base", "index": 1},
    {"run_id": "run-2", "cell": "base", "index": 2},
]


async def _make(db, ensemble_id="ens-1", **kwargs):
    return await db.create_ensemble(
        ensemble_id=ensemble_id,
        topic=kwargs.pop("topic", "renewal renewal"),
        spec=kwargs.pop("spec", SPEC),
        members=kwargs.pop("members", MEMBERS),
        base_config=kwargs.pop("base_config", {"max_messages": 40}),
        name=kwargs.pop("name", "renewal"),
        **kwargs,
    )


class TestTheParentRow:
    async def test_round_trips_with_its_spec_and_members(self, db):
        await _make(db)
        row = await db.get_ensemble("ens-1")

        assert row["topic"] == "renewal renewal"
        assert row["status"] == "pending"
        assert json.loads(row["spec_json"]) == SPEC
        assert json.loads(row["members_json"]) == MEMBERS
        assert json.loads(row["base_config_json"]) == {"max_messages": 40}
        assert row["report_json"] is None

    async def test_pending_until_a_member_starts(self, db):
        # Matches a run's own first status, so the two read the same way in a UI, and a
        # parent stuck at `pending` is visibly one whose fan-out never happened.
        created = await _make(db)
        assert created["status"] == "pending"

    async def test_creating_the_same_id_twice_is_refused(self, db):
        await _make(db)
        with pytest.raises(Exception):
            await _make(db)

    async def test_missing_is_none_not_an_error(self, db):
        assert await db.get_ensemble("nope") is None


class TestItIsNotARun:
    async def test_the_sort_key_is_outside_the_run_prefix(self):
        # The one-line reason the isolation below holds. Asserted directly so a rename of
        # the prefix cannot quietly put parents back in the run list.
        assert not _ensemble_sk("x").startswith("RUN#")

    async def test_it_does_not_appear_in_list_runs(self, db):
        await _make(db)
        await db.create_run(run_id="r1", topic="t", cast=[], name="a-run")
        assert [r["id"] for r in await db.list_runs()] == ["r1"]

    async def test_it_is_not_swept_as_a_stale_run(self, db):
        # `list_runs_by_status` is the startup sweep's input. A parent row caught by it
        # would be marked interrupted, or worse, resumed.
        await _make(db)
        await db.create_run(run_id="r1", topic="t", cast=[], name="a-run")
        await db.update_run_status("r1", "running")
        found = await db.list_runs_by_status("running")
        assert [r["id"] for r in found] == ["r1"]

    async def test_a_run_and_an_ensemble_may_share_a_name(self, db):
        # An ensemble writes no NAME# marker: uniqueness is among an owner's RUNS, and the
        # ensemble's own name does not live in that space — only its members' derived names
        # do. Rejecting a free ensemble name because an old run held it would be a
        # surprising refusal with no reason a user could act on.
        await db.create_run(run_id="r1", topic="t", cast=[], name="renewal")
        await _make(db, name="renewal")
        assert (await db.get_ensemble("ens-1"))["name"] == "renewal"


class TestMembership:
    async def test_members_come_back_in_declared_order_with_their_runs(self, db):
        await _make(db)
        await db.create_run(
            run_id="run-1", topic="t", cast=[], name="m1",
            ensemble_id="ens-1", ensemble_cell="base",
        )
        await db.create_run(
            run_id="run-2", topic="t", cast=[], name="m2",
            ensemble_id="ens-1", ensemble_cell="base",
        )

        members = await db.list_ensemble_members("ens-1")
        assert [m["run_id"] for m in members] == ["run-1", "run-2"]
        assert [m["index"] for m in members] == [1, 2]
        assert all(m["cell"] == "base" for m in members)
        assert members[0]["run"]["ensemble_cell"] == "base"
        assert members[0]["run"]["ensemble_id"] == "ens-1"

    async def test_a_member_that_was_never_created_is_reported_as_missing(self, db):
        # The denominator has to stay 2. "1 of 1 agreed" from a cell that asked for two
        # runs is the censoring docs/ENSEMBLE-CONVERSATIONS.md §3.4 is about, arrived at
        # through a storage shortcut rather than through impatience.
        await _make(db)
        await db.create_run(
            run_id="run-1", topic="t", cast=[], name="m1",
            ensemble_id="ens-1", ensemble_cell="base",
        )

        members = await db.list_ensemble_members("ens-1")
        assert len(members) == 2
        assert members[1]["run"] is None
        assert members[1]["cell"] == "base"

    async def test_an_unknown_ensemble_has_no_members(self, db):
        assert await db.list_ensemble_members("nope") == []

    async def test_a_standalone_run_carries_no_membership(self, db):
        await db.create_run(run_id="r1", topic="t", cast=[], name="solo")
        run = await db.get_run("r1")
        assert run["ensemble_id"] is None
        assert run["ensemble_cell"] is None


class TestUpdates:
    async def test_status_and_completion(self, db):
        await _make(db)
        await db.update_ensemble("ens-1", status="running")
        assert (await db.get_ensemble("ens-1"))["status"] == "running"

        await db.update_ensemble("ens-1", status="complete", completed_at=1234)
        row = await db.get_ensemble("ens-1")
        assert (row["status"], row["completed_at"]) == ("complete", 1234)

    async def test_the_report_is_stored_with_its_cost_and_a_timestamp(self, db):
        await _make(db)
        await db.update_ensemble(
            "ens-1", report={"held": ["a"]}, report_cost_usd=0.25,
        )
        row = await db.get_ensemble("ens-1")
        assert json.loads(row["report_json"]) == {"held": ["a"]}
        assert row["report_cost_usd"] == 0.25
        assert row["report_generated_at"] > 0

    async def test_an_update_leaves_untouched_fields_alone(self, db):
        # An `UpdateExpression` rather than a whole-item put: the report is written from one
        # request while members may still be flipping the status from another, and a put
        # would have one clobber the other.
        await _make(db)
        await db.update_ensemble("ens-1", report={"held": ["a"]})
        await db.update_ensemble("ens-1", status="complete")

        row = await db.get_ensemble("ens-1")
        assert json.loads(row["report_json"]) == {"held": ["a"]}
        assert row["status"] == "complete"
        assert json.loads(row["spec_json"]) == SPEC

    async def test_updating_nothing_is_a_no_op_not_an_error(self, db):
        await _make(db)
        await db.update_ensemble("ens-1")
        assert (await db.get_ensemble("ens-1"))["status"] == "pending"

    async def test_updating_a_missing_ensemble_is_refused(self, db):
        with pytest.raises(Exception):
            await db.update_ensemble("nope", status="complete")

    async def test_an_oversized_report_is_refused_with_its_size(self, db):
        await _make(db)
        huge = {"blob": "x" * (MAX_REPORT_BYTES + 1000)}
        with pytest.raises(ValueError, match=str(MAX_REPORT_BYTES)):
            await db.update_ensemble("ens-1", report=huge)

        # And nothing was written, so a too-large report does not leave a half-updated row.
        assert (await db.get_ensemble("ens-1"))["report_json"] is None


class TestTenantIsolation:
    async def test_another_owner_cannot_read_it(self, db):
        await _make(db)
        assert await db.get_ensemble("ens-1", owner_sub=OTHER) is None

    async def test_another_owner_sees_none_of_them_listed(self, db):
        await _make(db)
        assert await db.list_ensembles(owner_sub=OTHER) == []
        assert len(await db.list_ensembles(owner_sub=TEST_OWNER)) == 1

    async def test_another_owner_gets_no_members(self, db):
        # `list_ensemble_members` resolves the parent in the caller's partition first, so a
        # cross-tenant read cannot reach the member run ids either.
        await _make(db)
        assert await db.list_ensemble_members("ens-1", owner_sub=OTHER) == []


class TestListing:
    async def test_newest_first(self, db):
        import time as _time

        await _make(db, ensemble_id="old")
        _time.sleep(1.1)  # created_at is whole seconds
        await _make(db, ensemble_id="new", name="second")

        assert [e["id"] for e in await db.list_ensembles()] == ["new", "old"]

    async def test_the_limit_applies(self, db):
        for i in range(4):
            await _make(db, ensemble_id=f"e{i}", name=f"n{i}")
        assert len(await db.list_ensembles(limit=2)) == 2


class TestMembersCarryTheirDerivedStats:
    """`get_run` returns the ROW; turn count and cost are derived from the event log.

    Reported from the UI: every member read "complete · 0 turns · $0.000". The status was right,
    which is what made it read as a broken view rather than as a missing join — `list_runs` calls
    `get_run_stats` for exactly this reason and `list_ensemble_members` did not.
    """

    async def test_turn_count_and_cost_are_present(self, db):
        await _make(db)
        await db.create_run(
            run_id="run-1", topic="t", cast=[], name="m1",
            ensemble_id="ens-1", ensemble_cell="base",
        )
        for seq in range(3):
            await db.append_event(
                run_id="run-1", turn=seq + 1, seq=seq, event_type="agent.response",
                agent_name="Ada", payload={"message": "x", "cost_usd": 0.25},
            )
        await db.update_run_status("run-1", "complete")

        members = await db.list_ensemble_members("ens-1")
        run = members[0]["run"]
        assert run["status"] == "complete"
        assert run["turn_count"] == 3
        assert run["total_cost_usd"] == pytest.approx(0.75)

    async def test_a_member_with_no_events_reports_zero_rather_than_missing(self, db):
        # Zero is the honest answer for a run that has not started; the bug was zero for a run
        # that had finished forty turns.
        await _make(db)
        await db.create_run(
            run_id="run-1", topic="t", cast=[], name="m1",
            ensemble_id="ens-1", ensemble_cell="base",
        )
        members = await db.list_ensemble_members("ens-1")
        assert members[0]["run"]["turn_count"] == 0

    async def test_a_missing_member_is_still_none(self, db):
        # The stats join must not resurrect a member that was never created.
        await _make(db)
        members = await db.list_ensemble_members("ens-1")
        assert all(m["run"] is None for m in members)


class TestResearchProvenanceOnDocuments:
    """`origin`, `authority` and `research_batch` — docs/PERSONA-RESEARCH.md §5.1.

    These exist because research ingested into the collection ALREADY BOUND at a scope until
    2026-10-01, and collections written then hold both kinds. Without them a curated collection and
    a researched one are indistinguishable, and two things become impossible: telling a reader what
    they are looking at, and undoing a research pass without rebuilding somebody's hand-assembled
    work. They are also how the setup route tells a research collection from a curated one.
    """

    async def test_only_a_collection_made_FOR_research_carries_the_marker(self, db):
        # The Research state writes only where `research_for` names its own pass, so a
        # collection a person made must never carry one — absent, as every older row reads.
        mine = await db.create_knowledge_base(name="hand made", description="d")
        found = await db.create_knowledge_base(name="Research — r · shared",
                                               research_for="run:r")
        assert (await db.get_knowledge_base(mine["id"]))["research_for"] is None
        assert (await db.get_knowledge_base(found["id"]))["research_for"] == "run:r"

    async def _kb(self, db):
        return (await db.create_knowledge_base(name="provider law", description="d"))["id"]

    async def test_an_ordinary_upload_carries_no_research_fields(self, db):
        # An upload must not look like research. Absent, not empty-string: every document written
        # before these fields existed reads the same way.
        kb_id = await self._kb(db)
        await db.add_kb_document(kb_id, title="hand written", text="x" * 300)
        doc = (await db.list_kb_documents(kb_id))[0]
        assert doc["origin"] is None
        assert doc["authority"] is None
        assert doc["research_batch"] is None

    async def test_a_researched_document_records_all_three(self, db):
        kb_id = await self._kb(db)
        await db.add_kb_document(
            kb_id, title="ARS 32-2201", text="x" * 300,
            source_path="https://azleg.gov/ars/32/02201.htm",
            origin="researched", authority="controlling", research_batch="b1",
        )
        doc = (await db.list_kb_documents(kb_id))[0]
        assert doc["origin"] == "researched"
        assert doc["authority"] == "controlling"
        assert doc["research_batch"] == "b1"
        assert doc["source_path"] == "https://azleg.gov/ars/32/02201.htm"


class TestAReRunReplacesRatherThanAccumulates:
    async def _seeded(self, db):
        kb_id = (await db.create_knowledge_base(name="provider law", description="d"))["id"]
        await db.add_kb_document(kb_id, title="curated by hand", text="c" * 300)
        await db.add_kb_document(kb_id, title="old research", text="o" * 300,
                                 origin="researched", authority="commentary",
                                 research_batch="batch-1")
        await db.add_kb_document(kb_id, title="new research", text="n" * 300,
                                 origin="researched", authority="controlling",
                                 research_batch="batch-2")
        return kb_id

    async def test_it_removes_only_earlier_researched_documents(self, db):
        kb_id = await self._seeded(db)
        removed = await db.replace_research_documents(kb_id, batch="batch-2", keep="batch-2")

        assert len(removed) == 1
        titles = {d["title"] for d in await db.list_kb_documents(kb_id)}
        assert titles == {"curated by hand", "new research"}

    async def test_curated_documents_are_never_touched(self, db):
        # THE property. An operator who assembled a collection by hand and then enabled research
        # must be able to undo the research without rebuilding their own work.
        kb_id = await self._seeded(db)
        await db.replace_research_documents(kb_id, batch="batch-3")
        titles = {d["title"] for d in await db.list_kb_documents(kb_id)}
        assert "curated by hand" in titles

    async def test_with_nothing_earlier_it_removes_nothing(self, db):
        kb_id = (await db.create_knowledge_base(name="k", description="d"))["id"]
        await db.add_kb_document(kb_id, title="only research", text="x" * 300,
                                 origin="researched", research_batch="batch-1")
        assert await db.replace_research_documents(kb_id, batch="batch-1") == []

    async def test_the_batch_is_the_key_not_the_url(self, db):
        # The same page can legitimately be re-fetched with different content, and the newer fetch
        # should win. A URL-keyed dedupe would keep whichever arrived first.
        kb_id = (await db.create_knowledge_base(name="k", description="d"))["id"]
        url = "https://azleg.gov/ars/32/02201.htm"
        await db.add_kb_document(kb_id, title="old text", text="o" * 300, source_path=url,
                                 origin="researched", research_batch="batch-1")
        await db.add_kb_document(kb_id, title="newer text", text="n" * 300, source_path=url,
                                 origin="researched", research_batch="batch-2")

        await db.replace_research_documents(kb_id, batch="batch-2", keep="batch-2")
        docs = await db.list_kb_documents(kb_id)
        assert [d["title"] for d in docs] == ["newer text"]
