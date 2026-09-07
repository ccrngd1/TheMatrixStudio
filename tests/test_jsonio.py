# SPDX-License-Identifier: Apache-2.0
"""Tests for tolerant JSON extraction — the parser whose absence silently disabled
two shipped features.

Measured 2026-09-06 on a 30-turn cognition run against Claude Haiku 4.5: **30 of 30
turns** returned markdown-fenced JSON, every one failed `json.loads`, and the
consequences were invisible because both affected code paths degrade quietly:

- **Cognition became completely inert** — 0 memories, 0 reflections, 0 rationales
  across five personas and 30 turns, while the transcript carried fenced JSON blobs
  as utterances and the run cost *more* than not using cognition.
- **The Phase 4a validation gate dropped every suspicion** — its confirmation call
  fails open, so a JSONDecodeError became `violation: False`.

So the first test here is the exact observed payload shape. If a future refactor
reintroduces a strict parse, that test fails rather than a feature quietly dying.
"""

import json

import pytest

from matrix_studio.jsonio import extract_json_object

# The literal shape observed in the broken run, reproduced from a real transcript.
OBSERVED = '''```json
{
  "utterance": "I want to be direct: I've watched this exact question kill two deals.",
  "rationale": "This is the customer signal I own.",
  "goal_served": "Protect the deal",
  "memories": [{"content": "Two deals died at document ingest", "importance": 0.9, "tags": ["deal"]}]
}
```'''


# --------------------------------------------------------------------------
# The regression that motivated the module
# --------------------------------------------------------------------------


def test_the_exact_observed_fenced_payload_parses():
    """The literal failure. 30 of 30 turns looked like this."""
    obj = extract_json_object(OBSERVED)
    assert obj is not None, "the observed real-world payload still does not parse"
    assert obj["utterance"].startswith("I want to be direct")
    assert obj["rationale"] == "This is the customer signal I own."
    assert obj["memories"][0]["importance"] == 0.9


def test_a_strict_parse_would_have_failed_on_it():
    """Documents WHY the module exists, so the tolerance is not later removed as
    unnecessary. If this ever passes, the model changed, not the requirement."""
    with pytest.raises(json.JSONDecodeError):
        json.loads(OBSERVED)


# --------------------------------------------------------------------------
# The shapes a model actually produces
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '  {"a": 1}  ',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        '```JSON\n{"a": 1}\n```',
        'Here is the object you asked for:\n{"a": 1}',
        '{"a": 1}\nHope that helps!',
        '```json\n{"a": 1}',            # unclosed fence (truncated at max_tokens)
        '```json{"a": 1}```',           # no newlines
        '\n\n```json\n{"a": 1}\n```\n', # leading/trailing blank lines
    ],
)
def test_shapes_that_must_parse(text):
    assert extract_json_object(text) == {"a": 1}, repr(text)


def test_nested_objects_survive_the_widest_brace_span():
    """The last-resort brace match must be greedy, or a nested object would be cut
    short and produce a partial parse rather than a clean failure."""
    obj = extract_json_object('prose {"a": {"b": {"c": 3}}, "d": 4} more prose')
    assert obj == {"a": {"b": {"c": 3}}, "d": 4}


def test_multiline_string_values_survive():
    """Utterances contain newlines — every real turn does."""
    obj = extract_json_object('```json\n{"utterance": "line one\\nline two"}\n```')
    assert obj["utterance"] == "line one\nline two"


# --------------------------------------------------------------------------
# Failure must be None, never an exception and never a non-dict
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", [None, "", "   ", "not json at all", "```json\nnot json\n```", "{oops"]
)
def test_unparseable_returns_none(text):
    assert extract_json_object(text) is None


@pytest.mark.parametrize("text", ["[1, 2, 3]", '"a string"', "42", "true", "null"])
def test_non_objects_return_none(text):
    """Callers do `.get(...)` on the result, so a list or scalar must not come back
    as success — that would push the failure downstream into an AttributeError."""
    assert extract_json_object(text) is None


def test_never_raises_on_hostile_input():
    """This sits on the hot path of every cognition turn and of the validation gate.
    A parser that can throw turns a formatting quirk into a failed run."""
    for text in ("`" * 5000, "{" * 5000, "```json\n" + "{" * 2000, "\x00\x01"):
        assert extract_json_object(text) is None


# --------------------------------------------------------------------------
# The call sites that had reimplemented this, now delegating
# --------------------------------------------------------------------------


def test_analysis_extract_json_delegates():
    """analysis.py grew its own version in Phase 1.5 while the engine never learned
    the lesson. One implementation now, so it cannot be learned twice and missed."""
    from matrix_studio.analysis import _extract_json

    assert _extract_json(OBSERVED) is not None
    assert _extract_json("garbage") is None


def test_naming_extract_json_delegates():
    """naming.py had hand-rolled fence stripping. Same lesson, third copy."""
    from matrix_studio.naming import _extract_json as naming_extract

    assert naming_extract('```json\n{"name": "trusted-robot"}\n```') == {
        "name": "trusted-robot"
    }
    assert naming_extract("garbage") is None


# --------------------------------------------------------------------------
# Sibling defect from the same session: provider parameter restrictions
# --------------------------------------------------------------------------


def test_litellm_drop_params_is_enabled():
    """`bedrock/global.anthropic.claude-sonnet-5` accepts ONLY temperature=1, and the
    engine passes 0.7 (settings), 0.3 (speaker selection) and 0.0 (validation gate,
    reflection). Without drop_params every call raised UnsupportedParamsError and the
    engine wrote the error text into the transcript AS THE CHARACTER'S SPEECH.

    Asserted here rather than trusted, because the symptom is a whole run of error
    strings and the cause is one missing line at import time.
    """
    import litellm

    import matrix_studio.engine.simulator  # noqa: F401  (sets the flag at import)

    assert litellm.drop_params is True, (
        "litellm.drop_params is off; models with parameter restrictions will fail "
        "every call and their error text will be stored as dialogue"
    )


# --------------------------------------------------------------------------
# Truncation repair
#
# The second real-world failure mode, measured twice: a five-persona wizard draft
# and a run summary where four of five fields were complete and only the last was
# clipped. In both cases a strict parse returned NOTHING, so complete data was
# discarded because later data was missing.
# --------------------------------------------------------------------------


def test_recovers_complete_fields_from_a_summary_cut_off_mid_string():
    """The exact shape of the observed failure: a five-field summary whose last
    field was clipped by max_tokens. Four complete fields must survive."""
    truncated = (
        '{\n  "consensus": ["a", "b"],\n  "dissenters": ["c"],\n'
        '  "key_ideas": ["d", "e"],\n  "open_questions": ["f"],\n'
        '  "overview": "The group examined a proposed bridge re'
    )
    obj = extract_json_object(truncated)
    assert obj is not None, "complete fields were discarded because a later one was cut"
    assert obj["consensus"] == ["a", "b"]
    assert obj["dissenters"] == ["c"]
    assert obj["key_ideas"] == ["d", "e"]
    assert obj["open_questions"] == ["f"]
    # The clipped field is ABSENT rather than blank: a caller can see what is missing,
    # where a silent empty string would look like the model having nothing to say.
    assert "overview" not in obj


def test_recovers_when_a_nested_array_is_cut_mid_element():
    obj = extract_json_object('{"a": [1, 2], "b": ["x", "yy')
    assert obj == {"a": [1, 2]}


def test_recovers_when_cut_immediately_after_a_complete_field():
    obj = extract_json_object('{"a": 1, "b": 2,')
    assert obj == {"a": 1, "b": 2}


def test_repair_is_not_fooled_by_a_brace_inside_a_string():
    obj = extract_json_object('{"a": "has { and } inside", "b": "cut he')
    assert obj == {"a": "has { and } inside"}


def test_no_complete_field_before_the_cut_returns_none():
    """Better to report nothing than to invent a shape."""
    assert extract_json_object('{"a": "cut immediately') is None
    assert extract_json_object("{") is None


def test_repair_never_fires_when_the_document_is_valid():
    """The repair path must be a last resort — a valid document must parse whole,
    including its final field."""
    obj = extract_json_object('{"a": 1, "overview": "complete"}')
    assert obj == {"a": 1, "overview": "complete"}


def test_the_summary_has_its_own_token_budget():
    """Regression on the root cause: the analyst summary shared the PER-TURN
    utterance budget (2048), which a 24-turn five-field summary overflows. A turn is
    2-4 sentences; an analysis of a transcript is not."""
    from matrix_studio.settings import Settings

    s = Settings(_env_file=None)
    assert s.summary_max_tokens >= 8000
    assert s.summary_max_tokens > s.litellm_max_tokens
