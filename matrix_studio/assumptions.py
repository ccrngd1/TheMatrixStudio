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
        if e.get("event_type") != "assumption.made":
            continue
        p = e.get("payload")
        p = json.loads(p) if isinstance(p, str) else (p or {})
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
