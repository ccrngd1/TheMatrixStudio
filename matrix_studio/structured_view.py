# SPDX-License-Identifier: Apache-2.0
"""
Phase 4d — the optional structured output view (Narrative / Consequences /
Updated State / Possibilities).

A game-master-style read-out of a turn, PROJECTED from the turn's canonical
events + the turn's snapshot. It is a derived view, never the canonical record:

  * It never invents content. Narrative is the turn's real utterance(s),
    verbatim. Every Consequences/Updated-State line carries the ``seq`` of the
    canonical event that backs it (``source_seq``) — a line without a backing
    event cannot be constructed here by design, and the test suite asserts it.
  * Possibilities are the OPEN pending threads as of the turn (4b ledger) —
    surfaced next directions, explicitly non-limiting. No threads -> the
    section says so instead of inventing options.
  * Absent data yields an honest "no data" note per section, never filler.

Default OFF (``settings.structured_output``); it can also be requested
per-call. Turning it on changes NOTHING about canonical events/snapshots —
it only adds a read-only projection endpoint.
"""

from typing import Any, Dict, List, Optional

from matrix_studio.state import SimSnapshot


def _line(text: str, seq: int) -> Dict[str, Any]:
    """One Consequences/State line + the canonical event seq that backs it."""
    return {"text": text, "source_seq": seq}


def build_structured_view(
    turn: int,
    turn_events: List[Dict[str, Any]],
    snapshot: Optional[SimSnapshot],
) -> Dict[str, Any]:
    """
    Project one turn's canonical events (wire shape: ``{turn, seq, event_type,
    agent_name, payload}``) + the turn's snapshot into the four-section view.

    Pure: no LLM, no DB, no mutation — same inputs, same output.
    """
    narrative: List[Dict[str, Any]] = []
    immediate: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    state_lines: List[Dict[str, Any]] = []

    for e in turn_events:
        etype = e["event_type"]
        p = e.get("payload") or {}
        seq = e["seq"]

        if etype == "agent.response":
            speaker = p.get("speaker") or e.get("agent_name") or "?"
            message = p.get("message")
            if message is None:
                message = p.get("content", "")
            entry: Dict[str, Any] = {
                "speaker": speaker,
                "utterance": message,  # verbatim — never paraphrased
                "source_seq": seq,
            }
            if p.get("injected"):
                entry["injected"] = True
            narrative.append(entry)

        elif etype == "goal.updated":
            immediate.append(_line(
                f"{p.get('agent')} changed goals: {p.get('before')} -> {p.get('after')}",
                seq,
            ))
            state_lines.append(_line(
                f"goals[{p.get('agent')}] = {p.get('after')}", seq))

        elif etype == "relationship.updated":
            immediate.append(_line(
                f"{p.get('agent')} now sees {p.get('other')} as: {p.get('stance')}",
                seq,
            ))
            state_lines.append(_line(
                f"relationship[{p.get('agent')} -> {p.get('other')}] = {p.get('stance')}",
                seq,
            ))

        elif etype == "memory.formed":
            state_lines.append(_line(
                f"{p.get('agent')} formed a memory: {p.get('content')}", seq))

        elif etype == "agent.reflected":
            state_lines.append(_line(
                f"{p.get('agent')} formed a belief: {p.get('belief')}", seq))

        elif etype == "thread.opened":
            deferred.append(_line(
                f"new {p.get('thread_type')} thread [{p.get('id')}]: {p.get('description')}",
                seq,
            ))

        elif etype == "thread.resolved":
            immediate.append(_line(
                f"thread [{p.get('id')}] resolved: {p.get('description')} "
                f"(opened turn {p.get('origin_turn')})",
                seq,
            ))

        elif etype == "thread.abandoned":
            immediate.append(_line(
                f"thread [{p.get('id')}] abandoned: {p.get('description')}", seq))

        elif etype == "validation.flagged":
            immediate.append(_line(
                f"turn emitted flagged: violates {p.get('principle')} ({p.get('reason')})",
                seq,
            ))

        elif etype == "sim.capped":
            immediate.append(_line(
                f"run capped at ${p.get('total_cost_usd')} (cap ${p.get('cap_usd')})",
                seq,
            ))

    # Possibilities: the OPEN threads as of this turn — real ledger state, not
    # generated suggestions. Explicitly non-limiting.
    possibilities: List[Dict[str, Any]] = []
    if snapshot is not None:
        for t in snapshot.pending_threads:
            if t.status != "open":
                continue
            possibilities.append({
                "thread_id": t.id,
                "thread_type": t.thread_type,
                "description": t.description,
                "opened_turn": t.origin_turn,
            })

    return {
        "turn": turn,
        "narrative": {
            "entries": narrative,
            "note": None if narrative else "No utterance was recorded for this turn.",
        },
        "consequences": {
            "immediate": immediate,
            "deferred": deferred,
            "note": None if (immediate or deferred)
            else "No state-change events were recorded for this turn.",
        },
        "updated_state": {
            "deltas": state_lines,
            "note": None if state_lines
            else "No state deltas were recorded for this turn.",
        },
        "possibilities": {
            "open_threads": possibilities,
            "non_limiting": True,
            "note": None if possibilities
            else "No open threads; possibilities are unconstrained.",
        },
    }
