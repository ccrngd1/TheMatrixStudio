# SPDX-License-Identifier: Apache-2.0
"""What an ensemble may vary, and the proof that it varied only that.

The module under test is mostly refusals, so most of these tests assert a refusal. That is
deliberate: `docs/ENSEMBLE-CONVERSATIONS.md` §3.3 records a nine-run sweep that moved
three settings at once with one run per cell and could not attribute a single difference it
found. The planner exists to make that shape unconstructible, and a refusal with no test
is a refusal someone deletes to make their branch pass.

`check_isolated` is the load-bearing one. It measures divergence from the materialised
configs instead of trusting the declaration, so the tests feed it configs that diverge in
ways no cell declared — including via a shared mutable base, which is how this bug would
actually arrive.
"""

import pytest

from matrix_studio import ensemble_spec as spec


BASE = {"max_messages": 40, "selection": {"fairness": True}}


# --------------------------------------------------------------------------- #
# the default shape
# --------------------------------------------------------------------------- #


class TestReplicatesAreTheDefault:
    def test_replicates_vary_nothing(self):
        members = spec.plan(BASE, spec.replicates())
        assert len(members) == spec.DEFAULT_REPLICATES
        assert {m.cell for m in members} == {"base"}
        # The point of the default mode: every member is the base config, unchanged.
        assert all(m.config == BASE for m in members)
        assert spec.divergent_keys(members) == {}

    def test_members_are_indexed_from_one_for_names(self):
        members = spec.plan(BASE, spec.replicates(n=3))
        assert [m.index for m in members] == [1, 2, 3]
        assert [m.name_for("renewal") for m in members] == [
            "renewal-base1", "renewal-base2", "renewal-base3",
        ]

    def test_planning_does_not_mutate_the_caller_s_config(self):
        base = {"selection": {"fairness": True}}
        spec.plan(base, spec.with_hybrid(n=2, base=2))
        assert base == {"selection": {"fairness": True}}, (
            "A shared base config mutated by the planner would make every later member "
            "carry an earlier cell's override."
        )

    def test_a_cell_of_one_is_refused(self):
        with pytest.raises(spec.SpecError, match="within-cell variance"):
            spec.plan(BASE, [spec.Cell(label="base", n=1)])


# --------------------------------------------------------------------------- #
# what may vary
# --------------------------------------------------------------------------- #


class TestTheOverrideAllowlist:
    def test_hybrid_cell_diverges_only_where_declared(self):
        cells = spec.with_hybrid(n=3, base=5)
        members = spec.plan(BASE, cells)
        assert len(members) == 8
        assert sorted(spec.divergent_keys(members)) == [
            "selection.hybrid_opening_rounds", "selection.method",
        ]
        hybrid = [m for m in members if m.cell == "hybrid"]
        assert all(m.config["selection"]["method"] == "hybrid" for m in hybrid)
        # Fairness is untouched by the override, so it is still shared across cells.
        assert all(m.config["selection"]["fairness"] is True for m in members)

    def test_turn_count_is_refused_as_censoring(self):
        with pytest.raises(spec.SpecError, match="censoring"):
            spec.plan(BASE, [spec.Cell("short", 2, {"max_messages": 8})])

    def test_fairness_is_refused_as_already_measured(self):
        with pytest.raises(spec.SpecError, match="0.458"):
            spec.plan(BASE, [spec.Cell("unfair", 2, {"selection.fairness": False})])

    def test_persona_instructions_are_refused_by_prefix(self):
        # §7: the personas are the instrument. Refused by prefix so a key nobody thought
        # of is refused too, rather than falling through to the generic message.
        with pytest.raises(spec.SpecError, match="measuring instrument"):
            spec.plan(BASE, [spec.Cell("blunt", 2, {"personas.dismissal_rule": "blunt"})])

    def test_an_unlisted_key_is_refused_and_the_message_names_what_is_allowed(self):
        with pytest.raises(spec.SpecError) as err:
            spec.plan(BASE, [spec.Cell("k", 2, {"retrieval.k": 9})])
        assert "selection.method" in str(err.value)

    def test_a_refused_key_beats_the_generic_message(self):
        # `models.voice` is both absent from the allowlist and specifically refused. The
        # specific reason is the useful one, so it has to win.
        with pytest.raises(spec.SpecError, match="Changes the instrument"):
            spec.plan(BASE, [spec.Cell("opus", 2, {"models.voice": "bedrock/x"})])


# --------------------------------------------------------------------------- #
# labels
# --------------------------------------------------------------------------- #


class TestLabels:
    def test_duplicate_labels_are_refused_because_they_would_merge_cells(self):
        with pytest.raises(spec.SpecError, match="flattening"):
            spec.plan(BASE, [spec.Cell("base", 2), spec.Cell("base", 2)])

    def test_an_empty_label_is_refused(self):
        with pytest.raises(spec.SpecError, match="needs a label"):
            spec.plan(BASE, [spec.Cell("  ", 2)])

    def test_a_padded_label_is_refused_rather_than_trimmed(self):
        # Trimming would make "base" and "base " the same cell in the report while the
        # stored spec still showed two, so this is refused instead of silently fixed.
        with pytest.raises(spec.SpecError, match="trailing space"):
            spec.plan(BASE, [spec.Cell("base ", 2)])

    def test_no_cells_is_refused(self):
        with pytest.raises(spec.SpecError, match="at least one cell"):
            spec.plan(BASE, [])

    def test_the_member_guard_holds(self):
        with pytest.raises(spec.SpecError, match=str(spec.MAX_MEMBERS)):
            spec.plan(BASE, [spec.Cell("a", spec.MAX_MEMBERS), spec.Cell("b", 2)])


# --------------------------------------------------------------------------- #
# applying overrides
# --------------------------------------------------------------------------- #


class TestApplyOverrides:
    def test_creates_a_missing_block(self):
        out = spec.apply_overrides({}, {"selection.method": "hybrid"})
        assert out == {"selection": {"method": "hybrid"}}

    def test_replaces_a_none_block_rather_than_raising(self):
        # Every optional config block is spelled `None` when absent, so this is the
        # common case, not an edge case.
        out = spec.apply_overrides(
            {"selection": None}, {"selection.method": "rotation"}
        )
        assert out == {"selection": {"method": "rotation"}}

    def test_preserves_siblings(self):
        out = spec.apply_overrides(BASE, {"selection.method": "hybrid"})
        assert out["selection"] == {"fairness": True, "method": "hybrid"}
        assert out["max_messages"] == 40

    def test_empty_key_is_refused(self):
        with pytest.raises(spec.SpecError, match="cannot be empty"):
            spec.apply_overrides(BASE, {"": 1})


# --------------------------------------------------------------------------- #
# the isolation proof
# --------------------------------------------------------------------------- #


class TestCheckIsolated:
    def test_members_of_one_cell_must_share_a_config(self):
        members = [
            spec.Member(cell="base", index=1, config={"max_messages": 40}),
            spec.Member(cell="base", index=2, config={"max_messages": 24}),
        ]
        with pytest.raises(spec.SpecError, match="byte-identical"):
            spec.check_isolated(members, [spec.Cell("base", 2)])

    def test_an_undeclared_between_cell_difference_is_refused(self):
        # The bug this is really for: cell configs that differ somewhere nobody declared.
        # Here the hybrid cell also carries a shorter run, which is exactly the
        # confound that made the original nine-run sweep uninterpretable.
        cells = [
            spec.Cell("base", 2),
            spec.Cell("hybrid", 2, {"selection.method": "hybrid"}),
        ]
        members = [
            spec.Member("base", 1, {"max_messages": 40, "selection": {"method": "moderated"}}),
            spec.Member("base", 2, {"max_messages": 40, "selection": {"method": "moderated"}}),
            spec.Member("hybrid", 1, {"max_messages": 8, "selection": {"method": "hybrid"}}),
            spec.Member("hybrid", 2, {"max_messages": 8, "selection": {"method": "hybrid"}}),
        ]
        with pytest.raises(spec.SpecError, match="max_messages"):
            spec.check_isolated(members, cells)

    def test_a_declared_difference_passes(self):
        cells = [spec.Cell("base", 2), spec.Cell("hybrid", 2, {"selection.method": "hybrid"})]
        members = [
            spec.Member("base", 1, {"selection": {"method": "moderated"}}),
            spec.Member("base", 2, {"selection": {"method": "moderated"}}),
            spec.Member("hybrid", 1, {"selection": {"method": "hybrid"}}),
            spec.Member("hybrid", 2, {"selection": {"method": "hybrid"}}),
        ]
        spec.check_isolated(members, cells)

    def test_a_key_present_in_one_cell_and_absent_in_another_counts_as_divergent(self):
        # Absent is not the same as equal. If this compared only shared keys, a cell that
        # gained a whole block would pass isolation.
        members = [
            spec.Member("a", 1, {"x": 1}),
            spec.Member("b", 1, {"x": 1, "y": 2}),
        ]
        assert "y" in spec.divergent_keys(members)

    def test_an_empty_block_differs_from_an_absent_one(self):
        members = [
            spec.Member("a", 1, {"selection": {}}),
            spec.Member("b", 1, {}),
        ]
        assert "selection" in spec.divergent_keys(members)


# --------------------------------------------------------------------------- #
# storing and rebuilding a spec
# --------------------------------------------------------------------------- #


class TestDescribeAndRebuild:
    def test_round_trips(self):
        cells = spec.with_hybrid(n=3, base=4)
        rebuilt = spec.cells_from(spec.describe(cells))
        assert rebuilt == cells

    def test_describe_is_json_shaped(self):
        import json

        assert json.loads(json.dumps(spec.describe(spec.with_hybrid()))) == [
            {"label": "base", "n": 5, "overrides": {}},
            {
                "label": "hybrid",
                "n": 3,
                "overrides": {
                    "selection.method": "hybrid",
                    "selection.hybrid_opening_rounds": 2,
                },
            },
        ]

    def test_rebuilding_validates(self):
        with pytest.raises(spec.SpecError, match="within-cell variance"):
            spec.cells_from([{"label": "base", "n": 1}])

    def test_an_unknown_cell_field_is_refused(self):
        with pytest.raises(spec.SpecError, match="Unknown cell field"):
            spec.cells_from([{"label": "base", "n": 2, "method": "hybrid"}])

    def test_overrides_must_be_an_object(self):
        with pytest.raises(spec.SpecError, match="dotted paths"):
            spec.cells_from([{"label": "base", "n": 2, "overrides": ["a"]}])


# --------------------------------------------------------------------------- #
# tiers
# --------------------------------------------------------------------------- #


class TestTiers:
    @pytest.mark.parametrize(
        "held,total,expected",
        [
            (5, 5, "unanimous"),
            (0, 5, "absent"),
            (4, 5, "split"),
            (3, 5, "split"),
            (2, 5, "split"),
            (1, 5, "rare"),    # never independently reproduced
            (2, 2, "unanimous"),
            (1, 2, "rare"),
        ],
    )
    def test_the_tiers(self, held, total, expected):
        assert spec.tier(held, total) == expected

    def test_a_majority_cut_is_not_reintroduced(self):
        # The property §8.2 actually demands: at N=5, 3/5 and 2/5 are the same finding.
        # A tier boundary between them would smuggle back the comparison the doc says
        # this N cannot support. An earlier draft used `held * 2 >= total` and did exactly
        # that, which is why this is pinned separately from the table above.
        assert spec.tier(3, 5) == spec.tier(2, 5)

    def test_rare_means_unreplicated_not_merely_few(self):
        # Scaling N must not move the boundary: 2 of 12 is still 'two runs independently
        # produced it', which is a different claim from 'one run did'.
        assert spec.tier(2, 12) == "split"
        assert spec.tier(1, 12) == "rare"

    def test_zero_runs_has_no_tier(self):
        with pytest.raises(ValueError, match="not defined"):
            spec.tier(0, 0)

    def test_an_impossible_count_is_refused(self):
        with pytest.raises(ValueError, match="not a possible count"):
            spec.tier(6, 5)
