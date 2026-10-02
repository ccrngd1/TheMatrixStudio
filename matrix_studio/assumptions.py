# SPDX-License-Identifier: Apache-2.0
"""Working assumptions: what the room reasons from when nobody in it can know the answer.

## Why

Stage 1 of "conversations end in 'it depends'" measured that personas name the data they need and the
result that would move them, and then stop: the data does not exist inside a simulation, so a firm
persona's correct reply is "show me", every time. An assumption lets the room carry on — "assume churn
is about 7%" — and makes the dependence visible, so a reader knows which conclusions rest on it and can
fork the run with a different value to see what it was worth (`docs/studies/EVIDENCE-LEAN.md`, BACKLOG Stage 3).

## What an assumption is NOT

Evidence. The holding rule moves a [firm] position only when something on its "what would change your
mind" list actually turns up, and an assumption is never that — it changes what a persona would do IF it
holds, not what the persona is convinced of. The prompt says so, because the risk this feature carries
is a new route to capitulation: a room talking itself into agreement on a number somebody made up.

## Where they come from

`config.assumptions`, set by the operator before the run (source `operator`, numbered A1..An). Each is
recorded as an `assumption.made` event at turn 0, so the transcript shows it and a branch replays it.
"""

from __future__ import annotations

import re as _re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Most assumptions a run may be given. Each is prompt text on every turn.
MAX_ASSUMPTIONS = 8
MAX_STATEMENT_CHARS = 300
MAX_BASIS_CHARS = 300

OPERATOR = "operator"


@dataclass(frozen=True)
class Assumption:
    id: str
    statement: str
    basis: str = ""
    source: str = OPERATOR
    turn: int = 0

    def payload(self) -> Dict[str, Any]:
        return {"id": self.id, "statement": self.statement, "basis": self.basis,
                "source": self.source, "turn": self.turn}


def from_config(config: Optional[Dict[str, Any]]) -> List[Assumption]:
    """The operator's assumptions, numbered in the order given. Blank statements are skipped."""
    out: List[Assumption] = []
    for raw in (config or {}).get("assumptions") or []:
        if not isinstance(raw, dict):
            continue
        statement = " ".join(str(raw.get("statement") or "").split())[:MAX_STATEMENT_CHARS]
        if not statement:
            continue
        basis = " ".join(str(raw.get("basis") or "").split())[:MAX_BASIS_CHARS]
        out.append(Assumption(f"A{len(out) + 1}", statement, basis))
        if len(out) >= MAX_ASSUMPTIONS:
            break
    return out


def assumptions_block(assumptions: Sequence[Assumption]) -> str:
    """The prompt block every persona sees. Empty when there are none, so a run without them is
    byte-identical to one from before this existed."""
    if not assumptions:
        return ""
    lines = "\n".join(
        f"- {a.id}: {a.statement}" + (f" (basis: {a.basis})" if a.basis else "") for a in assumptions
    )
    return (
        "\n\nWorking assumptions for this discussion — set so it can move past what nobody here can "
        f"know; they are NOT established facts:\n{lines}\n"
        "Reason from them as if they hold, and say what you would do if they do. They change what you "
        "would do IF they hold, not what you are convinced of: an assumption is not evidence, and it is "
        "never something on your \"what would change your mind\" list turning up. If you think one is "
        "wrong, say so plainly, by its id, and why."
    )


def from_events(events: Sequence[Dict[str, Any]]) -> List[Assumption]:
    """The assumptions a run actually used, read from its `assumption.made` events, in order."""
    import json

    out: Dict[str, Assumption] = {}
    for e in events:
        if e.get("event_type") not in ("assumption.made", "assumption.withdrawn"):
            continue
        p = e.get("payload")
        p = json.loads(p) if isinstance(p, str) else (p or {})
        if e["event_type"] == "assumption.withdrawn":
            # A fork that withdrew it (slice C): from here on the room no longer reasons from it.
            out.pop(str(p.get("id")), None)
            continue
        # A later event with the same id REPLACES the earlier one — how a fork swaps an assumption.
        if p.get("id") and p.get("statement"):
            out[str(p["id"])] = Assumption(str(p["id"]), str(p["statement"]), str(p.get("basis") or ""),
                                           str(p.get("source") or OPERATOR), int(p.get("turn") or 0))
    return list(out.values())


def summary_note(assumptions: Sequence[Assumption]) -> str:
    """What the analyst is told, so a conclusion that rests on an assumption is not reported as a finding."""
    if not assumptions:
        return ""
    lines = "\n".join(f"- {a.id}: {a.statement}" for a in assumptions)
    return (
        "\n\nThe cast was told to reason from these working assumptions, which are NOT established "
        f"facts:\n{lines}\nWhere a conclusion depends on one, say so by its id."
    )


# --------------------------------------------------------------------------- #
# Made during the run, by the moderator (Stage 3, slice B)
# --------------------------------------------------------------------------- #

MODERATOR = "moderator"

#: `config.dynamic_assumptions`. Off by default, like every optional capability here: a run without it
#: is byte-identical to one from before, and it costs one small call per check.
DEFAULT_EVERY = 4
DEFAULT_LIMIT = 3


@dataclass(frozen=True)
class DynamicSettings:
    enabled: bool = False
    #: Check after every this-many completed turns.
    every: int = DEFAULT_EVERY
    #: Most assumptions the moderator may add in one run. The operator's do not count against it.
    limit: int = DEFAULT_LIMIT


def dynamic_from_config(config: Optional[Dict[str, Any]]) -> DynamicSettings:
    raw = (config or {}).get("dynamic_assumptions")
    if raw is True:
        return DynamicSettings(enabled=True)
    if not isinstance(raw, dict) or not raw.get("enabled"):
        return DynamicSettings()
    try:
        every = max(1, int(raw.get("every") or DEFAULT_EVERY))
        limit = max(0, int(raw.get("limit") if raw.get("limit") is not None else DEFAULT_LIMIT))
    except (TypeError, ValueError):
        every, limit = DEFAULT_EVERY, DEFAULT_LIMIT
    return DynamicSettings(True, every, limit)


def due(settings: DynamicSettings, completed_turns: int, ledger: Sequence[Assumption]) -> bool:
    """Whether to check now: on, a multiple of `every` completed turns, cap not spent, and not already
    checked after this turn (a retried slice must not add a second assumption for the same gap)."""
    if not settings.enabled or completed_turns <= 0 or completed_turns % settings.every:
        return False
    made = [a for a in ledger if a.source == MODERATOR]
    return len(made) < settings.limit and all(a.turn != completed_turns for a in made)


_PROPOSE_PROMPT = """You keep a discussion moving. It is about: {topic}

Working assumptions already in force (do not repeat or contradict these):
{ledger}

The recent conversation:
{conversation}

Decide ONE thing: is the discussion stuck on an unknown that NOBODY in it can supply — the same \
missing fact or number asked for by two participants, or twice by one — that no assumption above \
covers?

If so, propose ONE working assumption so the discussion can move on: the most plausible value of that \
unknown, stated plainly and specifically, and its basis — preferably estimates the participants \
themselves gave. It must be a FACT about the world (a number, a date, a rate, what a rule says), never \
the decision under discussion, never a plan, and never anyone's position.

Quote the asks: copy, word for word, at least two places in the conversation above where someone asked \
for this unknown or said they needed it. Copy exactly — the quotes are checked against the transcript, \
and a proposal whose asks cannot be found is discarded.

{consultants}If there is no such unknown, the answer is null. Most of the time the answer is null.

Reply with ONLY a JSON object:
{{"assumption": null}}
or
{{"assumption": {{"statement": "<the assumption>", "basis": "<why this value, in one sentence>", \
"gap": "<the unknown, in a few words>", "asks": ["<exact words>", "<exact words>"]}}}}"""

#: Verbatim asks a proposal must cite, found in the conversation. The prompt's rule — "the same unknown
#: asked for by two participants, or twice by one" — measured as NOT followed when merely stated: on the
#: first live run the check proposed at both of its two chances, once with a plan rather than a fact.
#: Quoting makes the rule checkable without another model call.
MIN_ASKS = 2
MIN_ASK_CHARS = 12


#: Messages the check reads — and the only ones its quoted asks are verified against.
RECENT_MESSAGES = 12


def propose_messages(topic: str, conversation: Sequence[Dict[str, Any]],
                     ledger: Sequence[Assumption], recent: int = RECENT_MESSAGES,
                     consultants: Sequence[Any] = ()) -> List[Dict[str, str]]:
    lines = "\n".join(f"{m.get('speaker')}: {m.get('content')}" for m in list(conversation)[-recent:])
    held = "\n".join(f"- {a.id}: {a.statement}" for a in ledger) or "(none)"
    return [{"role": "user", "content": _PROPOSE_PROMPT.format(
        topic=topic, ledger=held, conversation=lines or "(nothing yet)",
        consultants=consultants_note(consultants))}]


def consultants_note(consultants: Sequence[Any]) -> str:
    """Asking before assuming. A consultant (`experts.py`) answers from documents; an assumption is a
    guess. When one could hold the answer and has not been asked, the room should ask. Empty when the
    run has none, so the prompt is unchanged for every run without consultants."""
    if not consultants:
        return ""
    names = "\n".join(f"- {e.name}: {e.expertise or 'a subject-matter expert'}" for e in consultants)
    return (
        "The room can ask these consultants, who answer from their own documents (their answers appear "
        f"in the conversation as \"<name> (consultant)\"):\n{names}\n"
        "If the unknown is something one of them could plausibly find in their documents and nobody has "
        "asked them yet, the answer is null — the room should ask, not assume. Propose an assumption for "
        "it only after a consultant has said it is not in their sources, or when none of them could know.\n\n"
    )


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().replace("\u2019", "'").replace("\u201c", '"')
                    .replace("\u201d", '"').split())


def verified_asks(asks: Any, conversation: Sequence[Dict[str, Any]]) -> Tuple[List[str], int]:
    """(the quoted asks that really occur in the conversation, how many distinct MESSAGES they occur in).

    Word for word, case and spacing aside. Messages, not quotes, are what count: two people asking in the
    same words is two asks, and one quote repeated is still one message.
    """
    said = [_norm(m.get("content")) for m in conversation]
    found: List[str] = []
    where: set = set()
    for ask in asks if isinstance(asks, list) else []:
        q = _norm(str(ask)).strip(" .\"'")
        if len(q) < MIN_ASK_CHARS:
            continue
        hits = {i for i, s in enumerate(said) if q in s}
        if hits and q not in found:
            found.append(q)
            where |= hits
    return found, len(where)


#: Word overlap at which a proposal counts as repeating an assumption already in force. Measured
#: (docs/studies/MODERATOR-ASSUMPTIONS.md): 2 of 9 made were verbatim repeats despite "do not repeat" in the prompt.
REPEAT_OVERLAP = 0.8


def _words(text: str) -> set:
    return set(_norm(text).replace(",", " ").replace(".", " ").split())


def repeats(statement: str, ledger: Sequence[Assumption]) -> Optional[str]:
    """The id of an assumption in force that this statement repeats, or None."""
    mine = _words(statement)
    for a in ledger:
        theirs = _words(a.statement)
        if mine and theirs and len(mine & theirs) / len(mine | theirs) >= REPEAT_OVERLAP:
            return a.id
    return None


def parse_proposal(parsed: Optional[Dict[str, Any]],
                   conversation: Sequence[Dict[str, Any]] = (),
                   ledger: Sequence[Assumption] = ()) -> Tuple[Optional[Dict[str, Any]], str]:
    """(the proposed assumption or None, why not). Anything malformed is None: a missing assumption costs
    a turn of "show me"; an invented one would steer the whole run."""
    raw = (parsed or {}).get("assumption")
    if not isinstance(raw, dict):
        return None, "no gap"
    # The model sometimes writes the id it expects into the statement ("A3: Of the 1,260…"); the engine
    # assigns ids, so a leading one is dropped rather than shown twice.
    statement = _re.sub(r"^\s*A\d+\s*[:.)\-]\s*", "", " ".join(str(raw.get("statement") or "").split()))
    statement = statement[:MAX_STATEMENT_CHARS]
    if not statement:
        return None, "no statement"
    same = repeats(statement, ledger)
    if same:
        return None, f"repeats {same}, already in force"
    asks, messages = verified_asks(raw.get("asks"), conversation)
    if messages < MIN_ASKS:
        return None, f"the quoted asks occur in {messages} message(s) of the conversation; {MIN_ASKS} needed"
    return {"statement": statement,
            "basis": " ".join(str(raw.get("basis") or "").split())[:MAX_BASIS_CHARS],
            "gap": " ".join(str(raw.get("gap") or "").split())[:MAX_BASIS_CHARS],
            "asks": asks}, ""


def next_id(ledger: Sequence[Assumption]) -> str:
    nums = [int(a.id[1:]) for a in ledger if a.id[:1] == "A" and a.id[1:].isdigit()]
    return f"A{max(nums, default=0) + 1}"


# --------------------------------------------------------------------------- #
# Used and disputed: what the room did with each assumption
# --------------------------------------------------------------------------- #


#: Words that, in the same sentence as an assumption's id, mark the sentence as disputing it. A heuristic
#: — personas are told to dispute an assumption "plainly, by its id", which is what makes one workable —
#: and reported as such: every flagged sentence is quoted in the full report so a reader can overrule it.
DISPUTE_CUES = _re.compile(
    r"\b(?:wrong|doubt\w*|disagree\w*|don'?t (?:buy|accept|trust|believe)|not (?:a guarantee|realistic|"
    r"convinced|credible|safe to assume)|too (?:optimistic|pessimistic|high|low|rosy|aggressive|"
    r"conservative)|unrealistic|optimistic|skeptic\w*|sceptic\w*|reject\w*|push(?:ing)? back|"
    r"can'?t accept|isn'?t (?:right|realistic|credible)|questionable|shaky|flawed|overstat\w*|"
    r"understat\w*|soft|dispute\w*|won'?t hold|doesn'?t hold|unfounded)\b",
    _re.IGNORECASE,
)
_SENTENCE = _re.compile(r"(?<=[.!?])\s+")


def usage(assumption_ids: Sequence[str], transcript: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per assumption id: how many messages cite it, and the sentences that appear to dispute it.

    ``transcript`` items carry ``speaker``, ``turn`` and ``message`` (the export's shape). An id is matched
    as a word ("A1", "A1's"), so A1 never matches A12. A dispute that never names the id — "that 7% is
    soft" — is not found: recall is limited to what the prompt asked personas to do.
    """
    out: Dict[str, Dict[str, Any]] = {a: {"cited": 0, "disputes": []} for a in assumption_ids}
    patterns = {a: _re.compile(rf"\b{_re.escape(a)}\b") for a in assumption_ids}
    for m in transcript:
        if m.get("injected"):
            # The operator's own words, not the room reasoning from it.
            continue
        text = str(m.get("message") or "")
        for aid, pat in patterns.items():
            if not pat.search(text):
                continue
            out[aid]["cited"] += 1
            for sentence in _SENTENCE.split(text):
                # "If A2 doesn't hold, we pause" reasons FROM the assumption; it does not dispute it.
                # Measured on the first live runs: the only false positive was exactly that shape.
                conditional = _re.search(rf"\b(?:if|unless|in case|should|whether)\b[^.;:]{{0,60}}\b{_re.escape(aid)}\b",
                                         sentence, _re.IGNORECASE)
                if pat.search(sentence) and DISPUTE_CUES.search(sentence) and not conditional:
                    out[aid]["disputes"].append({"speaker": m.get("speaker"), "turn": m.get("turn"),
                                                 "sentence": sentence.strip()[:300]})
                    break
    return out
