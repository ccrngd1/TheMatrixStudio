# SPDX-License-Identifier: Apache-2.0
"""
The per-collection floor: a bound collection must be able to contribute.

Run `2d2ac45b` bound six personas to six private collections plus one cast-wide collection
holding the proposal under discussion. All 24 turns retrieved from the shared collection and
none from any persona's own — with correct bindings and every index queried. A global top-k
at ``k=3`` gave every slot to the collection whose wording mirrored the conversation, on
every turn, because a turn's query is a term bag drawn from recent conversation text.

The tests below are the two halves of that: the floor promotes a starved collection, and it
does not otherwise rearrange what global rank already got right.
"""

import pytest

from matrix_studio.storage.vectors import merge_with_source_floor


def row(kb: str, score: float, chunk: str = "") -> dict:
    return {"kb_id": kb, "score": score, "chunk_id": chunk or f"{kb}-{score}"}


class TestTheFloorPromotesAStarvedCollection:
    def test_the_shape_that_failed_in_production(self):
        """Shared collection sweeps the top; one private collection is bound and worse."""
        rows = [
            row("proposal", 0.21), row("proposal", 0.22), row("proposal", 0.23),
            row("riley", 0.49),
        ]
        got = merge_with_source_floor(rows, 3, floor=1)
        assert [r["kb_id"] for r in got] == ["proposal", "proposal", "riley"], got

    def test_without_the_floor_the_private_collection_gets_nothing(self):
        """The premise. `floor=0` must reproduce the bug, or the test above proves nothing."""
        rows = [
            row("proposal", 0.21), row("proposal", 0.22), row("proposal", 0.23),
            row("riley", 0.49),
        ]
        got = merge_with_source_floor(rows, 3, floor=0)
        assert {r["kb_id"] for r in got} == {"proposal"}

    def test_the_best_row_overall_is_never_dropped(self):
        """A floor may not cost the top passage — that would be a worse bug than the one
        it fixes, and a silent one."""
        rows = [row("a", 0.10)] + [row("b", 0.20 + i / 100) for i in range(5)]
        got = merge_with_source_floor(rows, 3, floor=1)
        assert got[0]["kb_id"] == "a" and got[0]["score"] == 0.10

    def test_results_stay_sorted_by_distance(self):
        """`apply_similarity_floor` and every caller downstream assume ascending order."""
        rows = [row("a", 0.9), row("b", 0.1), row("c", 0.5)]
        scores = [r["score"] for r in merge_with_source_floor(rows, 3, floor=1)]
        assert scores == sorted(scores)


class TestItDoesNotRearrangeWhatWasAlreadyRight:
    def test_a_balanced_result_is_unchanged(self):
        rows = [row("a", 0.1), row("b", 0.2), row("a", 0.3), row("b", 0.4)]
        floored = merge_with_source_floor(rows, 3, floor=1)
        exact = merge_with_source_floor(rows, 3, floor=0)
        assert [r["chunk_id"] for r in floored] == [r["chunk_id"] for r in exact]

    def test_one_collection_alone_is_unaffected(self):
        rows = [row("only", 0.1), row("only", 0.2), row("only", 0.3), row("only", 0.4)]
        got = merge_with_source_floor(rows, 3, floor=1)
        assert [r["score"] for r in got] == [0.1, 0.2, 0.3]


class TestDegradation:
    def test_more_collections_than_slots_never_exceeds_k(self):
        """Six bound collections, three slots: the answer must still be three rows."""
        rows = [row(f"kb{i}", 0.10 * (i + 1)) for i in range(6)]
        got = merge_with_source_floor(rows, 3, floor=1)
        assert len(got) == 3
        # And the three that win are the best three, so the outcome is deterministic
        # rather than dependent on iteration order.
        assert [r["kb_id"] for r in got] == ["kb0", "kb1", "kb2"]

    def test_a_run_scoped_row_does_NOT_get_a_reserved_slot(self):
        """A row with no `kb_id` is not a collection, so it reserves nothing.

        The run's own attached documents come back without a `kb_id`, and reserving for
        them would change behaviour this bug says nothing about:
        `test_the_trim_happens_after_the_merge_not_per_source` asserts that KB passages
        outranking a run passage take every slot, and that should keep being true. They
        still compete normally for slots the floor did not claim.

        My first version of this test asserted the opposite and passed — which is why it is
        worth stating explicitly rather than leaving to the implementation.
        """
        rows = [
            {"score": 0.60, "chunk_id": "run-1"},          # the run's own document
            row("kb", 0.10), row("kb", 0.11), row("kb", 0.12),
        ]
        got = merge_with_source_floor(rows, 3, floor=1)
        assert [r["chunk_id"] for r in got] == ["kb-0.1", "kb-0.11", "kb-0.12"], got

    def test_but_it_still_wins_an_unreserved_slot_on_rank(self):
        rows = [
            {"score": 0.05, "chunk_id": "run-1"},          # better than everything
            row("kb", 0.10), row("kb", 0.11),
        ]
        got = merge_with_source_floor(rows, 2, floor=1)
        assert [r["chunk_id"] for r in got] == ["run-1", "kb-0.1"], got

    @pytest.mark.parametrize("k", [0, -1])
    def test_a_non_positive_k_returns_nothing(self, k):
        assert merge_with_source_floor([row("a", 0.1)], k, floor=1) == []

    def test_no_rows_returns_nothing(self):
        assert merge_with_source_floor([], 3, floor=1) == []

    def test_a_missing_score_is_treated_as_zero_rather_than_raising(self):
        """Defensive: a provider row without a distance must not end a turn."""
        got = merge_with_source_floor([{"kb_id": "a"}, row("b", 0.5)], 2, floor=1)
        assert len(got) == 2

    def test_duplicate_rows_from_one_source_are_all_eligible(self):
        """The floor reserves ONE row per source; the rest compete normally, so a source
        with several strong passages still fills the remaining slots."""
        rows = [row("a", 0.1), row("a", 0.2), row("a", 0.3), row("b", 0.9)]
        got = merge_with_source_floor(rows, 4, floor=1)
        assert [r["kb_id"] for r in got] == ["a", "a", "a", "b"]


class TestTheFloorSurvivesTheOuterMerge:
    """`retrieval.retrieve_for_turn` merges KB rows with the run's own rows and trims to
    `fetch_k` — a second global top-k that would discard exactly what the fan-out reserved.
    Asserted here at the helper level; the wiring is asserted by the retrieval tests."""

    def test_a_second_pass_over_already_floored_rows_is_stable(self):
        rows = [
            row("proposal", 0.21), row("proposal", 0.22), row("riley", 0.49),
        ]
        once = merge_with_source_floor(rows, 3, floor=1)
        twice = merge_with_source_floor(once, 3, floor=1)
        assert [r["chunk_id"] for r in once] == [r["chunk_id"] for r in twice]
