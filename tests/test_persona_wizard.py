# SPDX-License-Identifier: Apache-2.0
"""Tests for the persona wizard — drafting a cast from a short brief.

This is authoring assistance, not simulation: the output is a draft the operator
edits and it never starts a run by itself. So the tests are about *robustness of the
draft*, not about content quality, which no test can judge.

The properties that matter:

- A draft that would be rejected at run creation is worse than no draft, because the
  operator only finds out after filling in a form. So anything that fails
  `StructuredPersona` validation is dropped here.
- Nothing is silently empty. A wizard that returns `[]` on failure looks like a
  feature that does nothing; it raises with a reason instead.
- The malformed shapes a real model produces are handled, including the
  markdown-fenced JSON that silently disabled two features earlier in this project.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio.persona_wizard import (
    DEFAULT_PERSONAS,
    MAX_PERSONAS,
    MIN_PERSONAS,
    WizardError,
    suggest_cast,
)


def _resp(content: str):
    return MagicMock(choices=[MagicMock(message=MagicMock(content=content))])


def _member(name="Dana", **over):
    m = {
        "name": name,
        "role": "Head of Distribution",
        "persona": "Pragmatic, protective of the install story.",
        "goals": ["Protect time-to-first-run"],
        "optimises_for": ["time-to-first-run"],
        "dismisses": ["retrieval answer quality"],
        "persuaded_by": ["a clean-machine install"],
        "formative_event": {"year": 2023, "event": "A quickstart needed a vector DB",
                            "lesson": "Extra services cost you users"},
        "position": "No feature may add an external service",
        "firmness": "firm",
        "evidence_that_shifts": ["an embedded index that is a file"],
        "underlying_concern": "I own it when a customer never gets a working run",
    }
    m.update(over)
    return m


def _payload(*members):
    return json.dumps({"cast": list(members)})


async def _run(content, **kw):
    with patch("matrix_studio.persona_wizard.litellm.acompletion",
               return_value=_resp(content)):
        return await suggest_cast(brief="Should we add retrieval?", **kw)


# --------------------------------------------------------------------------
# The happy path, and that it produces something the API will actually accept
# --------------------------------------------------------------------------


async def test_drafts_a_cast_in_the_shape_the_run_api_takes():
    cast = await _run(_payload(_member("Dana"), _member("Marcus")))
    assert [c["name"] for c in cast] == ["Dana", "Marcus"]
    vp = cast[0]["structured"]["viewpoints"][0]
    assert vp["firmness"] == "firm"
    assert vp["evidence_that_shifts"] == ["an embedded index that is a file"]
    assert cast[0]["structured"]["preferences"]["dismisses"] == ["retrieval answer quality"]
    assert cast[0]["structured"]["background"]["formative_events"][0]["year"] == 2023


async def test_the_draft_validates_against_the_real_persona_model():
    """A draft that 422s at run creation is worse than no draft."""
    from matrix_studio.personas import parse_structured

    cast = await _run(_payload(_member()))
    assert parse_structured(cast[0]["structured"]) is not None


async def test_it_generates_the_withheld_concern():
    """The concern is the hardest field to author and the operator is meant to see
    it — it is withheld from the CONVERSATION, not from the person building the panel."""
    cast = await _run(_payload(_member()))
    assert cast[0]["structured"]["viewpoints"][0]["underlying_concern"]


# --------------------------------------------------------------------------
# Malformed model output
# --------------------------------------------------------------------------


async def test_handles_markdown_fenced_json():
    """The exact failure that silently disabled cognition and the 4a gate earlier in
    this project. The wizard uses the same tolerant parser."""
    cast = await _run("```json\n" + _payload(_member()) + "\n```")
    assert len(cast) == 1


async def test_drops_entries_missing_a_name_or_a_voice():
    """Not a draft an operator can edit — noise to delete."""
    cast = await _run(_payload(
        _member("Dana"), _member("", persona="x"), _member("Nameless", persona=""),
    ))
    assert [c["name"] for c in cast] == ["Dana"]


async def test_an_unknown_firmness_becomes_negotiable_rather_than_dropping_the_persona():
    """Safe direction: a position wrongly negotiable merely gets argued, while one
    wrongly firm becomes an immovable wall."""
    cast = await _run(_payload(_member(firmness="rock solid")))
    assert cast[0]["structured"]["viewpoints"][0]["firmness"] == "negotiable"


async def test_tolerates_a_string_where_a_list_was_asked_for():
    cast = await _run(_payload(_member(dismisses="cost", goals="ship it")))
    assert cast[0]["structured"]["preferences"]["dismisses"] == ["cost"]
    assert cast[0]["goals"] == ["ship it"]


async def test_tolerates_a_missing_or_malformed_formative_event():
    cast = await _run(_payload(
        _member("A", formative_event=None), _member("B", formative_event="not a dict"),
        _member("C", formative_event={"event": "no year given"}),
    ))
    assert len(cast) == 3
    assert cast[0]["structured"]["background"]["formative_events"] == []
    assert cast[2]["structured"]["background"]["formative_events"][0]["year"] is None


async def test_a_persona_with_no_position_still_drafts():
    """Prose and preferences are useful on their own; the operator can add a position."""
    cast = await _run(_payload(_member(position="")))
    assert cast[0]["structured"]["viewpoints"] == []


async def test_deduplicates_names():
    """Duplicate names collide in the engine's agent dict and would silently drop a
    persona at run start."""
    cast = await _run(_payload(_member("Dana"), _member("dana"), _member("Marcus")))
    assert [c["name"] for c in cast] == ["Dana", "Marcus"]


# --------------------------------------------------------------------------
# Failures are loud, with a reason
# --------------------------------------------------------------------------


async def test_an_empty_brief_is_rejected_before_spending_a_call():
    with patch("matrix_studio.persona_wizard.litellm.acompletion") as call:
        with pytest.raises(WizardError, match="Describe the situation"):
            await suggest_cast(brief="   ")
        call.assert_not_called()


async def test_unusable_output_raises_with_a_reason_rather_than_returning_empty():
    """Returning [] would look like a feature that does nothing."""
    for content in ("not json at all", '{"something_else": []}', '{"cast": "nope"}'):
        with pytest.raises(WizardError, match="usable cast"):
            await _run(content)


async def test_a_cast_of_only_unusable_entries_raises():
    with pytest.raises(WizardError, match="No usable personas"):
        await _run(_payload({"junk": True}, _member("", persona="")))


async def test_an_api_failure_surfaces_the_reason():
    with patch("matrix_studio.persona_wizard.litellm.acompletion",
               side_effect=RuntimeError("throttled")):
        with pytest.raises(WizardError, match="throttled"):
            await suggest_cast(brief="anything")


# --------------------------------------------------------------------------
# Count handling
# --------------------------------------------------------------------------


async def test_count_is_clamped_to_a_workable_panel_size():
    """Below 2 nobody can disagree; above 7 each persona gets too few turns for a
    position to be challenged and held."""
    many = _payload(*[_member(f"P{i}") for i in range(12)])
    assert len(await _run(many, count=99)) == MAX_PERSONAS
    assert len(await _run(many, count=0)) >= MIN_PERSONAS


async def test_returns_no_more_than_requested_even_if_the_model_overshoots():
    over = _payload(*[_member(f"P{i}") for i in range(6)])
    assert len(await _run(over, count=3)) == 3


async def test_the_prompt_asks_for_the_two_calibration_properties():
    """Neither can be verified by a test — they are judgments about content — so what
    is checked is that the prompt actually requests them. Without both, a generated
    panel converges or teaches nothing (docs/PHASE5-PREMISE-VALIDATION.md)."""
    with patch("matrix_studio.persona_wizard.litellm.acompletion",
               return_value=_resp(_payload(_member()))) as call:
        await suggest_cast(brief="a brief", count=DEFAULT_PERSONAS)
    prompt = call.call_args.kwargs["messages"][0]["content"]
    assert "Do NOT make the firmly-held positions the correct ones" in prompt
    assert "MUST differ substantially" in prompt
    assert "must actually conflict" in prompt


# --------------------------------------------------------------------------
# Truncation salvage
#
# This is not a hypothetical: the first live call returned five well-formed
# personas cut off mid-object, and the strict parse rejected the whole document.
# Discarding four complete personas because a fifth was clipped is a worse
# outcome than a short cast.
# --------------------------------------------------------------------------


async def test_salvages_complete_personas_from_a_truncated_reply():
    full = _payload(_member("Dana"), _member("Marcus"), _member("Priya"))
    # Cut a few characters PAST the third object's opening brace, which is what a
    # max_tokens stop does. The first fixture for this cut past the object's end
    # instead and so tested nothing — worth stating, because a salvage test that
    # feeds valid JSON silently passes.
    truncated = full[: full.index('"Priya"') + 12]
    assert not truncated.endswith("}"), "fixture is not actually truncated"
    cast = await _run(truncated)
    assert [c["name"] for c in cast] == ["Dana", "Marcus"]


async def test_salvage_is_not_confused_by_braces_inside_prose():
    """A brace in a string literal must not desynchronise the scanner."""
    tricky = _member("Dana", persona="She calls it the {big rewrite} and won't drop it.")
    full = _payload(tricky, _member("Marcus"))
    cast = await _run(full[: full.index('"Marcus"') + 12])
    assert [c["name"] for c in cast] == ["Dana"]
    assert "{big rewrite}" in cast[0]["persona"]


async def test_salvage_ignores_nested_objects_that_are_not_personas():
    """`formative_event` is a nested object with no name/persona; the shape filter is
    what stops it being collected as a cast member alongside the real one."""
    full = _payload(_member("Dana"))
    cast = await _run(full[:-2])  # clip the closing wrapper braces
    assert [c["name"] for c in cast] == ["Dana"]


async def test_a_truncated_reply_with_no_complete_persona_still_raises():
    cast_json = _payload(_member("Dana"))
    with pytest.raises(WizardError, match="usable cast"):
        await _run(cast_json[:60])


async def test_the_token_budget_is_large_enough_to_matter():
    """Regression on a real failure: max_tokens=4000 truncated a 5-persona draft and
    produced a total failure caused only by a budget."""
    with patch("matrix_studio.persona_wizard.litellm.acompletion",
               return_value=_resp(_payload(_member()))) as call:
        await suggest_cast(brief="a brief")
    assert call.call_args.kwargs["max_tokens"] >= 12000
