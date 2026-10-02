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
