# SPDX-License-Identifier: Apache-2.0
"""
The one-page decision brief — `matrix_studio/brief.py`.

Its organising rule came from the room that asked for it (brainstorm-opus): **no number without
its group and replicate count; otherwise "not yet measured".** The customer persona dropped his own
request for a single confidence number there, persuaded it would be indefensible to leadership. So
most of these assert what the brief must NOT say.
"""

import re

import pytest

from matrix_studio import brief as br
from tests.test_export import CONCERN, VALIDITY, _concl, _ensemble_model, _run_model, _with


# --------------------------------------------------------------------------- #
# A single conversation is never given a confidence number
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fmt", ["md", "html"])
def test_one_conversation_is_not_yet_measured_and_carries_no_percentage(fmt):
    out = br.render(br.run_brief(_run_model()), fmt)
    assert "not yet measured" in out
    assert "one conversation" in out
    # The VISIBLE text, not the stylesheet — `width:100%` is not a confidence claim, and the
    # first version of this test tripped on it.
    visible = re.sub(r"<style>.*?</style>", "", out, flags=re.S)
    assert not re.search(r"\b\d{1,3}\s?%", visible), "a single run must not be given a confidence percentage"


def test_the_run_brief_says_how_to_get_a_measurement():
    assert "as an ensemble" in br.render_markdown(br.run_brief(_run_model()))


# --------------------------------------------------------------------------- #
# An ensemble's conclusions are per group, and only the recurring ones lead
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fmt", ["md", "html"])
def test_ensemble_conclusions_are_per_group_never_pooled(fmt):
    b = br.ensemble_brief(_with([_concl("exclude California", base=(4, 5), hybrid=(1, 4))]))
    out = br.render(b, fmt)
    assert "4 of 5" in out and "1 of 4" in out
    assert "5 of 9" not in out


def test_only_recurring_conclusions_are_listed_and_the_rest_are_counted():
    b = br.ensemble_brief(_with([
        _concl("exclude California", base=(3, 5)),
        _concl("file today", base=(1, 5)),
        _concl("pilot first", base=(1, 5), hybrid=(1, 4)),  # two runs, never twice in one group
    ]))
    md = br.render_markdown(b)
    assert "exclude California" in md
    assert "file today" not in md and "pilot first" not in md
    # Counted, and described by what the rule tests — not "a single run", which "pilot first"
    # (one run in EACH group) is not.
    assert "2 further conclusion(s) were never reached twice within any group" in md


def test_divergence_is_the_headline_when_nothing_recurred():
    md = br.render_markdown(br.ensemble_brief(_with([_concl("a", base=(1, 5)), _concl("b", base=(1, 5))])))
    assert "No conclusion recurred" in md


def test_no_agreement_is_stated_rather_than_omitted():
    rep = _ensemble_model()["report"]
    for c in rep["claims"]:
        for cell in c["per_cell"].values():
            cell["tier"] = "split"
    rep["conclusions"] = []
    md = br.render_markdown(br.ensemble_brief(_ensemble_model(report=rep)))
    assert "Nothing was held by every run of any group." in md


def test_an_ensemble_without_a_report_says_so():
    md = br.render_markdown(br.ensemble_brief(_ensemble_model(report=None)))
    assert "No report has been generated" in md


def test_the_ensemble_confidence_line_names_each_groups_replicates():
    rep = _ensemble_model()["report"]
    rep["cells"] = [{"cell": "base", "usable": 5}, {"cell": "hybrid", "usable": 3}]
    md = br.render_markdown(br.ensemble_brief(_ensemble_model(report=rep)))
    assert "base: 5 usable run(s); hybrid: 3 usable run(s)" in md


# --------------------------------------------------------------------------- #
# One page, and it says when it is not the whole story
# --------------------------------------------------------------------------- #


def test_caps_say_what_they_cut():
    m = _run_model(summary={"overview": "x", "consensus": [f"point {i}" for i in range(10)],
                            "open_questions": [f"q {i}" for i in range(9)], "key_ideas": [],
                            "dissenters": [{"speaker": f"P{i}", "position": "no"} for i in range(6)]})
    md = br.render_markdown(br.run_brief(m))
    assert f"…and {10 - br.MAX_AGREED} more in the full report" in md
    assert f"…and {9 - br.MAX_OPEN} more in the full report" in md
    assert f"…and {6 - br.MAX_DISSENT} more in the full report" in md


def test_a_long_item_is_clipped_visibly():
    long = "word " * 200
    m = _run_model(summary={"overview": long, "consensus": [long], "open_questions": [],
                            "key_ideas": [], "dissenters": []})
    b = br.run_brief(m)
    assert b["agreed"][0].endswith("…") and len(b["agreed"][0]) <= br.MAX_ITEM_CHARS + 1
    assert b["bottom_line"].endswith("…")


def test_the_longest_realistic_brief_stays_near_one_page():
    """Measured on brainstorm-opus: 812 words before the caps. A brief built at EVERY cap —
    the worst case, not the realistic one — was 704 words on the first try, which spills onto a
    second page; the item and bottom-line ceilings were lowered until the worst case fits."""
    long = "a considered, specific sentence that a persona might well say in a long debate " * 4
    m = _run_model(topic=long * 3, summary={
        "overview": long * 4, "consensus": [long] * 10, "open_questions": [long] * 10,
        "key_ideas": [], "dissenters": [{"speaker": "P", "position": long}] * 10})
    assert len(br.render_markdown(br.run_brief(m)).split()) < 650


# --------------------------------------------------------------------------- #
# Inherited guarantees
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fmt", ["md", "html"])
def test_private_persona_fields_never_appear(fmt):
    out = br.render(br.run_brief(_run_model()), fmt)
    assert CONCERN not in out and VALIDITY not in out


def test_model_text_is_escaped():
    hostile = "<script>alert(1)</script>"
    m = _run_model(name=hostile, topic=hostile, summary={
        "overview": hostile, "consensus": [hostile], "open_questions": [hostile], "key_ideas": [],
        "dissenters": [{"speaker": hostile, "position": hostile}]})
    out = br.render_html(br.run_brief(m))
    assert "<script>alert" not in out and "&lt;script&gt;" in out


def test_the_brief_is_labelled_without_pointing_at_transcripts_that_are_not_there():
    out = br.render_markdown(br.run_brief(_run_model()))
    assert br.BRIEF_LABEL in out
    assert "transcript(s) above" not in out


def test_structured_key_ideas_and_consensus_items_render_as_text():
    m = _run_model(summary={"overview": "o", "open_questions": [], "key_ideas": [], "dissenters": [],
                            "consensus": [{"idea": "ship templates", "proposed_by": "Priya"}]})
    assert br.run_brief(m)["agreed"] == ["ship templates — Priya"]


def test_filenames_say_it_is_a_brief():
    assert br.filename(br.run_brief(_run_model()), "md") == "renewal-take-2-run-brief.md"
    assert br.filename(br.ensemble_brief(_ensemble_model()), "html") == "renewal-cells-ensemble-brief.html"


# --------------------------------------------------------------------------- #
# What would settle it: the evidence plan and the conditional recommendation
# --------------------------------------------------------------------------- #

_PLAN_ROW = {"data": "pilot churn", "asked_by": "Dana", "decision": "launch or hold",
             "moves_them": "under 5% and I'm in", "best_guess": "about 7%", "cheapest_way": "a 2-week pilot"}


@pytest.mark.parametrize("fmt", ["md", "html"])
def test_the_brief_says_what_would_settle_it(fmt):
    m = _run_model(summary={"overview": "o", "consensus": [], "open_questions": [], "key_ideas": [],
                            "dissenters": [], "evidence_plan": [_PLAN_ROW],
                            "conditional_recommendation": "If churn is under 5%, launch; if not, hold. Lean hold."})
    out = br.render(br.run_brief(m), fmt)
    assert "What would settle it" in out and "Lean hold" in out
    assert "pilot churn" in out and "about 7%" in out and "under 5%" in out


def test_no_plan_means_no_section():
    out = br.render_markdown(br.run_brief(_run_model()))
    assert "What would settle it" not in out


def test_a_plan_gives_the_vaguer_lists_room_and_the_brief_stays_one_page():
    long = "a considered, specific sentence that a persona might well say in a long debate " * 4
    row = {k: long for k in _PLAN_ROW}
    m = _run_model(topic=long * 3, summary={
        "overview": long * 4, "consensus": [long] * 10, "open_questions": [long] * 10, "key_ideas": [],
        "dissenters": [{"speaker": "P", "position": long}] * 10,
        "evidence_plan": [row] * 10, "conditional_recommendation": long * 4})
    b = br.run_brief(m)
    assert len(b["evidence"]) == br.MAX_EVIDENCE and b["evidence_more"] == 10 - br.MAX_EVIDENCE
    assert len(b["open"]) == br.MAX_OPEN_WITH_PLAN and len(b["dissent"]) == br.MAX_DISSENT_WITH_PLAN
    assert len(br.render_markdown(b).split()) < 650


def test_plan_text_is_escaped():
    hostile = "<script>alert(1)</script>"
    m = _run_model(summary={"overview": "o", "consensus": [], "open_questions": [], "key_ideas": [],
                            "dissenters": [], "evidence_plan": [{**_PLAN_ROW, "data": hostile}],
                            "conditional_recommendation": hostile})
    out = br.render_html(br.run_brief(m))
    assert "<script>alert" not in out
