# SPDX-License-Identifier: Apache-2.0
"""
Persona names in text outputs say they are simulated — `matrix_studio/persona_label.py`.

"(bot) Ruth" in every export and brief, for personas and consultants. Never for an operator's injected
message, which is a real person's words; never in the stored data; and never by rewriting the analyst's
prose, only the summary fields that are themselves a name.
"""

import copy

import pytest

from matrix_studio import brief as br
from matrix_studio import export as ex
from matrix_studio.persona_label import BOT_PREFIX, bot, label_run_model


def _model(**over):
    m = {
        "kind": "run", "id": "r1", "name": "renewal", "description": None, "topic": "t", "status": "complete",
        "created_at": 1790000000, "turn_count": 3, "cost_usd": 0.1, "converged": None,
        "cast": ex.public_cast([
            {"name": "Ruth", "persona": "a lawyer", "goals": [], "structured": {"role": "counsel"}},
            {"name": "Sam", "persona": "an engineer", "goals": []},
        ]),
        "settings": [], "research": None, "assumptions": [],
        "transcript": [
            {"turn": 1, "speaker": "Ruth", "message": "Sam is wrong.", "passages": [],
             "shift": {"sentences": ["Sam moved me."], "conditions": [], "matched_conditions": [],
                       "no_listed_condition": False,
                       "credits": [{"kind": "persona", "name": "Sam"}, {"kind": "assumption", "name": "A1"}]}},
            {"turn": 2, "speaker": "Kim (consultant)", "message": "Asked by Ruth: “q”\n\nAn answer.",
             "passages": []},
            {"turn": 3, "speaker": "Operator", "message": "Please wrap up.", "passages": [], "injected": True},
        ],
        "summary": {"overview": "Ruth argued with Sam.", "dissenters": [{"speaker": "Ruth", "position": "no"}],
                    "evidence_plan": [{"data": "a survey", "asked_by": "Ruth and Sam", "decision": "go",
                                       "moves_them": "a yes", "best_guess": "no", "cheapest_way": "ask"}],
                    "consensus": [], "open_questions": [], "key_ideas": [{"idea": "x", "proposed_by": "Sam"}]},
        "exported_at": 1790000100,
    }
    m.update(over)
    return m


def test_bot_is_idempotent():
    assert bot("Ruth") == "(bot) Ruth" == bot(bot("Ruth"))
    assert bot("") == ""


def test_the_model_is_marked_and_the_input_is_not():
    model = _model()
    before = copy.deepcopy(model)
    out = label_run_model(model)
    assert model == before, "labelling mutated the export model"
    assert [c["name"] for c in out["cast"]] == ["(bot) Ruth", "(bot) Sam"]
    assert [t["speaker"] for t in out["transcript"]] == ["(bot) Ruth", "(bot) Kim (consultant)", "Operator"]
    assert out["summary"]["dissenters"][0]["speaker"] == "(bot) Ruth"
    assert out["summary"]["evidence_plan"][0]["asked_by"] == "(bot) Ruth, (bot) Sam"
    assert out["summary"]["key_ideas"][0]["proposed_by"] == "(bot) Sam"
    # A shift's credits: the persona is marked, the assumption is not.
    assert out["transcript"][0]["shift"]["credits"] == [{"kind": "persona", "name": "(bot) Sam"},
                                                        {"kind": "assumption", "name": "A1"}]
    # Prose is the model's and is left exactly as written.
    assert out["summary"]["overview"] == "Ruth argued with Sam."
    assert out["transcript"][0]["message"] == "Sam is wrong."
    assert label_run_model(out) == out


def test_a_summary_name_nobody_in_the_cast_has_is_left_alone():
    out = label_run_model(_model(summary={"dissenters": [{"speaker": "the board", "position": "no"}]}))
    assert out["summary"]["dissenters"][0]["speaker"] == "the board"


def test_a_persona_added_at_a_fork_is_marked_from_the_transcript():
    m = _model()
    m["transcript"].append({"turn": 4, "speaker": "Carol", "message": "Hello.", "passages": []})
    m["summary"]["dissenters"].append({"speaker": "Carol", "position": "maybe"})
    out = label_run_model(m)
    assert out["transcript"][-1]["speaker"] == "(bot) Carol"
    assert out["summary"]["dissenters"][-1]["speaker"] == "(bot) Carol"


@pytest.mark.parametrize("fmt", ["md", "html"])
def test_the_export_marks_every_persona_and_no_operator(fmt):
    out = ex.render(_model(), fmt)
    assert "(bot) Ruth" in out and "(bot) Sam" in out and "(bot) Kim (consultant)" in out
    assert "(bot) Operator" not in out
    assert out.count(BOT_PREFIX.strip()) >= 5


def test_the_markdown_headings_and_turns_carry_the_marker():
    out = ex.render_markdown(_model())
    assert "### (bot) Ruth — counsel" in out
    assert "**Turn 1 — (bot) Ruth**" in out
    assert "**Turn 3 — Operator *(injected by the operator)*" in out
    assert "- **(bot) Ruth**: no" in out


def test_the_brief_marks_standing_objections_and_evidence():
    b = br.run_brief(_model())
    assert b["dissent"] == ["(bot) Ruth: no"]
    assert "((bot) Ruth, (bot) Sam)" in b["evidence"][0]
    md = br.render(b, "md")
    assert "- (bot) Ruth: no" in md


def test_an_ensemble_model_passes_through():
    m = {"kind": "ensemble", "id": "e1"}
    assert label_run_model(m) is m


# --------------------------------------------------------------------------- #
# The summary's underlying concerns: each row's `speaker` names a persona too
# --------------------------------------------------------------------------- #


def _concerns_model(withheld=False):
    return _model(summary={
        "overview": "o", "consensus": [], "open_questions": [], "key_ideas": [], "dissenters": [],
        "concerns": [
            {"speaker": "Ruth", "concern": "the clause binds us for a decade", "surfaced": "yes",
             "where": "turn 1: Ruth said Sam is wrong", "addressed": "no"},
            {"speaker": "not stated", "concern": "an unowned worry", "surfaced": "no",
             "where": "not stated", "addressed": "no"},
        ],
        "concerns_withheld": withheld,
    })


def test_a_concern_rows_speaker_is_marked_and_its_quote_is_not():
    out = label_run_model(_concerns_model())
    rows = out["summary"]["concerns"]
    assert rows[0]["speaker"] == "(bot) Ruth"
    # "not stated" names nobody in the cast, so it is not made into a persona.
    assert rows[1]["speaker"] == "not stated"
    # `where` is the analyst's quote: prose, left exactly as written.
    assert rows[0]["where"] == "turn 1: Ruth said Sam is wrong"
    assert label_run_model(out) == out


@pytest.mark.parametrize("withheld", [False, True])
def test_the_exports_concern_table_marks_the_persona(withheld):
    md = ex.render_markdown(_concerns_model(withheld))
    row = next(line for line in md.splitlines() if "the clause binds us" in line)
    assert row.startswith("| (bot) Ruth | the clause binds us for a decade |")
    assert "| not stated | an unowned worry |" in md
    assert "(bot) not stated" not in md
    html = ex.render_html(_concerns_model(withheld))
    assert "<tr><td>(bot) Ruth</td><td>the clause binds us for a decade</td>" in html
    assert "(bot) not stated" not in html


@pytest.mark.parametrize("fmt", ["md", "html"])
@pytest.mark.parametrize("withheld", [False, True])
def test_the_briefs_concern_lines_mark_the_persona(fmt, withheld):
    b = br.run_brief(_concerns_model(withheld))
    assert any(line.startswith("(bot) Ruth (surfaced: yes; addressed: no): the clause binds us")
               for line in b["concerns"])
    assert not any("(bot) not stated" in line for line in b["concerns"])
    out = br.render(b, fmt)
    assert "(bot) Ruth (surfaced: yes; addressed: no)" in out
    assert "(bot) not stated" not in out


def test_with_every_name_marked_the_worst_case_brief_stays_one_page():
    """`test_brief`'s worst cases use names nobody in the cast has, so none of them is marked. Here the
    dissenter, the evidence row and the concerns all name the cast's one persona, on the same model, and
    the brief still fits the one-page bound of 650 (measured: 642 with assumptions, 645 without; the
    clipped lines give the marker's characters back)."""
    from tests.test_brief import _PLAN_ROW
    from tests.test_export import _run_model

    long = "a considered, specific sentence that a persona might well say in a long debate " * 4
    m = _run_model(topic=long * 3, summary={
        "overview": long * 4, "consensus": [long] * 10, "open_questions": [long] * 10, "key_ideas": [],
        "dissenters": [{"speaker": "Morgan", "position": long}] * 10,
        "evidence_plan": [{**{k: long for k in _PLAN_ROW}, "asked_by": "Morgan"}] * 10,
        "conditional_recommendation": long * 4,
        "concerns": [{"speaker": "Morgan", "concern": long, "surfaced": "no", "where": long,
                      "addressed": "no"}] * 10,
        "concerns_withheld": True},
        assumptions=[{"id": f"A{i}", "statement": long, "basis": long, "source": "operator", "turn": 0}
                     for i in range(8)])
    b = br.run_brief(m)
    assert all(line.startswith("(bot) Morgan") for line in b["concerns"] + b["dissent"])
    assert len(br.render_markdown(b).split()) < 650
    m.pop("assumptions")
    assert len(br.render_markdown(br.run_brief(m)).split()) < 650
