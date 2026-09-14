# SPDX-License-Identifier: Apache-2.0
"""
The literal-newline failure: a complete cognition reply that strict JSON rejects.

A persona told to answer in "2-4 paragraphs" writes those paragraphs inside the
``utterance`` string, and models separate them with a real newline rather than ``\\n``.
`json.loads` with its default ``strict=True`` raises "Invalid control character" on that,
so the whole object — rationale, memories, goal and relationship updates — was discarded.

Measured 2026-09-14 against `bedrock/global.anthropic.claude-sonnet-5` in json_object mode
with this project's own cognition schema: **2 of 5 replies were rejected, and both parsed
under `strict=False`**. One recovered all six fields.

The consequence in run `2d2ac45b`: cognition configured on, rationale present on 4 of 24
turns, 3 memories in the whole conversation, and a transcript that read as if the feature
were switched off. The fixtures below are shaped like the replies that actually failed.
"""

import json

import pytest

from matrix_studio.jsonio import extract_json_object, repair_truncated_object


def test_a_multi_paragraph_utterance_with_real_newlines_parses():
    """The exact shape that failed: paragraph breaks inside the utterance string."""
    raw = (
        '{"utterance": "First paragraph, in character.\n\n'
        'Second paragraph, still in character.", '
        '"rationale": "why I said it", "goal_served": "my goal"}'
    )
    # The premise: this is genuinely invalid strict JSON, so the test is not vacuous.
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)

    parsed = extract_json_object(raw)
    assert parsed is not None, "a complete reply was discarded over a paragraph break"
    assert parsed["rationale"] == "why I said it"
    assert parsed["goal_served"] == "my goal"
    assert "Second paragraph" in parsed["utterance"]


def test_every_cognition_field_survives():
    """Named separately: the utterance surviving is not the point.

    The utterance survives even without the fix — the engine falls back to the raw text
    — so a test that only checked the speech would pass with the bug in place. What was
    being lost is the structured half.
    """
    raw = (
        '{"utterance": "Line one.\n\nLine two.", "rationale": "r", "goal_served": "g", '
        '"memories": [{"content": "m", "importance": 0.5, "tags": ["t"]}], '
        '"goal_update": ["new goal"], "relationship_updates": {"Ada": "wary"}}'
    )
    parsed = extract_json_object(raw)
    assert parsed is not None
    assert sorted(parsed) == [
        "goal_served", "goal_update", "memories", "rationale",
        "relationship_updates", "utterance",
    ]
    assert parsed["memories"][0]["content"] == "m"
    assert parsed["relationship_updates"] == {"Ada": "wary"}


def test_a_tab_inside_a_string_parses_too():
    """Newlines are the common case; the rule being relaxed covers control characters."""
    assert extract_json_object('{"utterance": "a\tb"}') == {"utterance": "a\tb"}


def test_a_fenced_multi_paragraph_reply_parses():
    """Both known formatting quirks at once, which is what a real reply looks like."""
    raw = '```json\n{"utterance": "One.\n\nTwo.", "rationale": "r"}\n```'
    parsed = extract_json_object(raw)
    assert parsed is not None and parsed["rationale"] == "r"


def test_truncation_repair_keeps_completed_fields_despite_newlines():
    """The repair path parses too, so it needed the same relaxation.

    A reply cut off at `max_tokens` still contains the newlines the model wrote in the
    fields it DID finish, so a strict repair rejects exactly the data it exists to save.
    """
    raw = (
        '{"utterance": "Para one.\n\nPara two.", "rationale": "complete", '
        '"memories": [{"content": "half-writ'
    )
    parsed = repair_truncated_object(raw)
    assert parsed is not None, "truncation repair dropped two complete fields"
    assert parsed["rationale"] == "complete"
    assert "Para two" in parsed["utterance"]
    # Deliberately partial: the caller can see `memories` is missing rather than being
    # handed an empty list it cannot distinguish from "the model formed no memories".
    assert "memories" not in parsed


def test_genuinely_unparseable_still_returns_none():
    """The relaxation must not turn "no JSON here" into a false positive."""
    assert extract_json_object("I am not going to answer in JSON, sorry.") is None
    assert extract_json_object("") is None
    assert extract_json_object('["a", "list"]') is None


def test_a_malformed_member_is_salvaged_as_a_partial_object_not_invented():
    """`strict=False` relaxes control characters and nothing else.

    A member with no value looks exactly like a truncated reply, so the repair path
    salvages the fields that ARE complete and drops the broken one. Asserted rather than
    assumed: my first version of this test expected None, and the partial object is the
    better answer — it hands the caller what the model actually finished.
    """
    parsed = extract_json_object('{"utterance": "x", "rationale": }')
    assert parsed == {"utterance": "x"}, "salvage should keep complete fields only"
