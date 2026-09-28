# SPDX-License-Identifier: Apache-2.0
"""Working assumptions: what the room reasons from when nobody in it can know the answer.

## Why

Stage 1 of "conversations end in 'it depends'" measured that personas name the data they need and the
result that would move them, and then stop: the data does not exist inside a simulation, so a firm
persona's correct reply is "show me", every time. An assumption lets the room carry on — "assume churn
is about 7%" — and makes the dependence visible, so a reader knows which conclusions rest on it and can
fork the run with a different value to see what it was worth (`docs/EVIDENCE-LEAN.md`, BACKLOG Stage 3).

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

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

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

If so, propose ONE working assumption so the discussion can move on: the most plausible value, stated \
plainly and specifically, and its basis — preferably estimates the participants themselves gave. It \
must be a FACT about the world, never the decision under discussion and never anyone's position.

If not, the answer is null. Most of the time the answer is null.

Reply with ONLY a JSON object:
{{"assumption": null}}
or
{{"assumption": {{"statement": "<the assumption>", "basis": "<why this value>", "gap": "<the unknown it fills, and who asked>"}}}}"""


def propose_messages(topic: str, conversation: Sequence[Dict[str, Any]],
                     ledger: Sequence[Assumption], recent: int = 12) -> List[Dict[str, str]]:
    lines = "\n".join(f"{m.get('speaker')}: {m.get('content')}" for m in list(conversation)[-recent:])
    held = "\n".join(f"- {a.id}: {a.statement}" for a in ledger) or "(none)"
    return [{"role": "user", "content": _PROPOSE_PROMPT.format(
        topic=topic, ledger=held, conversation=lines or "(nothing yet)")}]


def parse_proposal(parsed: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """The proposed assumption, or None. Anything malformed is None: a missing assumption costs a turn of
    "show me"; an invented one built from a parse accident would steer the whole run."""
    raw = (parsed or {}).get("assumption")
    if not isinstance(raw, dict):
        return None
    statement = " ".join(str(raw.get("statement") or "").split())[:MAX_STATEMENT_CHARS]
    if not statement:
        return None
    return {"statement": statement,
            "basis": " ".join(str(raw.get("basis") or "").split())[:MAX_BASIS_CHARS],
            "gap": " ".join(str(raw.get("gap") or "").split())[:MAX_BASIS_CHARS]}


def next_id(ledger: Sequence[Assumption]) -> str:
    nums = [int(a.id[1:]) for a in ledger if a.id[:1] == "A" and a.id[1:].isdigit()]
    return f"A{max(nums, default=0) + 1}"
