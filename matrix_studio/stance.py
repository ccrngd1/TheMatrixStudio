# SPDX-License-Identifier: Apache-2.0
"""Where each persona ended a run: the stance the UI draws on cards, the HUD, the room map and the dial.

docs/MOBILE-UI.md §6.1, decided by the owner as option 1: stance is **post-run only**, and built from two
things the system already records rather than from a classifier.

- **holding**: the persona is named among the summary's *dissenters*, the standing objections at the end.
- **support**: not a dissenter, and a position shift was flagged for them (`shifts.py`) at some point.
- **unstated**: everyone else. Not "undecided": nothing recorded says which way they went.

A live run has no stance, and neither does a finished run whose summary has no dissenter list (the field
was switched off, or the analyst's reply could not be parsed): with no list, "not a dissenter" would be a
guess, and every persona would read as support or unstated by default. ``stances`` returns None for both.

"support" is the weakest of the three and is named for what the UI draws, not for what was proved: a
flagged shift is a word match (`shifts.py`), and moving is not the same as agreeing. The UI labels the
states with a glyph and a word so none of this rests on colour.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

HOLDING = "holding"
SUPPORT = "support"
UNSTATED = "unstated"


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


def stances(
    cast: Iterable[str],
    summary: Optional[Dict[str, Any]],
    shifted: Iterable[str],
) -> Optional[Dict[str, str]]:
    """Each cast member's stance, or None when the summary cannot say who dissented (see module doc)."""
    names = [str(c) for c in cast if c]
    if not isinstance(summary, dict) or not isinstance(summary.get("dissenters"), list):
        return None
    holding = set()
    for d in summary["dissenters"]:
        speaker = d.get("speaker") if isinstance(d, dict) else None
        hit = _match(str(speaker), names) if speaker else None
        if hit:
            holding.add(hit)
    moved = {str(s) for s in shifted}
    return {n: HOLDING if n in holding else SUPPORT if n in moved else UNSTATED for n in names}


def shifted_speakers(events: Iterable[Dict[str, Any]]) -> List[str]:
    """Who had a position shift flagged, from a run's event log."""
    out: List[str] = []
    for e in events:
        if e.get("event_type") != "position.shift":
            continue
        payload = e.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        who = (payload or {}).get("speaker") or e.get("agent_name")
        if who and who not in out:
            out.append(str(who))
    return out
