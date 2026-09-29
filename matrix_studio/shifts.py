# SPDX-License-Identifier: Apache-2.0
"""Flagging a persona's announced change of position, and what it credits. Flag-only — never regenerates.

## Why

`docs/EVIDENCE-LEAN-FOLDING.md`: the one fold found was a persona giving ground for a reason on neither of
its stated conditions while *saying* it was on its list. The holding rule asks a persona to move only when
its own condition turns up; nothing checked that it had. This puts the check in front of the reader
instead: at the message where a persona says it moved, what it credited — another persona, a consultant, a
scheduled message, a working assumption — and the conditions it had said would move it.

## What it is not

A judgement. A regenerating gate here was deliberately not built (BACKLOG, "no validation gate for
abandoned convictions"): "conceded a position" is a matter of degree, and a false positive would rewrite a
turn in which a persona legitimately changed its mind. This records; the reader decides.

## How a shift is found

Personas announce shifts in words, and only REALISED ones count: "it moved me", "I'll give ground", "I was
wrong", "that's what changed my mind". Prospective and conditional forms — "that would move me", "before I
concede", "nothing has changed my position" — are the common case and are excluded by a negation or
conditional in the cue's own clause. Measured on the twelve evidence-lean runs (480 messages): the first
version of the cues fired 51 times, almost all prospective; this one fires 8 times, each a real shift or an
explicit concession.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

SHIFT = re.compile(
    r"\b(?:moved me|i(?:'ll| will) give (?:you )?(?:that |some )?ground|i(?:'m| am) giving ground|i concede"
    r"|i(?:'ve| have) conceded|i(?:'m| am) conceding|i(?:'ll| will) concede|i was wrong"
    r"|you(?:'ve| have) (?:convinced|persuaded) me|i(?:'m| am) (?:now )?persuaded|what changed my mind"
    r"|changed my (?:mind|position|view)|my position (?:has )?(?:moved|shifted|changed)"
    r"|i(?:'ve| have) (?:moved|shifted) (?:off|on|my))\b",
    re.IGNORECASE,
)
NOT_REALISED = re.compile(
    r"\b(?:if|unless|until|before|would|could|whether|nothing|hasn'?t|haven'?t|didn'?t|doesn'?t|won'?t|not"
    r"|never|nobody|no one|what i'?d)\b",
    re.IGNORECASE,
)
_SENTENCES = re.compile(r"(?<=[.!?])[\"”’]?\s+")
_CLAUSE = re.compile(r"[,;:—]| - | and | but ")
_WORD = re.compile(r"[a-z][a-z'\-]{2,}")
_STOP = set(
    "the and that this with from have has had for are was were been being into onto than then them they their "
    "there what when where which while would could should about above after again against because before "
    "below between both each few more most other some such only own same very will just also any all not "
    "can its it's itself our ours your yours his her hers him she he who whom why how one two".split()
)
DEFENDED = ("firm", "non-negotiable", "requires-escalation")


def shift_sentences(message: str) -> List[str]:
    """Sentences in which the speaker says its own position moved. Empty for almost every message."""
    out = []
    for sentence in _SENTENCES.split(message or ""):
        m = SHIFT.search(sentence)
        if m and not NOT_REALISED.search(_CLAUSE.split(sentence[: m.start()])[-1]):
            out.append(sentence.strip())
    return out


def _mentions(text: str, name: str) -> bool:
    return bool(name) and re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text, re.IGNORECASE) is not None


def credited(sentences: Sequence[str], message: str, *, personas: Sequence[str], speaker: str,
             consultants: Sequence[str] = (), injected: Sequence[str] = (),
             assumptions: Sequence[str] = ()) -> List[Dict[str, str]]:
    """What the shift credits, from the names it uses: the shift sentence first, else the whole message.

    Never guessed from who spoke last. A shift that names nobody is recorded as naming nobody, which is
    itself worth a reader's attention.
    """
    kinds = ([("assumption", a) for a in assumptions] + [("scheduled message", n) for n in injected]
             + [("consultant", n) for n in consultants] + [("persona", n) for n in personas if n != speaker])
    for scope in (" ".join(sentences), message):
        found = [{"kind": k, "name": n} for k, n in kinds if _mentions(scope, n)]
        if found:
            return found
    return []


def _content_words(text: str) -> set:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def conditions_of(structured: Any) -> List[Dict[str, str]]:
    """The persona's stated change-conditions, one entry per condition, for its defended positions."""
    out = []
    for vp in (getattr(structured, "viewpoints", None) or []):
        if str(getattr(vp, "firmness", "")) not in DEFENDED:
            continue
        for cond in getattr(vp, "evidence_that_shifts", None) or []:
            out.append({"position": str(vp.position), "firmness": str(vp.firmness), "condition": str(cond)})
    return out


def matched_conditions(message: str, conditions: Sequence[Dict[str, str]], min_shared: int = 2) -> List[str]:
    """Conditions whose content words the message shares at least ``min_shared`` of. A word overlap, so
    "appears to name" rather than "met" — whether the condition actually turned up is the reader's call."""
    said = _content_words(message)
    return [c["condition"] for c in conditions if len(_content_words(c["condition"]) & said) >= min_shared]


def flag(message: str, *, speaker: str, structured: Any, personas: Sequence[str],
         consultants: Sequence[str] = (), injected: Sequence[str] = (),
         assumptions: Sequence[str] = ()) -> Optional[Dict[str, Any]]:
    """The `position.shift` payload for a message, or None when it announces no shift."""
    sentences = shift_sentences(message)
    if not sentences:
        return None
    conds = conditions_of(structured)
    matched = matched_conditions(message, conds)
    return {
        "speaker": speaker,
        "sentences": [s[:400] for s in sentences],
        "credits": credited(sentences, message, personas=personas, speaker=speaker, consultants=consultants,
                            injected=injected, assumptions=assumptions),
        "conditions": conds,
        "matched_conditions": matched,
        # The fold that motivated this: a defended persona moving with no listed condition in sight.
        "no_listed_condition": bool(conds) and not matched,
    }
