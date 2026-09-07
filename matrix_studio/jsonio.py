# SPDX-License-Identifier: Apache-2.0
"""Tolerant extraction of a JSON object from a model response.

## Why this module exists

Measured 2026-09-06, on a 30-turn cognition run against
``bedrock/global.anthropic.claude-haiku-4-5``: **30 of 30 turns** came back wrapped
in a markdown fence —

    ```json
    {"utterance": "...", "rationale": "...", "memories": [...]}
    ```

— and every one of them failed ``json.loads``. The consequences, all silent:

1. **Cognition was completely inert.** ``_generate_response`` degrades gracefully on
   a parse failure by keeping the raw text as the utterance, so the run completed and
   looked fine. But the transcript contained fenced JSON blobs instead of speech, and
   across five personas and 30 turns the run produced **0 memories, 0 reflections, 0
   rationales, 0 goal_served**. A feature shipped since v0.2 was doing nothing while
   costing more than not using it.
2. **The Phase 4a validation gate silently dropped every suspicion.** Its LLM
   confirmation call catches broad exceptions and fails open, so a JSONDecodeError
   became ``violation: False``. The selective confirmation had never confirmed
   anything against this model.
3. **Speaker selection fell back to substring matching** on the raw JSON, which
   mostly still found a name — the least harmful of the three, and the reason nothing
   looked obviously broken.

``response_format={"type": "json_object"}`` is passed on all of these calls. It is
evidently not honoured on this provider/model path, so the code cannot rely on it.

Two other modules had already learned this independently — ``analysis.py`` grew an
``_extract_json`` in Phase 1.5 and ``naming.py`` strips fences by hand — while the
engine and the validation gate never did. That duplication is why the lesson did not
spread, so this is now one implementation that all of them use.

## Why not just strip fences

Because the failure is "the model added something around the JSON", and a fence is
only the commonest form of that. Prose before or after the object is just as likely,
so the last resort is the widest ``{...}`` span rather than a fence-specific rule.

## Truncation repair

The other real-world failure is the reply being **cut off** at ``max_tokens``, which
leaves valid JSON that simply stops mid-value. Measured twice in this project: a
five-persona wizard draft, and a run summary where four of five fields were complete
and only the last was clipped — and in both cases a strict parse returned nothing at
all, so complete data was discarded because later data was missing.

So the last resort before giving up is to close the object at the last complete
key/value pair. That yields a *partial* object, which is the honest outcome: the
caller gets the fields the model actually finished. Raising the token budget is the
real fix for any given caller; this stops a budget being a total failure.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

# A fenced block, with or without a language tag: ```json { ... } ``` or ``` { ... } ```
_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_-]+)?\s*(\{.*?\})\s*```", re.DOTALL)
# Widest brace span, for prose wrapped around a bare object.
_BRACE_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json_object(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """Best-effort parse of a JSON **object** out of a model response.

    Tries, in order: the whole string, a fenced block, then the widest ``{...}``
    span. Returns ``None`` when nothing parses or the result is not a dict — callers
    treat that as "the model did not answer in the requested shape" and degrade, so
    returning a non-dict would push the problem downstream.

    Never raises. This sits on the hot path of every cognition turn and of the
    validation gate; a parser that can throw would turn a formatting quirk into a
    failed run.
    """
    if not text:
        return None

    for candidate in _candidates(text):
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj

    # Everything failed; the commonest remaining cause is truncation.
    return repair_truncated_object(text)


def repair_truncated_object(text: str) -> Optional[Dict[str, Any]]:
    """Recover the complete leading fields of a JSON object cut off mid-value.

    Finds the last point at which the object was structurally complete — a ``,`` or a
    closing bracket at depth 1 — truncates there, closes any still-open brackets, and
    parses. Returns ``None`` when nothing complete precedes the cut.

    The result is deliberately PARTIAL rather than padded with empty values. A caller
    that receives four of five fields can see which one is missing; one handed five
    fields where the fifth is a silent blank cannot.
    """
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    body = text[start:]

    # Track structure, remembering the last position where a top-level member ended.
    stack: List[str] = []
    in_string = False
    escaped = False
    cut = -1
    for i, ch in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            if len(stack) == 1:
                cut = i + 1  # a nested value just closed at top level
        elif ch == "," and len(stack) == 1:
            cut = i  # a top-level member just ended

    if cut <= 0:
        return None
    # Rebuild: everything up to the cut, plus the closers still owed. Depth at `cut`
    # is 1 by construction, so a single "}" closes it.
    try:
        obj = json.loads(body[:cut].rstrip().rstrip(",") + "}")
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _candidates(text: str):
    """The strings worth attempting, cheapest and most likely first."""
    stripped = text.strip()
    yield stripped

    fence = _FENCE_RE.search(stripped)
    if fence:
        yield fence.group(1)

    # Bare fence markers with no brace captured by the pattern above (e.g. the model
    # emitted ```json\n{...}  and never closed the fence, which does happen when a
    # response is truncated at max_tokens).
    if stripped.startswith("```"):
        body = stripped.lstrip("`")
        if body[:4].lower() == "json":
            body = body[4:]
        yield body.strip().rstrip("`").strip()

    brace = _BRACE_RE.search(stripped)
    if brace:
        yield brace.group(0)
