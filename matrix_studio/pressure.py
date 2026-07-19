# SPDX-License-Identifier: Apache-2.0
"""
Phase 4c — adaptive-pressure intervention (EXPERIMENTAL, opt-in).

A new member of the 2b branch-from-checkpoint intervention family: observe
run-level signals (repetition, stalled threads, turn budget remaining) at the
fork and inject ONE narrator-voiced WORLD event as a branch turn, to raise the
stakes when a run is going flat. Always a branch mutation — history is never
edited in place.

Agency guard (hard, enforced here AND at the 4a gate):
  * Pressure modulates the WORLD (environment, external events, NPC actions).
    It never speaks for a participant, never edits their goals, and never
    negates their freedom of choice — agency (#4 in the §4a hierarchy)
    outranks dramatic impact (#6).
  * Every generated pressure text is checked with the same
    ``validation.check_agency`` the 4a gate uses. A violating text is
    regenerated once; if it still violates, the WHOLE intervention is REJECTED
    (PressureRejectedError -> HTTP 422). Adaptive pressure never falls back to
    emitting agency-violating content, and it is never rewritten in place.

Honesty notes:
  * The observed signals are computed from real state only (transcript +
    pending-thread ledger + budget) and are recorded verbatim on the
    ``pressure.applied`` event, so the intervention is fully auditable.
  * A failed/empty LLM generation rejects the intervention rather than
    fabricating a fallback event.

OFF by default (``settings.adaptive_pressure_enabled``); the API refuses the
mutation kind entirely while disabled.
"""

import logging
import re
from typing import Any, Dict, List, Optional

import litellm

from matrix_studio.state import PendingThread
from matrix_studio.validation import check_agency

logger = logging.getLogger(__name__)

# How many recent messages the repetition signal looks over.
SIGNAL_WINDOW = 6

# Token-overlap (Jaccard) threshold above which two recent messages count as
# repetitive for the repetition signal.
REPETITION_JACCARD = 0.6

PRESSURE_RETRY_BUDGET = 1


class PressureRejectedError(ValueError):
    """The generated pressure violated the agency guard after the retry budget,
    or generation failed — the intervention is rejected (never emitted,
    never rewritten). Surfaced by the API as HTTP 422."""


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def observe_signals(
    conversation: List[Dict[str, Any]],
    pending_threads: List[PendingThread],
    from_turn: int,
    max_messages: int,
    stale_after: int = 5,
) -> Dict[str, Any]:
    """
    Compute the run-level signals adaptive pressure acts on, from real state
    only. Pure and deterministic.

    Returns ``{repetition, stale_threads, open_threads, budget_remaining,
    as_of_turn}`` where ``repetition`` is the max pairwise token-overlap among
    the last SIGNAL_WINDOW messages (0.0 when fewer than 2) and
    ``stale_threads`` lists open threads older than ``stale_after`` turns.
    """
    recent = conversation[-SIGNAL_WINDOW:]
    repetition = 0.0
    for i in range(len(recent)):
        for j in range(i + 1, len(recent)):
            a = _tokens(str(recent[i].get("content", "")))
            b = _tokens(str(recent[j].get("content", "")))
            if a and b:
                repetition = max(repetition, len(a & b) / len(a | b))

    open_threads = [t for t in pending_threads if t.status == "open"]
    stale = [
        {"id": t.id, "description": t.description, "age": from_turn - t.origin_turn}
        for t in open_threads
        if (from_turn - t.origin_turn) >= stale_after
    ]

    return {
        "repetition": round(repetition, 3),
        "stale_threads": stale,
        "open_threads": len(open_threads),
        "budget_remaining": max(0, max_messages - from_turn),
        "as_of_turn": from_turn,
    }


async def generate_pressure(
    topic: str,
    conversation: List[Dict[str, Any]],
    signals: Dict[str, Any],
    participants: List[str],
    settings,
    model: Optional[str] = None,
    focus: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate ONE narrator-voiced world event that raises the stakes, honouring
    the agency guard. Regenerates once on an agency violation; rejects
    (raises :class:`PressureRejectedError`) if the retry still violates or
    generation fails.

    Returns ``{"content", "tokens_in", "tokens_out", "cost_usd", "attempts"}``.
    """
    recent = conversation[-10:]
    conv_text = "\n".join(
        f"{m.get('speaker')}: {m.get('content')}" for m in recent
    )
    stale_lines = "\n".join(
        f"- {t['description']} (dangling for {t['age']} turns)"
        for t in signals.get("stale_threads", [])
    ) or "- none"
    focus_line = f"\nDirection requested by the operator: {focus}" if focus else ""
    names = ", ".join(participants)

    prompt = f"""You are the narrator of a simulated conversation about "{topic}".
The discussion is losing momentum (repetition score {signals['repetition']}, {signals['open_threads']} unresolved threads, {signals['budget_remaining']} turns remaining).

Recent conversation:
{conv_text}

Dangling threads worth paying off:
{stale_lines}{focus_line}

Write ONE short narrator interjection (2-3 sentences) describing an EXTERNAL world event, new information, or NPC action that raises the stakes or forces the open questions to a head.

HARD RULES (violations are rejected):
- Describe only the WORLD or outside parties. Do not speak for, decide for, or describe the thoughts/actions of {names}.
- Never remove or negate any participant's freedom to choose how to respond.
- Do not resolve the conversation for them; create pressure, not conclusions.

Narrator interjection:"""

    tokens_in = 0
    tokens_out = 0
    cost_usd = 0.0
    last_reason = "generation failed"

    for attempt in range(PRESSURE_RETRY_BUDGET + 1):
        try:
            response = await litellm.acompletion(
                model=model or settings.litellm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=settings.litellm_temperature,
                max_tokens=300,
            )
            content = (response.choices[0].message.content or "").strip()
            usage = response.usage
            tokens_in += usage.prompt_tokens if usage else 0
            tokens_out += usage.completion_tokens if usage else 0
            if hasattr(response, "_hidden_params") and "response_cost" in response._hidden_params:
                cost_usd += response._hidden_params["response_cost"]
        except Exception as e:  # noqa: BLE001 - reject, never fabricate a fallback
            logger.error("Adaptive pressure generation failed: %s", e, exc_info=True)
            raise PressureRejectedError(
                f"adaptive_pressure: generation failed ({e})"
            ) from e

        if not content:
            last_reason = "empty generation"
            continue

        # HARD agency guard — same check the 4a gate runs. A violating text is
        # regenerated once, then the intervention is rejected outright.
        reason = check_agency(content)
        if reason is None:
            return {
                "content": content,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": cost_usd,
                "attempts": attempt + 1,
            }
        last_reason = reason
        logger.warning(
            "Adaptive pressure attempt %d rejected by agency guard: %s",
            attempt + 1, reason,
        )

    raise PressureRejectedError(
        f"adaptive_pressure: rejected by the agency guard after "
        f"{PRESSURE_RETRY_BUDGET + 1} attempts ({last_reason})"
    )
