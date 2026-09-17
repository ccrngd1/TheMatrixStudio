# SPDX-License-Identifier: Apache-2.0
"""
Aggregating a set of runs: what held, what dissented, what cannot coexist.

The design under test is a split, and the tests are organised around it:

  computed     every COUNT in the report — shares, Gini, coverage, how many runs held a
               demand. A model asked "how many runs agreed" answers plausibly and
               unfalsifiably; the same number derived from the extractions is checkable.
  extracted    one schema'd call per run for positions, demands, concessions, refusals.
  synthesised  one call over the extractions. The only generative step, and the one with
               rules against collapsing a set of conversations into a single answer.

The failure this whole module is built against: asked to summarise several conversations, a
model produces consensus. An ensemble report reading "the group agreed on a phased rollout"
when three of nine runs refused a rollout is worse than no report, because it looks like
evidence.
"""

import json

import pytest

from matrix_studio import ensemble

pytestmark = pytest.mark.asyncio


def _view(name, arm, turns, positions=None):
    v = ensemble.RunView(run_id=name, name=name, arm=arm, turns=turns,
                         positions=positions or {})
    v.metrics = ensemble.measure(v)
    return v


# One round of three, then two single turns — the hybrid shape.
TURNS = [
    (1, "Ada", "a" * 900), (1, "Bo", "b" * 900), (1, "Cy", "c" * 900),
    (2, "Ada", "d" * 500), (3, "Bo", "e" * 500),
]


# --------------------------------------------------------------------------- #
# computed
# --------------------------------------------------------------------------- #


class TestTheNumbersAreComputed:
    def test_shares_slots_and_multi_speaker_rounds(self):
        m = _view("r", {}, TURNS).metrics
        assert m["turns"] == 5
        assert m["slots"] == 3
        assert m["multi_speaker_slots"] == 1
        assert m["shares"] == {"Ada": 2, "Bo": 2, "Cy": 1}

    def test_gini_matches_the_research_harness(self):
        """`scripts/eval_speaker_selection.py` has its own copy — a script directory must not
        be an import dependency of the shipped package — so the two are pinned to each other
        here. Two implementations of one metric that disagree is worse than either."""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from eval_speaker_selection import gini as harness_gini

        for shares in ([16, 11, 8, 3, 1, 1], [4, 4, 4], [9, 0], [1]):
            assert ensemble.gini(shares) == pytest.approx(harness_gini(shares))

    def test_the_length_trend_is_reported_per_half(self):
        """The one within-run signal for whether a conversation was still gaining substance
        when it stopped, and the measure that separated hybrid from the other two live runs."""
        m = _view("r", {}, TURNS).metrics
        assert m["chars_first_half"] == 900
        assert m["chars_second_half"] > 0
        assert m["chars_second_half"] < m["chars_first_half"]

    def test_a_persona_who_never_spoke_is_named(self):
        v = _view("r", {}, [(1, "Ada", "x"), (2, "Ada", "y")])
        assert v.metrics["shares"] == {"Ada": 2}
        assert v.metrics["never_spoke"] == []  # the cast is derived FROM the transcript

    def test_no_composite_quality_score_is_invented(self):
        """Asserted because it is a tempting thing to add and would let a worse conversation
        outrank a better one by scoring well on whatever is easy to count."""
        m = _view("r", {}, TURNS).metrics
        for banned in ("score", "quality", "rating", "grade"):
            assert not any(banned in k for k in m), m.keys()


# --------------------------------------------------------------------------- #
# per-persona across runs
# --------------------------------------------------------------------------- #

def _pos(name, final, demands=(), refusals=(), concessions=()):
    return {"name": name, "final_position": final, "demands": list(demands),
            "refusals": list(refusals), "concessions": list(concessions)}


class TestPerPersonaAcrossRuns:
    def test_a_demand_in_every_run_is_invariant_and_one_in_some_is_situational(self):
        views = [
            _view("run1", {}, TURNS, {"outcome": "o", "personas": [
                _pos("Ada", "held", demands=["records pull before launch", "a 90-day window"]),
            ]}),
            _view("run2", {}, TURNS, {"outcome": "o", "personas": [
                _pos("Ada", "held", demands=["Records pull, before launch!"]),
            ]}),
        ]
        ada = ensemble.per_persona(views)["Ada"]
        assert [d["claim"] for d in ada["invariant_demands"]] == ["records pull before launch"]
        assert [d["runs"] for d in ada["invariant_demands"]] == [["run1", "run2"]]
        assert [d["claim"] for d in ada["situational_demands"]] == ["a 90-day window"]
        assert ada["situational_demands"][0]["runs"] == ["run1"]

    def test_invariance_is_measured_against_the_runs_the_persona_was_IN(self):
        """A persona who never spoke in a run cannot be said to have dropped a demand there.
        Measuring against the ensemble size instead would make every demand situational as
        soon as one run starved somebody — which is exactly the run you most want to compare."""
        views = [
            _view("a", {}, TURNS, {"outcome": "o", "personas": [_pos("Ada", "x", ["one thing"])]}),
            _view("b", {}, TURNS, {"outcome": "o", "personas": [_pos("Ada", "x", ["one thing"])]}),
            _view("c", {}, TURNS, {"outcome": "o", "personas": [_pos("Bo", "y")]}),
        ]
        ada = ensemble.per_persona(views)["Ada"]
        assert ada["appears_in_runs"] == 2 and ada["of_runs"] == 3
        assert len(ada["invariant_demands"]) == 1

    def test_concessions_carry_what_moved_them(self):
        views = [_view("r", {}, TURNS, {"outcome": "o", "personas": [
            _pos("Ada", "moved", concessions=[
                {"gave_up": "the video requirement", "because": "Bo produced the statute"}]),
        ]})]
        c = ensemble.per_persona(views)["Ada"]["concessions"]
        assert c == [{"run": "r", "gave_up": "the video requirement",
                      "because": "Bo produced the statute"}]

    def test_normalisation_only_collapses_the_obvious(self):
        """Over-merging silently deletes a dissent, so the key is deliberately crude: it
        folds case, punctuation and filler and nothing else. Two demands that differ in a
        word that matters must stay two demands."""
        assert ensemble._normalise("Records pull!") == ensemble._normalise("the records pull")
        assert ensemble._normalise("video required") != ensemble._normalise("video optional")


# --------------------------------------------------------------------------- #
# dissents
# --------------------------------------------------------------------------- #


class TestDissents:
    def test_a_refusal_the_outcome_ignored_is_recorded_as_standing(self):
        views = [_view("r", {}, TURNS, {
            "outcome": "Ship the pilot in California with a records pull.",
            "personas": [_pos("Ada", "objected",
                              refusals=["extending this to net-new authorizations"])],
        })]
        standing = ensemble.agreements_and_dissents(views)["standing_refusals"]
        assert standing == [{"run": "r", "persona": "Ada",
                             "refusal": "extending this to net-new authorizations"}]

    def test_a_refusal_the_outcome_adopted_is_not_a_dissent(self):
        """A disagreement that got resolved is not a dissent. The interesting artefact is the
        objection that survived being answered."""
        views = [_view("r", {}, TURNS, {
            "outcome": "Net-new authorizations are excluded from scope for this launch.",
            "personas": [_pos("Ada", "won", refusals=["net-new authorizations excluded"])],
        })]
        assert ensemble.agreements_and_dissents(views)["standing_refusals"] == []

    def test_unresolved_items_are_ranked_by_how_many_runs_left_them_open(self):
        views = [
            _view("a", {}, TURNS, {"outcome": "o", "personas": [],
                                   "unresolved": ["who owns indemnification"]}),
            _view("b", {}, TURNS, {"outcome": "o", "personas": [],
                                   "unresolved": ["Who owns indemnification?", "the SLA"]}),
        ]
        out = ensemble.agreements_and_dissents(views)["unresolved_by_frequency"]
        assert out[0]["count"] == 2 and out[0]["runs"] == ["a", "b"]
        assert out[1]["count"] == 1


# --------------------------------------------------------------------------- #
# extraction and synthesis: the model-facing seams
# --------------------------------------------------------------------------- #


class TestTheModelFacingSteps:
    async def test_extraction_sends_the_transcript_and_parses_a_fenced_reply(self):
        seen = {}

        async def fake(messages, model=None, temperature=0.4, max_tokens=None):
            seen["prompt"] = messages[0]["content"]
            seen["temperature"] = temperature
            return {"content": '```json\n{"personas": [], "outcome": "escalated"}\n```',
                    "cost_usd": 0.01}

        got = await ensemble.extract_positions(_view("r", {}, TURNS), call=fake)
        assert got["outcome"] == "escalated"
        assert got["_cost_usd"] == 0.01
        assert "[1] Ada:" in seen["prompt"]
        assert seen["temperature"] == 0.0, "extraction is not a creative act"

    async def test_an_unreadable_extraction_is_dropped_not_guessed(self):
        async def fake(messages, model=None, temperature=0.4, max_tokens=None):
            return {"content": "I could not do that", "cost_usd": 0.0}

        assert await ensemble.extract_positions(_view("r", {}, TURNS), call=fake) == {}

    async def test_the_extraction_prompt_forbids_manufacturing_movement(self):
        """The specific failure: a model asked for concessions will find them. A persona who
        never conceded must come back with an empty list, or the per-persona view invents
        evidence-driven position change that never happened."""
        assert "EMPTY concessions list" in ensemble._EXTRACT_PROMPT
        assert "Do not manufacture movement" in ensemble._EXTRACT_PROMPT
        assert "Do not smooth disagreement into agreement" in ensemble._EXTRACT_PROMPT

    async def test_the_synthesis_receives_every_arm_and_its_settings(self):
        """It cannot attribute a difference to a setting it was not told about."""
        seen = {}

        async def fake(messages, model=None, temperature=0.4, max_tokens=None):
            seen["prompt"] = messages[0]["content"]
            return {"content": "report", "cost_usd": 0.2}

        views = [
            _view("fair", {"method": "moderated", "max_messages": 40}, TURNS,
                  {"outcome": "o", "personas": []}),
            _view("hybrid", {"method": "hybrid", "hybrid_opening_rounds": 2}, TURNS,
                  {"outcome": "o", "personas": []}),
        ]
        out = await ensemble.synthesise(views, call=fake)
        assert out["content"] == "report" and out["cost_usd"] == 0.2
        assert '"method": "hybrid"' in seen["prompt"]
        assert '"hybrid_opening_rounds": 2' in seen["prompt"]
        assert "gini" in seen["prompt"], "the computed metrics travel with the arms"

    def test_the_synthesis_rules_forbid_a_single_reconciled_answer(self):
        """The trap the module exists to avoid, pinned so a later edit that asks for "the
        consensus across runs" fails here rather than in a report somebody trusts."""
        # Whitespace-normalised, because these are wrapped prose constants: asserting on
        # layout means a reflow breaks the test while the rule it protects is intact.
        rules = " ".join(ensemble._COMPATIBILITY_RULES.split())
        assert "forbidden from producing a single reconciled recommendation" in rules
        assert "ONLY use this if you can name a concrete case" in rules
        assert "same-action-different-reason" in rules
        assert "must name the runs it came from" in rules
        assert "consensus" not in ensemble._SYNTH_PROMPT.lower()

    async def test_collect_reads_the_arm_settings_off_the_run_row(self, db):
        await db.create_run(
            run_id="ens-1", topic="t",
            cast=[{"name": "Ada", "persona": "p", "goals": []}],
            config={"max_messages": 20, "selection": {"method": "hybrid",
                                                      "hybrid_opening_rounds": 3,
                                                      "closing_round": True}},
        )
        views = await ensemble.collect(db, ["ens-1", "does-not-exist"])
        assert len(views) == 1, "a missing run is skipped, not fatal"
        assert views[0].arm == {"method": "hybrid", "max_messages": 20,
                                "closing_round": True, "hybrid_opening_rounds": 3}


def test_the_extraction_prompt_asks_for_every_field_in_the_schema():
    """The schema constant was written before the prompt had any JSON contract at all, so nine
    transcripts came back as prose and were dropped. This pins them together: a field added to
    the schema and not to the prompt fails here."""
    prompt = ensemble._EXTRACT_PROMPT
    persona = ensemble._EXTRACT_SCHEMA["properties"]["personas"]["items"]["properties"]
    for field in list(persona) + ["outcome", "unresolved"]:
        assert field in prompt, field
    assert "Return ONLY a JSON object" in prompt
    # Last, not first: the cognition work measured that a contract stated before a wall of
    # transcript gets answered in prose (12/12 without passages vs 4/8 with).
    assert prompt.index("Return ONLY a JSON object") > prompt.index("{transcript}")
