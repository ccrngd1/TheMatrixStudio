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
