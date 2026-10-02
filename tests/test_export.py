# SPDX-License-Identifier: Apache-2.0
"""
Exporting a run or an ensemble — `matrix_studio/export.py`.

Most of these assert something an export must NOT do, because a flat file loses exactly the things
the UI enforces with structure and styling:

- **flatten an ensemble's cells.** No pooled total: the same "5 of 9" is a strong method-dependent
  finding or a coin flip depending on how it splits (`ENSEMBLE-CONVERSATIONS.md` §4). The UI's
  `ClaimTable.test.tsx` asserts the absence there; this asserts it in both export formats.
- **present analysis as record.** Model-generated summaries and syntheses are labelled in WORDS.
- **leak operator-private persona fields.** Checked by VALUE, not by key name: a leak would carry
  the text, and a grep for "underlying_concern" would pass while the concern itself was printed.
- **run model text as HTML.** A transcript is model output and the export is a file a browser opens.
"""

import pytest

from matrix_studio import export as ex
from tests.support import TEST_OWNER

CONCERN = "I wrote this and my name is on the line if a board disagrees"
VALIDITY = "operator note: this position is weaker than she thinks"


def _run_model(**over):
    m = {
        "kind": "run", "id": "r1", "name": "renewal, take 2", "description": "d", "topic": "t",
        "status": "complete", "created_at": 1790000000, "turn_count": 2, "cost_usd": 0.14,
        "converged": {"at_turn": 2, "reason": "declined"},
        "cast": ex.public_cast([{
            "name": "Morgan", "persona": "A provider", "goals": ["launch it"],
            "structured": {"role": "author", "viewpoints": [{
                "position": "Renewal is continuation", "firmness": "firm",
                "evidence_that_shifts": ["a board action"],
                "underlying_concern": CONCERN, "validity": VALIDITY,
            }]},
        }]),
        "settings": ["Turn budget: 2"],
        "research": {"status": "researched", "provider": "tavily", "cost_usd": 0.26,
                     "scopes": [{"scope": "shared", "documents": 20, "controlling": 4,
                                 "negative": False, "query_negatives": 2}]},
        "transcript": [
            {"turn": 1, "speaker": "Morgan", "message": "Line one.\nLine two.",
             "passages": [{"title": "Iowa Ch. 811", "ordinal": 4, "origin": "researched",
                           "authority": "controlling"}]},
        ],
        "summary": {"overview": "They argued.", "key_ideas": ["continuation"],
                    "consensus": [], "open_questions": ["is it legal"],
                    "dissenters": [{"speaker": "Riley", "position": "no"}]},
        "exported_at": 1790000100,
    }
    m.update(over)
    return m


def _ensemble_model(**over):
    m = {
        "kind": "ensemble", "id": "e1", "name": "renewal-cells", "description": None, "topic": "t",
        "status": "complete", "created_at": 1790000000, "cells": ["base", "hybrid"],
        "spec": [{"label": "base", "n": 5, "overrides": {}},
                 {"label": "hybrid", "n": 4, "overrides": {"selection.method": "hybrid"}}],
        "members": [{"cell": "base", "index": 1, "name": "b1", "status": "complete",
                     "turn_count": 40, "cost_usd": 0.95}],
        "research": None,
        "report": {
            "claims": [{"claim": "labwork required", "kind": "demand", "per_cell": {
                "base": {"held": 5, "of": 5, "tier": "unanimous"},
                "hybrid": {"held": 0, "of": 4, "tier": "absent"},
            }}, {"claim": "only in base", "kind": "refusal", "per_cell": {
                "base": {"held": 1, "of": 5, "tier": "rare"},
            }}],
            "agreements": {"unresolved_by_frequency": [{"claim": "is it legal", "runs": ["b1", "b2"]}]},
            "synthesis": "## Across runs\nIt held.",
            "caveats": ["Claim counts are a FLOOR."],
        },
        "exported_at": 1790000100,
    }
    m.update(over)
    return m


# --------------------------------------------------------------------------- #
# The cells are never pooled
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fmt", ["md", "html"])
def test_the_claim_table_has_one_column_per_group_and_no_total(fmt):
    out = ex.render(_ensemble_model(), fmt)
    # The counts a reader must see, per group.
    assert "5 of 5" in out and "0 of 4" in out
    # And the one they must not: pooled, "labwork required" is 5 of 9 — a weak split, which is
    # the opposite of what it is.
    assert "5 of 9" not in out
    assert "6 of 9" not in out


def test_the_markdown_claim_header_is_exactly_the_groups():
    header = next(l for l in ex.render_markdown(_ensemble_model()).splitlines()
                  if l.startswith("| claim"))
    assert header == "| claim | kind | base | hybrid |"


def test_a_group_that_produced_nothing_for_a_claim_is_a_dash_not_zero_of_n():
    # "0 of 4" is a finding — four runs, none held it. A group with no entry at all is absence of
    # evidence, and rendering it as "0 of N" would invent a denominator.
    out = ex.render_markdown(_ensemble_model())
    row = next(l for l in out.splitlines() if l.startswith("| only in base"))
    assert row.endswith("| 1 of 5 | — |")


# --------------------------------------------------------------------------- #
# Analysis is labelled in words
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fmt", ["md", "html"])
def test_a_run_summary_is_labelled_as_model_analysis(fmt):
    out = ex.render(_run_model(), fmt)
    assert ex.ANALYSIS_LABEL.split(".")[0] in out.replace("&#x27;", "'")
    # The label comes BEFORE the analysis it describes, so a reader meets it first.
    assert out.index("Model-generated analysis") < out.index("They argued.")


@pytest.mark.parametrize("fmt", ["md", "html"])
def test_an_ensemble_synthesis_is_labelled_too(fmt):
    out = ex.render(_ensemble_model(), fmt)
    assert out.index("Model-generated analysis") < out.index("It held.")


def test_no_summary_means_no_summary_section_and_no_label():
    out = ex.render_markdown(_run_model(summary=None))
    assert "## Summary" not in out
    assert "Model-generated analysis" not in out


# --------------------------------------------------------------------------- #
# Private persona fields never leave
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fmt", ["md", "html"])
def test_underlying_concern_and_validity_never_appear_by_VALUE(fmt):
    out = ex.render(_run_model(), fmt)
    assert CONCERN not in out
    assert VALIDITY not in out
    # And the public part of the same viewpoint IS there, so this is not passing vacuously.
    assert "Renewal is continuation" in out
    assert "a board action" in out


# --------------------------------------------------------------------------- #
# HTML never runs model text
# --------------------------------------------------------------------------- #


def test_model_text_is_escaped_in_html():
    hostile = '<script>alert(1)</script><img src=x onerror=alert(2)>'
    m = _run_model(
        name=hostile,
        transcript=[{"turn": 1, "speaker": hostile, "message": hostile, "passages": [
            {"title": hostile, "ordinal": 0, "origin": None, "authority": None}]}],
        summary={"overview": hostile, "key_ideas": [hostile], "consensus": [],
                 "open_questions": [], "dissenters": [{"speaker": hostile, "position": hostile}]},
    )
    out = ex.render_html(m)
    assert "<script>alert" not in out
    assert "<img src=x" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out


def test_ensemble_html_escapes_the_synthesis_and_claims():
    hostile = "<script>x()</script>"
    rep = _ensemble_model()["report"]
    rep["synthesis"] = hostile
    rep["claims"][0]["claim"] = hostile
    out = ex.render_html(_ensemble_model(report=rep))
    assert "<script>x()" not in out


def test_a_pipe_in_model_text_cannot_break_a_markdown_table():
    rep = _ensemble_model()["report"]
    rep["claims"][0]["claim"] = "a | b"
    out = ex.render_markdown(_ensemble_model(report=rep))
    row = next(l for l in out.splitlines() if l.startswith("| a"))
    assert "a \\| b" in row
    assert row.count(" | ") == 3   # claim, kind, base, hybrid — four cells, three separators


def test_the_html_carries_its_own_print_stylesheet():
    """PDF is the browser printing this file, so the file has to know how to be printed."""
    assert "@media print" in ex.render_html(_run_model())


def test_line_breaks_in_a_message_survive_html():
    out = ex.render_html(_run_model())
    assert "Line one.\nLine two." in out
    assert "white-space:pre-wrap" in out


# --------------------------------------------------------------------------- #
# The run model
# --------------------------------------------------------------------------- #


async def test_passages_are_attached_to_the_message_that_used_them(db):
    """A turn's retrieval precedes its response, and simultaneous mode can put several speakers in
    one turn — so passages are keyed (turn, speaker), not turn alone."""
    await db.create_run(run_id="x1", topic="t", cast=[{"name": "A"}, {"name": "B"}], name="x1",
                        config={}, owner_sub=TEST_OWNER)
    for seq, (etype, payload) in enumerate([
        ("document.retrieved", {"speaker": "A", "passages": [{"title": "for A", "ordinal": 0}]}),
        ("document.retrieved", {"speaker": "B", "passages": [{"title": "for B", "ordinal": 1}]}),
        ("agent.response", {"speaker": "A", "message": "A speaks"}),
        ("agent.response", {"speaker": "B", "message": "B speaks"}),
    ]):
        await db.append_event("x1", turn=1, seq=seq, event_type=etype, payload=payload,
                              agent_name=payload["speaker"])
    model = await ex.run_model(db, await db.get_run("x1"))
    by = {t["speaker"]: [p["title"] for p in t["passages"]] for t in model["transcript"]}
    assert by == {"A": ["for A"], "B": ["for B"]}


def test_filenames_are_safe_and_say_what_they_are():
    assert ex.filename(_run_model(), "md") == "renewal-take-2-run.md"
    assert ex.filename(_ensemble_model(), "html") == "renewal-cells-ensemble.html"
    assert ex.filename(_run_model(name="../../etc/passwd"), "md") == "etc-passwd-run.md"


def test_an_unknown_format_is_refused():
    with pytest.raises(ValueError, match="md"):
        ex.render(_run_model(), "pdf")


# --------------------------------------------------------------------------- #
# Conclusions and agreements (the ensemble export)
# --------------------------------------------------------------------------- #


def _concl(claim, **cells):
    return {"claim": claim, "kind": "conclusion", "per_cell": {
        c: {"held": h, "of": o, "tier": "unanimous" if h == o else ("rare" if h == 1 else "split")}
        for c, (h, o) in cells.items()}}


def _with(conclusions):
    rep = _ensemble_model()["report"]
    if conclusions is not None:
        rep["conclusions"] = conclusions
    return _ensemble_model(report=rep)


@pytest.mark.parametrize("fmt", ["md", "html"])
def test_conclusions_come_before_the_claim_table_and_per_group(fmt):
    out = ex.render(_with([_concl("exclude California", base=(4, 5), hybrid=(1, 4))]), fmt)
    assert out.index("What the runs concluded") < out.index("Claims, per group")
    assert "4 of 5" in out and "1 of 4" in out
    assert "5 of 9" not in out, "a conclusion must never be pooled across groups"


def test_a_report_that_predates_conclusions_says_so_and_is_not_read_as_none():
    out = ex.render_markdown(_with(None))
    assert "built before conclusions were extracted" in out
    assert "No run reached a conclusion" not in out


def test_no_conclusions_at_all_is_reported_as_the_finding():
    assert "No run reached a conclusion" in ex.render_markdown(_with([]))


def test_divergence_is_reported_rather_than_a_conclusion_promoted():
    out = ex.render_markdown(_with([_concl("exclude CA", base=(1, 5)),
                                    _concl("launch everywhere", base=(1, 5))]))
    assert "No conclusion recurred" in out
    assert "divergence is the result" in out


def test_a_recurring_conclusion_gets_no_divergence_note():
    out = ex.render_markdown(_with([_concl("exclude CA", base=(3, 5))]))
    assert "No conclusion recurred" not in out


def test_agreements_are_the_claims_unanimous_in_a_group():
    out = ex.render_markdown(_ensemble_model())
    section = out[out.index("What every run in a group agreed on"):out.index("Claims, per group")]
    # "labwork required" is 5 of 5 in base: unanimous there, and only there.
    assert "labwork required — base: all 5 runs" in section
    assert "hybrid" not in section


@pytest.mark.parametrize("fmt", ["md", "html"])
def test_conclusions_are_labelled_as_model_analysis(fmt):
    out = ex.render(_with([_concl("exclude CA", base=(3, 5))]), fmt)
    start = out.index("What the runs concluded")
    assert "Model-generated analysis" in out[start:start + 600]


def test_single_run_conclusions_are_separated_from_recurring_ones():
    """Measured on renewal-cells: 35 of 41 conclusions came from one run, and listed together they
    buried the six that recurred. A one-run conclusion is `rare` — not a finding — and says so."""
    out = ex.render_markdown(_with([
        _concl("exclude net-new", base=(2, 5), hybrid=(2, 3)),
        _concl("file Ohio certification today", base=(1, 5)),
    ]))
    rec = out.index("Reached in two or more runs")
    one = out.index("Never reached twice within a group (1)")
    assert rec < one
    assert out.index("exclude net-new") < one < out.index("file Ohio certification today")
    assert "rare, not findings" in out


@pytest.mark.parametrize("fmt", ["md", "html"])
def test_the_report_carries_the_full_evidence_plan(fmt):
    row = {"data": "pilot churn", "asked_by": "Dana", "decision": "launch or hold",
           "moves_them": "under 5%", "best_guess": "not stated", "cheapest_way": "a 2-week pilot"}
    m = _run_model(summary={"overview": "o", "evidence_plan": [row],
                            "conditional_recommendation": "If under 5%, launch."})
    out = ex.render(m, fmt)
    assert "What would settle it" in out and "If under 5%, launch." in out
    # The report has every column, including the two the one-page brief leaves out.
    assert "launch or hold" in out and "a 2-week pilot" in out and "not stated" in out


# --------------------------------------------------------------------------- #
# Underlying concerns: shown with the cast only when stated plainly; read by the summary in both modes
# --------------------------------------------------------------------------- #

_CAST = [{"name": "Morgan", "persona": "A provider", "goals": [], "structured": {"viewpoints": [{
    "position": "Renewal is continuation", "firmness": "firm", "evidence_that_shifts": ["a board action"],
    "underlying_concern": CONCERN, "validity": VALIDITY}]}}]

CONCERN_ROW = {"speaker": "Morgan", "concern": CONCERN, "surfaced": "partly",
               "where": "turn 4: “my name is on it”", "addressed": "no"}


@pytest.mark.parametrize("fmt", ["md", "html"])
def test_a_plain_runs_cast_shows_each_concern_with_its_position_but_never_validity(fmt):
    out = ex.render(_run_model(cast=ex.public_cast(_CAST, withhold_concerns=False)), fmt)
    assert CONCERN in out.replace("&#x27;", "'")
    assert "really worried about" in out
    assert VALIDITY not in out


def test_public_cast_withholds_by_default():
    """A caller that does not say which mode the run was in cannot print a hidden agenda."""
    assert "underlying_concern" not in ex.public_cast(_CAST)[0]["viewpoints"][0]
    assert "validity" not in ex.public_cast(_CAST, withhold_concerns=False)[0]["viewpoints"][0]


@pytest.mark.parametrize("personas, shown", [
    ({"enabled": True, "withhold_concerns": False}, True),
    ({"enabled": True, "withhold_concerns": True}, False),
    # A run from before 2026-10-02 has no key, and withheld.
    ({"enabled": True}, False),
])
async def test_the_run_model_reads_the_mode_from_the_runs_own_config(db, personas, shown):
    await db.create_run(run_id="c1", topic="t", cast=_CAST, name="c1", config={"personas": personas},
                        owner_sub=TEST_OWNER)
    model = await ex.run_model(db, await db.get_run("c1"))
    vp = model["cast"][0]["viewpoints"][0]
    assert ("underlying_concern" in vp) is shown
    assert "validity" not in vp


@pytest.mark.parametrize("fmt", ["md", "html"])
@pytest.mark.parametrize("withheld, heading", [
    (True, "Underlying concerns (hidden during the run)"),
    (False, "Underlying concerns"),
])
def test_the_report_carries_the_summarys_concerns_labelled_by_mode(fmt, withheld, heading):
    m = _run_model(summary={"overview": "o", "concerns": [CONCERN_ROW], "concerns_withheld": withheld})
    out = ex.render(m, fmt).replace("&#x27;", "'")
    assert heading in out
    if not withheld:
        assert "(hidden during the run)" not in out
    # The analysis section is where it appears, after the label that says it is analysis.
    assert out.index("Model-generated analysis") < out.index(heading)
    # Every column, the quote included.
    assert CONCERN in out and "partly" in out and "my name is on it" in out


def test_a_withheld_runs_concern_appears_only_in_the_labelled_summary_section():
    """The reveal is the summary's, in its own labelled section — never the cast's."""
    m = _run_model(summary={"overview": "o", "concerns": [CONCERN_ROW], "concerns_withheld": True})
    out = ex.render_markdown(m)
    assert out.count(CONCERN) == 1
    assert out.index("### Underlying concerns (hidden during the run)") < out.index(CONCERN)


def test_concern_text_is_escaped_and_cannot_break_a_table():
    hostile = "<script>alert(1)</script> | x"
    m = _run_model(summary={"overview": "o", "concerns": [{**CONCERN_ROW, "concern": hostile}]})
    assert "<script>alert" not in ex.render_html(m)
    row = next(line for line in ex.render_markdown(m).splitlines() if line.startswith("| Morgan"))
    assert "\\| x" in row


def test_no_concerns_means_no_concerns_section():
    out = ex.render_markdown(_run_model())
    assert "Underlying concerns" not in out
