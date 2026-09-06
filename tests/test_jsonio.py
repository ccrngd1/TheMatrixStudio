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
