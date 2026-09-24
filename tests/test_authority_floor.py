# SPDX-License-Identifier: Apache-2.0
"""
Reserving a prompt slot for controlling authority.

`docs/PERSONA-RESEARCH.md` §3. The feature's whole point is that personas state what would change
their mind — "a state statute defining specialty plans as regulated-only" — and across five
replicate runs none ever got it. If research finds that statute and it then loses every slot to a
law-firm article *about* it, the feature found the answer and hid it. That is worse than not
searching, because the corpus would show a controlling authority no turn ever saw.

**Why the existing source floor does not cover this.** That one reserves per COLLECTION. Research
ingests into the collection already bound at a scope, so the statute and thirty commentary chunks are
in the SAME collection competing for the SAME reserved slot — and the one that wins is whichever
matches a query drawn from conversation text, which is the commentary, because commentary is written
in the conversation's vocabulary and a statute is not. The measured shape of the `2d2ac45b` bug,
reappearing one level down.
"""

import pytest

from matrix_studio.storage.vectors import (
    apply_floors,
    merge_with_authority_floor,
    merge_with_source_floor,
)


def row(score, authority=None, kb="kb1", label=None):
    r = {"score": score, "kb_id": kb, "label": label or f"{authority or 'plain'}@{score}"}
    if authority:
        r["authority"] = authority
    return r


def labels(rows):
    return [r["label"] for r in rows]


class TestTheStatuteSurvives:
    def test_a_controlling_row_is_kept_even_when_outranked(self):
        # THE case. Three commentary chunks rank better than the statute, and at k=3 a plain top-k
        # takes all three.
        rows = [
            row(0.10, "commentary", label="blog-a"),
            row(0.11, "commentary", label="blog-b"),
            row(0.12, "commentary", label="blog-c"),
            row(0.40, "controlling", label="STATUTE"),
        ]
        assert "STATUTE" not in labels(sorted(rows, key=lambda r: r["score"])[:3])
        assert "STATUTE" in labels(merge_with_authority_floor(rows, 3))

    def test_it_takes_exactly_one_slot_by_default(self):
        rows = [row(0.1 + i / 100, "commentary", label=f"c{i}") for i in range(5)]
        rows += [row(0.9, "controlling", label="S1"), row(0.95, "controlling", label="S2")]
        got = labels(merge_with_authority_floor(rows, 3))
        assert got.count("S1") + got.count("S2") == 1, "one reserved slot, not all of them"
        assert len(got) == 3

    def test_a_higher_floor_reserves_more(self):
        rows = [row(0.1 + i / 100, "commentary", label=f"c{i}") for i in range(5)]
        rows += [row(0.9, "controlling", label="S1"), row(0.95, "controlling", label="S2")]
        got = labels(merge_with_authority_floor(rows, 4, floor=2))
        assert "S1" in got and "S2" in got

    def test_the_best_controlling_row_is_the_reserved_one(self):
        rows = [
            row(0.5, "controlling", label="statute-far"),
            row(0.2, "controlling", label="statute-near"),
            row(0.1, "commentary", label="blog"),
        ]
        got = labels(merge_with_authority_floor(rows, 2))
        assert "statute-near" in got and "statute-far" not in got

    def test_a_controlling_row_that_already_wins_costs_nothing(self):
        # No double-counting: it must not take the reserved slot AND a ranked slot.
        rows = [row(0.1, "controlling", label="S"), row(0.2, "commentary", label="c1"),
                row(0.3, "commentary", label="c2")]
        got = labels(merge_with_authority_floor(rows, 2))
        assert got == ["S", "c1"]


class TestItChangesNothingWithoutResearch:
    def test_rows_with_no_authority_behave_exactly_as_top_k(self):
        # Enabling this must not alter a run that has no researched documents — every passage written
        # before the field existed, and every ordinary upload, has no `authority`.
        rows = [row(0.3, label="a"), row(0.1, label="b"), row(0.2, label="c")]
        assert labels(merge_with_authority_floor(rows, 2)) == ["b", "c"]

    def test_floor_zero_is_plain_top_k(self):
        rows = [row(0.9, "controlling", label="S"), row(0.1, "commentary", label="c")]
        assert labels(merge_with_authority_floor(rows, 1, floor=0)) == ["c"]

    def test_an_unknown_tier_is_not_reserved(self):
        # The documented negative is tiered `unknown` deliberately: it is a record of a search, not a
        # source about the law, and it must not take the slot meant for an authority.
        rows = [row(0.9, "unknown", label="NEGATIVE"), row(0.1, "commentary", label="c")]
        assert labels(merge_with_authority_floor(rows, 1)) == ["c"]

    def test_persuasive_is_not_reserved(self):
        # Only `controlling` gets a floor. A model act is worth having and is not binding, so it
        # competes on rank.
        rows = [row(0.9, "persuasive", label="MODEL_ACT"), row(0.1, "commentary", label="c")]
        assert labels(merge_with_authority_floor(rows, 1)) == ["c"]


class TestEdges:
    @pytest.mark.parametrize("k", [0, -1])
    def test_no_slots_returns_nothing(self, k):
        assert merge_with_authority_floor([row(0.1, "controlling")], k) == []

    def test_no_rows_returns_nothing(self):
        assert merge_with_authority_floor([], 3) == []

    def test_it_never_returns_more_than_k(self):
        rows = [row(0.1 + i / 100, "controlling", label=f"s{i}") for i in range(9)]
        assert len(merge_with_authority_floor(rows, 3, floor=5)) == 3

    def test_the_result_is_in_distance_order(self):
        rows = [row(0.5, "controlling", label="S"), row(0.1, "commentary", label="c")]
        got = merge_with_authority_floor(rows, 2)
        assert [r["score"] for r in got] == sorted(r["score"] for r in got)

    def test_it_composes_with_the_source_floor(self):
        # They answer different questions and both are wanted: every collection contributes, AND a
        # controlling authority keeps a slot. Applied in sequence, the authority floor last, so it
        # sees what the source floor left.
        rows = [
            row(0.10, "commentary", kb="shared", label="shared-blog"),
            row(0.11, "commentary", kb="shared", label="shared-blog-2"),
            row(0.30, None, kb="casey", label="casey-own"),
            row(0.40, "controlling", kb="shared", label="STATUTE"),
        ]
        after_source = merge_with_source_floor(rows, 3, key="kb_id", floor=1)
        assert "casey-own" in labels(after_source), "the source floor did its job"
        # And the statute is still missing, which is the point of this class.
        assert "STATUTE" not in labels(after_source)
        assert "STATUTE" in labels(merge_with_authority_floor(rows, 3))


class TestApplyFloorsComposesThem:
    """The one place the two floors' interaction is decided, and where a wrong choice reintroduces a
    bug the other floor exists to fix."""

    def _rows(self):
        return [
            row(0.10, "commentary", kb="shared", label="shared-blog"),
            row(0.11, "commentary", kb="shared", label="shared-blog-2"),
            row(0.30, None, kb="casey", label="casey-own"),
            row(0.40, "controlling", kb="shared", label="STATUTE"),
        ]

    def test_off_by_default_it_is_exactly_the_source_floor(self):
        # A run with no researched documents must be unaffected. This is the assertion that makes the
        # feature safe to ship dark.
        rows = self._rows()
        assert labels(apply_floors(rows, 3)) == labels(
            merge_with_source_floor(rows, 3, key="kb_id", floor=1)
        )

    def test_with_the_floor_on_both_properties_hold(self):
        got = labels(apply_floors(self._rows(), 3, authority_floor=1))
        assert "STATUTE" in got, "the controlling authority reached the prompt"
        assert "casey-own" in got, "and the collection that could never win a slot still contributes"
        assert len(got) == 3

    def test_the_worst_ranked_row_pays_for_the_slot(self):
        # It costs one slot, spent on the row global rank valued least.
        got = labels(apply_floors(self._rows(), 3, authority_floor=1))
        assert "shared-blog" in got, "the best commentary is kept"
        assert "shared-blog-2" not in got, "the worst-ranked selected row paid"

    def test_nothing_is_bought_when_a_statute_already_won(self):
        rows = [
            row(0.10, "controlling", kb="shared", label="STATUTE"),
            row(0.20, "commentary", kb="shared", label="blog"),
            row(0.30, None, kb="casey", label="casey-own"),
        ]
        got = labels(apply_floors(rows, 3, authority_floor=1))
        assert sorted(got) == sorted(["STATUTE", "blog", "casey-own"])

    def test_a_controlling_row_is_never_the_one_dropped(self):
        rows = [
            row(0.10, "controlling", kb="a", label="S1"),
            row(0.20, "commentary", kb="a", label="c"),
            row(0.90, "controlling", kb="b", label="S2"),
        ]
        got = labels(apply_floors(rows, 2, authority_floor=1))
        assert "S1" in got, "the statute already selected was not sacrificed for another"

    def test_no_controlling_row_anywhere_changes_nothing(self):
        # Nothing controlling survived the similarity threshold, which is the CORRECT outcome: an
        # irrelevant statute in every prompt would read as the room ignoring the law.
        rows = [row(0.1, "commentary", label="a"), row(0.2, "commentary", label="b")]
        assert labels(apply_floors(rows, 1, authority_floor=1)) == ["a"]

    def test_it_never_returns_more_than_k(self):
        rows = self._rows() + [row(0.5, "controlling", kb="x", label="S2")]
        assert len(apply_floors(rows, 2, authority_floor=2)) == 2

    def test_the_result_stays_in_distance_order(self):
        got = apply_floors(self._rows(), 3, authority_floor=1)
        assert [r["score"] for r in got] == sorted(r["score"] for r in got)

    def test_an_empty_selection_is_left_alone(self):
        assert apply_floors([], 3, authority_floor=1) == []


class TestOnlySurplusPaysForTheSlot:
    """The error an earlier version made, and the reason `apply_floors` exists as one function.

    Dropping the worst-RANKED row reads as fair and is not: the worst-ranked row is usually the one
    the source floor RESERVED, because a collection wins its slot on its own best passage rather than
    on global rank. On the measured shape it evicted a persona's only passage to make room for a
    statute from the shared collection — trading an invisible failure for a visible one, in the wrong
    direction.
    """

    def test_a_collection_s_only_passage_is_never_evicted(self):
        rows = [
            row(0.10, "commentary", kb="shared", label="shared-blog"),
            row(0.11, "commentary", kb="shared", label="shared-blog-2"),
            row(0.30, None, kb="casey", label="casey-own"),
            row(0.40, "controlling", kb="shared", label="STATUTE"),
        ]
        got = labels(apply_floors(rows, 3, authority_floor=1))
        assert "casey-own" in got, "the reserved passage survived"
        assert "shared-blog-2" not in got, "the surplus passage paid"
        assert "STATUTE" in got

    def test_with_no_surplus_the_statute_does_not_get_a_slot(self):
        # Every slot is somebody's only contribution, so none can be bought. A collection
        # contributing nothing is invisible; a missing authority is visible in the corpus.
        rows = [
            row(0.10, None, kb="a", label="a-only"),
            row(0.20, None, kb="b", label="b-only"),
            row(0.90, "controlling", kb="c", label="STATUTE"),
        ]
        got = labels(apply_floors(rows, 2, authority_floor=1))
        assert got == ["a-only", "b-only"]
        assert "STATUTE" not in got

    def test_the_surplus_that_pays_is_the_worst_of_the_surplus(self):
        rows = [
            row(0.10, "commentary", kb="shared", label="s1"),
            row(0.20, "commentary", kb="shared", label="s2"),
            row(0.30, "commentary", kb="shared", label="s3"),
            row(0.90, "controlling", kb="other", label="STATUTE"),
        ]
        got = labels(apply_floors(rows, 3, authority_floor=1))
        assert "s1" in got and "s2" in got and "s3" not in got


class TestTheShapeMeasuredInRun602ddffe:
    """The live shape that produced a false alarm, pinned so it is not re-litigated.

    Run `602ddffe`'s A/B looked like the floor had paid for a promoted statute with a persona's
    own RUN-SCOPED passage — which `apply_floors` must never do, since a run-scoped row carries no
    `kb_id` and so can never be surplus. It had not: the row that paid carried a `kb_id`, and that
    collection had two passages selected, so its second-best was genuinely surplus.

    The error was in the inspection script, which labelled any `document_id` it could not find in
    its KB listing as "run-scoped" — a fallback that guesses, reported as a measurement. Three
    rounds of reading the code to predict the selection produced three wrong answers; one trace of
    the real calls settled it.

    Kept because the two shapes are one row apart and only one of them is a bug.
    """

    #: Dr. Jordan's pool: his collection (commentary best, controlling at rank 6), the shared
    #: collection's three chunks of one document, and one row with no collection at all.
    def _pool(self, *, lone_row_has_kb):
        return [
            row(0.5378, "commentary", kb="jordan", label="jordan-commentary"),
            # THE row in question. With a `kb_id` it is Jordan's second passage and may pay;
            # without one it is his only unreserved contribution and may not.
            row(0.5422, None, kb="jordan" if lone_row_has_kb else None, label="the-payer"),
            row(0.5709, "persuasive", kb="shared", label="shared-a"),
            row(0.5712, "persuasive", kb="shared", label="shared-b"),
            row(0.5779, "persuasive", kb="shared", label="shared-c"),
            row(0.5784, "controlling", kb="jordan", label="statute"),
        ]

    def test_a_second_passage_from_the_same_collection_pays(self):
        # What actually happened. Jordan's collection gives up its second-best row and gains a
        # statute — from that same collection — so it contributes two passages either way.
        got = apply_floors(
            self._pool(lone_row_has_kb=True), 3,
            kb_floor=1, authority_floor=1, prefer=["jordan"],
        )
        assert "statute" in labels(got)
        assert "the-payer" not in labels(got)
        assert labels(got) == ["jordan-commentary", "shared-a", "statute"]

    def test_a_row_with_no_collection_is_never_the_one_that_pays(self):
        # The bug the false alarm described. A row with no `kb_id` is not a collection's
        # reservation, but it is also not surplus — `per_kb` cannot see it as one of several —
        # so it must survive, and with nothing droppable the statute simply gets no slot.
        got = apply_floors(
            self._pool(lone_row_has_kb=False), 3,
            kb_floor=1, authority_floor=1, prefer=["jordan"],
        )
        assert "the-payer" in labels(got), (
            "a row that is nobody's surplus was evicted to buy an authority slot"
        )
        assert "statute" not in labels(got), (
            "the statute took a slot with no surplus row available to pay for it"
        )

    def test_and_the_floor_is_a_no_op_on_that_pool_without_a_surplus(self):
        # The premise of the test above, so it cannot pass vacuously: with no surplus the
        # selection is byte-identical whether the floor is on or off.
        pool = self._pool(lone_row_has_kb=False)
        off = apply_floors(pool, 3, kb_floor=1, authority_floor=0, prefer=["jordan"])
        on = apply_floors(pool, 3, kb_floor=1, authority_floor=1, prefer=["jordan"])
        assert labels(off) == labels(on)

    def test_a_collection_contributing_three_passages_loses_its_worst_not_its_best(self):
        # When the surplus sits in the collection that did NOT find the statute, the worst-ranked
        # surplus row pays and every collection keeps its best passage.
        pool = [
            row(0.10, "commentary", kb="a", label="a-best"),
            row(0.20, "commentary", kb="a", label="a-second"),
            row(0.30, "commentary", kb="a", label="a-third"),
            row(0.90, "controlling", kb="b", label="statute"),
        ]
        got = apply_floors(pool, 3, kb_floor=1, authority_floor=1)
        assert "a-best" in labels(got)
        assert "statute" in labels(got)
        assert "a-third" not in labels(got), "the worst surplus row should have paid"
