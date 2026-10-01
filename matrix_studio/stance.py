# SPDX-License-Identifier: Apache-2.0
"""Where each persona ended a run: the stance the UI draws on cards, the HUD, the room map and the dial.

docs/MOBILE-UI.md §6.1. Stance is **post-run only**, written when the summary is generated, in four states:

- **support**: accepts the outcome the room reached.
- **conditional**: accepts it with conditions — signs, but on something not yet done or settled, or while
  keeping an objection of their own standing against it.
- **holding**: does not accept it.
- **unstated**: nothing recorded says which way they went. Not "undecided".

## Two sources, per persona

1. **Their closing statement** (decided by the owner 2026-10-01). When the run had a closing round
   (`selection.closing_round`; `_CLOSING` in the engine: everyone, blind, states their final position, what
   they can and cannot accept, and what moved them), every closing statement is classified in ONE model
   call for the room as accepts / accepts_with_conditions / rejects / unclear, and for each the model must
   quote the sentence that decides it. accepts → support, accepts_with_conditions → conditional, rejects →
   holding.
2. **The summary** (option 1, decided 2026-09-29), for everyone the first source does not settle: no closing
   statement, an "unclear", a quote that is not in the statement, or a classifier call that failed. Named
   among the summary's dissenters → holding; a flagged position shift (`shifts.py`) and not a dissenter →
   support; everyone else → unstated. **A run with no closing round gets exactly this rule, as before.**

Each persona's stance is stored with its basis — the class, the quote, and which source decided it — so a
reader can see why, and check it.

## Why the closing statement (2026-10-01, the owner's call)

Run 8b4c59b6 had the closing round on, and option 1 could not express acceptance. Four of its six personas
had plainly accepted the final plan, most of them signing off on it in so many words, and read as
"unstated": accepting is not a shift, and the shift detector is a word match, which in the same run also
flagged a shift that was not one and missed one that was. Two who signed while keeping a standing objection
read as "holding", which hid that they signed. The closing round asks each persona for exactly what a stance is, so stance now reads it, and
"accepts with conditions" became a state of its own rather than being forced into support or holding.

## The guard: a verbatim quote, checked here

This project has reverted classifiers that sounded right and failed held-out checks (§6.1 option 2). A
classifier that is confidently wrong is the failure to design against, so a verdict counts only when the
quote the model gives for it appears in that persona's statement, word for word — case, curly quotes and
spacing aside, as `assumptions.verified_asks` checks a proposal's asks — and is long enough to say something.
Anything else is recorded as unclear and falls back to the summary. The quote is stored as it stands in the
statement, so what the reader sees is the persona's words, never the model's rendering of them.

A quote proves the model read the sentence, not that it read it right. Agreement with hand labels has been
smoke-checked on four live statements, which is not a validation; a held-out check against hand labels is
still owed before any surface should present this as more than "what the statement appears to say".

## Edges

With no dissenter list (the field switched off, or the analyst's reply unparsed) "not a dissenter" would be
a guess, so the summary rule gives nobody a stance and ``stances`` returns None. When the closing statements
settled some personas anyway, they keep their stance and everyone else is unstated; when they settled
nobody, the run has no stance, as before.

"support" from the summary is the weakest state and is named for what the UI draws, not for what was proved:
a flagged shift is a word match, and moving is not the same as agreeing. The UI labels every state with a
glyph and a word, so none of this rests on colour.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from matrix_studio.jsonio import extract_json_object

logger = logging.getLogger(__name__)

HOLDING = "holding"
SUPPORT = "support"
CONDITIONAL = "conditional"
UNSTATED = "unstated"

#: The classes a closing statement can be given, and the stance each one is.
ACCEPTS = "accepts"
WITH_CONDITIONS = "accepts_with_conditions"
REJECTS = "rejects"
UNCLEAR = "unclear"
CLASSES = (ACCEPTS, WITH_CONDITIONS, REJECTS, UNCLEAR)
_STATE_OF = {ACCEPTS: SUPPORT, WITH_CONDITIONS: CONDITIONAL, REJECTS: HOLDING}

#: Which source decided a persona's stance.
FROM_CLOSING = "closing"
FROM_SUMMARY = "summary"

#: Why a persona's stance came from the summary instead of their closing statement. Recorded per persona,
#: because "they made no statement" and "the model's quote was not in it" are different facts, and only the
#: second says anything about the classifier.
NO_CLOSING_ROUND = "no_closing_round"
NO_STATEMENT = "no_statement"
CLASSIFIER_FAILED = "classifier_failed"
NO_VERDICT = "no_verdict"
SAID_UNCLEAR = "unclear"
UNVERIFIED_QUOTE = "unverified_quote"

#: The shortest quote that counts. A verdict backed by "I agree" proves nothing about which statement or
#: which part of it was read; the same floor `assumptions.MIN_ASK_CHARS` sets for a quoted ask.
MIN_QUOTE_CHARS = 12

#: The output budget for the room's verdicts. Each is a name, a class and a sentence or two, about 120
#: tokens; this covers a cast of twenty with room. A reply cut off anyway loses only the personas after the
#: cut (`jsonio` keeps the complete ones), and those fall back to the summary.
CLASSIFIER_MAX_TOKENS = 3000

#: The topic is context, not the thing classified, and a brief can run to pages.
_TOPIC_CHARS = 2000


def _match(name: str, cast: List[str]) -> Optional[str]:
    """The cast member a summary's speaker string refers to, or None.

    The analyst writes names freely, so "Hale" may mean "Deputy Mayor Hale". An exact (case-blind) match
    wins; otherwise a unique cast name that contains the string, or is contained by it. Ambiguous or
    unknown names match nobody rather than the first candidate.
    """
    want = name.strip().casefold()
    if not want:
        return None
    for c in cast:
        if c.casefold() == want:
            return c
    near = [c for c in cast if want in c.casefold() or c.casefold() in want]
    return near[0] if len(near) == 1 else None


def _dissenter_positions(names: List[str], summary: Dict[str, Any]) -> Dict[str, str]:
    """Each cast member the summary names as a dissenter, with the position it gives them (first wins)."""
    out: Dict[str, str] = {}
    for d in summary.get("dissenters") or []:
        speaker = d.get("speaker") if isinstance(d, dict) else None
        hit = _match(str(speaker), names) if speaker else None
        if hit and hit not in out:
            out[hit] = str(d.get("position") or "").strip()
    return out


def stances(
    cast: Iterable[str],
    summary: Optional[Dict[str, Any]],
    shifted: Iterable[str],
) -> Optional[Dict[str, str]]:
    """The summary rule alone (option 1): each cast member's stance, or None when the summary cannot say who
    dissented (see module doc). This is the whole rule for a run with no closing round."""
    names = [str(c) for c in cast if c]
    if not isinstance(summary, dict) or not isinstance(summary.get("dissenters"), list):
        return None
    holding = set(_dissenter_positions(names, summary))
    moved = {str(s) for s in shifted}
    return {n: HOLDING if n in holding else SUPPORT if n in moved else UNSTATED for n in names}


def _payload(event: Dict[str, Any]) -> Dict[str, Any]:
    payload = event.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    return payload if isinstance(payload, dict) else {}


def shifted_speakers(events: Iterable[Dict[str, Any]]) -> List[str]:
    """Who had a position shift flagged, from a run's event log."""
    out: List[str] = []
    for e in events:
        if e.get("event_type") != "position.shift":
            continue
        who = _payload(e).get("speaker") or e.get("agent_name")
        if who and who not in out:
            out.append(str(who))
    return out


def last_shift_sentence(events: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """Each speaker's most recent flagged shift sentence, in their own words: the basis a summary "support"
    can show, since that state means "a shift was flagged and they are not a dissenter"."""
    out: Dict[str, str] = {}
    for e in events:
        if e.get("event_type") != "position.shift":
            continue
        payload = _payload(e)
        who = payload.get("speaker") or e.get("agent_name")
        sentences = payload.get("sentences")
        if who and isinstance(sentences, list) and sentences:
            out[str(who)] = str(sentences[0])
    return out


def closing_statements(events: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """Each persona's closing statement, from a run's event log.

    A closing statement is an `agent.response` the engine flagged `closing: true` — the round it runs after
    `max_messages` — so a statement is told from an ordinary last turn by that flag and nothing else. A
    persona who passed in the closing round has no `agent.response` there, so has no statement.
    """
    out: Dict[str, str] = {}
    for e in events:
        if e.get("event_type") != "agent.response":
            continue
        payload = _payload(e)
        if payload.get("closing") is not True:
            continue
        who = payload.get("speaker") or e.get("agent_name")
        text = str(payload.get("message") or "").strip()
        if who and text:
            out[str(who)] = text
    return out


# --------------------------------------------------------------------------- #
# The quote check
# --------------------------------------------------------------------------- #

_STRAIGHT = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
#: What a model wraps a quote in or ends it with that the sentence in the statement may not have.
_EDGES = " \t\r\n.,;:!?\"'‘’“”…"


def _normalised(text: str) -> Tuple[str, List[int]]:
    """``text`` lower-cased, quotes straightened and spaces collapsed, with — for every character of the
    result — the index in ``text`` it came from, so a match can be cut out of the original."""
    chars: List[str] = []
    where: List[int] = []
    space = True  # drops leading whitespace
    for i, ch in enumerate(text):
        if ch.isspace():
            if not space:
                chars.append(" ")
                where.append(i)
            space = True
            continue
        space = False
        for c in ch.translate(_STRAIGHT).lower():
            chars.append(c)
            where.append(i)
    if chars and chars[-1] == " ":
        chars.pop()
        where.pop()
    return "".join(chars), where


def verified_quote(quote: Any, statement: str) -> Optional[str]:
    """The words ``quote`` points at, as they stand in ``statement``, or None when they are not in it.

    One unbroken run of the statement: an ellipsis or a changed word is not in the statement and fails, which
    is the point — a quote stitched together can say what no sentence did.
    """
    if not isinstance(quote, str):
        return None
    needle, _ = _normalised(quote.strip(_EDGES))
    if len(needle) < MIN_QUOTE_CHARS:
        return None
    hay, where = _normalised(statement or "")
    at = hay.find(needle)
    if at < 0:
        return None
    return statement[where[at]: where[at + len(needle) - 1] + 1]


# --------------------------------------------------------------------------- #
# The classifier: one model call for the room
# --------------------------------------------------------------------------- #

_SYSTEM = (
    "You are reading the closing statements from a finished conversation between several participants. In "
    "the final round each participant was asked, without seeing anyone else's statement, to state their "
    "position as it now stood, to name what they could accept from what had been proposed and what they "
    "could not accept and why, and to say what had moved them.\n\n"
    "For each participant, decide from their own statement alone whether they accept the outcome on the "
    "table at the end: the plan, proposal or conclusion their statement responds to.\n"
    '- "accepts": they accept, support or sign off on it as it stands. Still "accepts" if they also defend '
    "it against being changed later, note that questions other people raised remain open or were left open "
    "by agreement, or say a view of theirs is unchanged where that view is not an objection to the "
    "outcome.\n"
    '- "accepts_with_conditions": they accept or sign off, but their acceptance depends on something that '
    "has not happened or is not settled (a change, a check, a safeguard, an approval, a later step), or they "
    "sign while keeping an objection of their own to it standing, even one they say does not block their "
    "signature.\n"
    '- "rejects": they do not accept it: they refuse to sign off, say an objection of theirs blocks their '
    "agreement, or back a different outcome instead.\n"
    '- "unclear": the statement does not say whether they accept, or can be read more than one way. Choose '
    "this rather than guess.\n\n"
    'For each participant give "quote" first: the sentence, or consecutive sentences, from THEIR statement '
    "that decide the class, copied character for character, with no paraphrase, no ellipsis and no added or "
    "changed words. A quote that does not appear in their statement exactly is discarded and the participant "
    'is recorded as unclear. For "unclear", quote the sentence closest to a position, or "" if there is '
    "none.\n\n"
    "Respond with ONLY a single JSON object of this exact shape (no prose, no code fence):\n"
    '{"personas": [{"name": "<their name exactly as given>", "quote": "<copied exactly>", '
    '"class": "accepts" | "accepts_with_conditions" | "rejects" | "unclear"}, ...]}\n'
    "with one entry per participant."
)


def classifier_messages(statements: Dict[str, str], topic: str) -> List[Dict[str, str]]:
    """The prompt: the topic for context, then each statement, delimited and named."""
    body = "\n\n".join(f'<statement name="{name}">\n{text}\n</statement>' for name, text in statements.items())
    topic = (topic or "").strip()
    if len(topic) > _TOPIC_CHARS:
        topic = topic[:_TOPIC_CHARS].rstrip() + "…"
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f'The conversation topic was: "{topic}".\n\nClosing statements:\n\n{body}\n\n'
                                    "Classify each participant now."},
    ]


def read_verdicts(obj: Any, statements: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    """Per persona who made a closing statement: ``{class, quote}``, plus ``fallback`` when it is unclear.

    A class outside the four, a persona the reply skipped, or a quote that is not in the persona's statement
    is unclear — never the class the model claimed, which is kept as ``claimed`` so how often the check
    overrules the model can be counted.
    """
    rows = obj.get("personas") if isinstance(obj, dict) else None
    if isinstance(rows, dict):  # {"Ada": {...}} rather than a list, which a model sometimes writes
        rows = [{"name": k, **v} for k, v in rows.items() if isinstance(v, dict)]
    names = list(statements)
    found: Dict[str, Dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        hit = _match(str(row.get("name") or ""), names) if isinstance(row, dict) else None
        if hit and hit not in found:
            found[hit] = row
    out: Dict[str, Dict[str, Any]] = {}
    for name, text in statements.items():
        row = found.get(name)
        cls = str((row or {}).get("class") or "").strip().lower()
        if cls not in CLASSES:
            out[name] = {"class": UNCLEAR, "quote": None, "fallback": NO_VERDICT}
            continue
        quote = verified_quote((row or {}).get("quote"), text)
        if cls == UNCLEAR:
            out[name] = {"class": UNCLEAR, "quote": quote, "fallback": SAID_UNCLEAR}
        elif quote is None:
            out[name] = {"class": UNCLEAR, "quote": None, "fallback": UNVERIFIED_QUOTE, "claimed": cls}
        else:
            out[name] = {"class": cls, "quote": quote}
    return out


async def classify_closing(
    statements: Dict[str, str], topic: str, model: Optional[str],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Dict[str, Any]]]]:
    """Classify the room's closing statements in one call. Never raises.

    Returns ``(record, verdicts)``: the record is what the call cost and whether it failed, stored with the
    stance so the cost can be itemised; ``verdicts`` is None when the call failed or its reply could not be
    read, which sends every persona to the summary rule.
    """
    from matrix_studio import analysis

    record: Dict[str, Any] = {"model": model, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "error": None}
    try:
        # temperature 0: the same statements should get the same verdicts when a summary is regenerated.
        result = await analysis._acompletion(
            classifier_messages(statements, topic), model=model, temperature=0.0,
            max_tokens=CLASSIFIER_MAX_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001 - a stance is a view of the run; it must not cost the summary
        logger.warning("Closing-statement classifier call failed: %s", exc)
        record["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return record, None
    record["tokens_in"] = int(result.get("tokens_in") or 0)
    record["tokens_out"] = int(result.get("tokens_out") or 0)
    record["cost_usd"] = float(result.get("cost_usd") or 0.0)
    obj = extract_json_object(result.get("content") or "")
    if not isinstance(obj, dict) or not isinstance(obj.get("personas"), (list, dict)):
        why = result.get("finish_reason")
        record["error"] = "the reply could not be read" + (f" (finish_reason={why})" if why else "")
        logger.warning("Closing-statement classifier reply could not be read (finish_reason=%s)", why)
        return record, None
    return record, read_verdicts(obj, statements)


# --------------------------------------------------------------------------- #
# The two sources together
# --------------------------------------------------------------------------- #


def resolve(
    cast: Iterable[str],
    summary: Optional[Dict[str, Any]],
    events: Iterable[Dict[str, Any]],
    statements: Dict[str, str],
    verdicts: Optional[Dict[str, Dict[str, Any]]],
) -> Tuple[Optional[Dict[str, str]], Optional[Dict[str, Dict[str, Any]]]]:
    """``({name: stance}, {name: basis})`` for a run, or ``(None, None)`` when nothing says anything.

    ``verdicts`` is the classifier's (None when it failed or there were no statements). Each basis is
    ``{stance, source, class, quote}``, and ``fallback`` when the summary decided:

    - source ``closing``: ``quote`` is the persona's own words from their closing statement, checked.
    - source ``summary``: ``quote`` is the summary's account of the objection (holding), the persona's
      flagged shift sentence (support), or None (unstated); ``class`` is what the classifier said, if it
      said anything, and ``fallback`` says why it did not decide.
    """
    events = list(events)
    names = [str(c) for c in cast if c]
    base = stances(names, summary, shifted_speakers(events))
    positions = _dissenter_positions(names, summary) if base is not None and isinstance(summary, dict) else {}
    shift_said = last_shift_sentence(events)
    state: Dict[str, str] = {}
    basis: Dict[str, Dict[str, Any]] = {}
    for n in names:
        v = (verdicts or {}).get(n)
        if v and v.get("class") in _STATE_OF:
            state[n] = _STATE_OF[v["class"]]
            basis[n] = {"stance": state[n], "source": FROM_CLOSING, "class": v["class"], "quote": v["quote"]}
            continue
        fallback = (
            NO_CLOSING_ROUND if not statements
            else NO_STATEMENT if n not in statements
            else CLASSIFIER_FAILED if verdicts is None
            else (v or {}).get("fallback") or NO_VERDICT
        )
        s = base.get(n, UNSTATED) if base is not None else UNSTATED
        quote = None
        if s == HOLDING:
            quote = positions.get(n) or None
        elif s == SUPPORT:
            quote = shift_said.get(n)
        state[n] = s
        basis[n] = {"stance": s, "source": FROM_SUMMARY, "class": (v or {}).get("class"), "quote": quote,
                    "fallback": fallback}
        if v and v.get("claimed"):
            basis[n]["claimed"] = v["claimed"]
    if base is None and not any(b["source"] == FROM_CLOSING for b in basis.values()):
        return None, None
    return state, basis
