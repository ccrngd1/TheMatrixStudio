# SPDX-License-Identifier: Apache-2.0
"""Phase 5i tests — citation provenance (first-hand / second-hand / unverified).

The design models attributed hearsay rather than suppression: evidence
legitimately travels through people, so a persona may use a document another
persona surfaced *provided it credits them*. What must never happen is presenting
second-hand evidence as first-hand.

The most important tests here are the NEGATIVE ones. Flagging an honest mention
("I haven't seen that doc you're referencing") would punish exactly the behaviour
this feature is trying to produce, so those cases are locked explicitly.
"""

import pytest

from matrix_studio.citations import (
    CitationContext,
    analyse_citations,
    citation_violation,
    provenance_payload,
)

CAST = ["Dana", "Priya", "Marcus"]


def ctx(own=(), prior=()):
    """own: [(title, ordinal)] retrieved this turn; prior: [(speaker, title)]."""
    passages = [{"title": t, "ordinal": o} for t, o in own]
    return CitationContext.build(own_passages=passages, prior_firsthand=prior)


def kinds(utterance, speaker="Dana", context=None):
    cites = analyse_citations(utterance, speaker, CAST, context or ctx())
    return [(c.title, c.kind) for c in cites]


# --------------------------------------------------------------------------
# First-hand
# --------------------------------------------------------------------------


def test_citing_your_own_retrieved_document_is_firsthand():
    c = ctx(own=[("spec.md", 0)])
    assert kinds("Per spec.md #0, the install must stay one process.", context=c) == [
        ("spec.md", "firsthand")
    ]


def test_bracketed_citation_form_is_recognised():
    c = ctx(own=[("distribution-constraints.md", 1)])
    got = analyse_citations(
        "The constraint is in [distribution-constraints.md #1], which specifies "
        "no external service.", "Dana", CAST, c,
    )
    assert [x.kind for x in got] == ["firsthand"]
    assert got[0].ordinal == 1


def test_firsthand_without_an_ordinal():
    c = ctx(own=[("spec.md", 3)])
    assert kinds("spec.md states the constraint plainly.", context=c) == [
        ("spec.md", "firsthand")
    ]


def test_wrong_chunk_ordinal_of_a_held_document_is_unverified():
    """Right document, a chunk the speaker never actually saw."""
    c = ctx(own=[("spec.md", 0)])
    got = analyse_citations("Per spec.md #9, we must ship vectors.", "Dana", CAST, c)
    assert got[0].kind == "unverified"
    assert "retrieved only" in got[0].reason


# --------------------------------------------------------------------------
# The observed real failure
# --------------------------------------------------------------------------


def test_asserting_another_personas_document_without_credit_is_a_violation():
    """The exact failure observed in a live run.

    Priya retrieved phase4-report.md and cited it; Dana then wrote "which is what
    phase4-report.md #31 actually specifies" having never had access to it.
    """
    c = ctx(own=[("project-spec.md", 27)], prior=[("Priya", "phase4-report.md")])
    utterance = (
        "FTS5 does token matching, which is what phase4-report.md #31 actually "
        "specifies, and that's sufficient for v1."
    )
    got = analyse_citations(utterance, "Dana", CAST, c)
    assert got[0].kind == "unverified"
    assert got[0].attributive is True
    assert "surfaced by Priya" in got[0].reason
    assert citation_violation(got) is not None


def test_crediting_the_persona_who_surfaced_it_is_legitimate():
    """The fix: attributed hearsay is accepted, because that is how real
    evidence propagates through a team."""
    c = ctx(own=[("project-spec.md", 27)], prior=[("Priya", "phase4-report.md")])
    utterance = (
        "Priya cited phase4-report.md #31 as saying thread retrieval has no "
        "ranking, and I'll take that at face value."
    )
    got = analyse_citations(utterance, "Dana", CAST, c)
    assert got[0].kind == "secondhand"
    assert got[0].via == "Priya"
    assert citation_violation(got) is None


def test_crediting_someone_who_never_cited_it_is_a_violation():
    """Fabricated hearsay — the new surface this design introduces, and the most
    checkable one, since the transcript is right there."""
    c = ctx(own=[("project-spec.md", 0)], prior=[("Priya", "phase4-report.md")])
    got = analyse_citations(
        "Marcus said cost-model.md specifies a hard cap.", "Dana", CAST, c
    )
    assert got[0].kind == "unverified"
    assert "never cited it" in got[0].reason


def test_citing_a_document_no_one_has_is_a_violation():
    got = analyse_citations(
        "Per invented-standard.pdf, we are required to ship vectors.",
        "Dana", CAST, ctx(own=[("spec.md", 0)]),
    )
    assert got[0].kind == "unverified"
    assert "no participant has retrieved" in got[0].reason


# --------------------------------------------------------------------------
# Negative cases — honest talk about documents must NOT be flagged
# --------------------------------------------------------------------------


def test_disclaiming_access_is_not_a_violation():
    """Real output from a live run; flagging this would punish honesty."""
    c = ctx(own=(), prior=[("Dana", "distribution-constraints.md")])
    got = analyse_citations(
        "I haven't seen that distribution-constraints.md doc you're referencing, "
        "so I'm going from field experience.", "Simone", CAST + ["Simone"], c,
    )
    assert got[0].kind != "unverified"
    assert got[0].attributive is False
    assert citation_violation(got) is None


def test_plain_mention_without_assertion_is_not_a_violation():
    got = analyse_citations(
        "Someone should probably read spec.md before we decide.",
        "Dana", CAST, ctx(),
    )
    assert got[0].kind == "mention"
    assert citation_violation(got) is None


def test_dont_have_phrasing_is_treated_as_a_disclaimer():
    got = analyse_citations(
        "I don't have cost-observations.md in front of me, but per what I recall "
        "the overhead was small.", "Dana", CAST, ctx(),
    )
    assert citation_violation(got) is None


def test_no_citations_at_all_is_clean():
    assert analyse_citations("We should ship it this quarter.", "Dana", CAST, ctx()) == []
    assert citation_violation([]) is None


def test_non_document_words_are_not_mistaken_for_citations():
    """A sentence with dots and abbreviations must not produce phantom labels."""
    got = analyse_citations(
        "Per our SLA, e.g. the 99.9% target, we cannot ship yet.",
        "Dana", CAST, ctx(),
    )
    assert got == []


# --------------------------------------------------------------------------
# Multiple citations, and the payload
# --------------------------------------------------------------------------


def test_mixed_citations_are_classified_independently():
    c = ctx(own=[("spec.md", 0)], prior=[("Priya", "report.md")])
    got = analyse_citations(
        "Per spec.md #0 the install is fixed, and Priya cited report.md as "
        "confirming the ranking gap, though ghost.pdf supposedly requires more.",
        "Dana", CAST, c,
    )
    by_title = {x.title: x.kind for x in got}
    assert by_title["spec.md"] == "firsthand"
    assert by_title["report.md"] == "secondhand"
    assert by_title["ghost.pdf"] in ("unverified", "mention")


def test_violation_reports_the_first_attributive_offender():
    c = ctx(own=[("spec.md", 0)])
    got = analyse_citations(
        "spec.md #0 is clear. Also ghost.pdf specifies otherwise.",
        "Dana", CAST, c,
    )
    reason = citation_violation(got)
    assert reason and "ghost.pdf" in reason


def test_provenance_payload_is_serialisable_and_complete():
    c = ctx(own=[("spec.md", 0)], prior=[("Priya", "report.md")])
    got = analyse_citations(
        "Per spec.md #0 we are fixed, and Priya cited report.md as saying no.",
        "Dana", CAST, c,
    )
    payload = provenance_payload(got)
    assert {p["kind"] for p in payload} == {"firsthand", "secondhand"}
    second = next(p for p in payload if p["kind"] == "secondhand")
    assert second["via"] == "Priya"
    assert second["label"].startswith("report.md")
    # Must survive JSON round-tripping into the event log.
    import json
    assert json.loads(json.dumps(payload)) == payload


def test_citation_label_formatting():
    c = ctx(own=[("spec.md", 4)])
    got = analyse_citations("Per spec.md #4 it is settled.", "Dana", CAST, c)
    assert got[0].label == "spec.md #4"
    got2 = analyse_citations("spec.md states it.", "Dana", CAST, ctx(own=[("spec.md", 0)]))
    assert got2[0].label == "spec.md"


def test_context_build_is_case_insensitive():
    c = ctx(own=[("Spec.MD", 0)])
    assert kinds("Per spec.md #0, fine.", context=c) == [("spec.md", "firsthand")]
