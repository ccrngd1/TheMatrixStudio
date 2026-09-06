# SPDX-License-Identifier: Apache-2.0
"""
Phase 4a — the priority-hierarchy validation gate.

The PROJECT-SPEC §4a hierarchy (world coherence > causality > continuity >
agent agency > character consistency > dramatic impact > novelty) becomes an
enforced PRE-EMIT pass: after a turn is generated and before it is committed as
``agent.response``, the candidate utterance is checked in hierarchy order. On a
violation the engine REGENERATES the turn (retry budget 1) and, if the retry
still violates, emits the last attempt with a ``validation.flagged`` event.

Honesty gate (hard invariant, carried from 2c): this module only ever renders a
VERDICT on a candidate utterance. It never edits, truncates, or "fixes" model
output — rewriting an utterance in place would fabricate cognition.

Mechanism (CC working decision: heuristic-first, cost-controlled):
  * A small set of deterministic heuristics runs on every gated turn. Each is
    deliberately high-precision and maps to one hierarchy principle:
      - coherence:   the speaker breaks the one-speaker frame by emitting
                     dialogue attributed to ANOTHER cast member ("Ben: ...").
      - continuity:  the utterance verbatim-repeats a recent message (a state
                     loop; the timeline stops progressing). Applies only to
                     utterances >= DUPLICATE_MIN_CHARS — short conversational
                     replies ("I agree.") legitimately repeat.
      - agency:      the utterance flatly negates another participant's freedom
                     of choice ("you have no choice", "you cannot refuse", ...).
                     This check is REUSED by the 4c adaptive-pressure guard.
      - character_consistency: the speaker claims, first-person, to BE another
                     cast member ("I am Ben ...").
  * One fuzzy heuristic (near-duplicate: high token overlap with a recent
    message without being verbatim) only raises a SUSPICION; a suspicion
    triggers a single small LLM confirmation call (``llm_confirm``). This is
    the "selective LLM check only on suspected violations" cost control: clean
    turns never pay for an extra model call.
  * If the confirmation call itself fails, the suspicion is dropped (fail-open)
    — a broken checker must never spiral a run into regeneration. Confident
    heuristic verdicts need no LLM and are unaffected.

Heuristics are honest about being heuristics: they can miss violations (no
semantic world-model) and can rarely false-positive (e.g. a speaker QUOTING
"I am Ben"). The bounded reject-and-regenerate design absorbs false positives:
worst case a turn is regenerated once and emitted flagged — never blocked,
never rewritten.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

import litellm

from matrix_studio.jsonio import extract_json_object

logger = logging.getLogger(__name__)

# §4a priority order, highest first. Checks run in this order and report the
# HIGHEST violated principle.
PRIORITY_HIERARCHY = (
    "coherence",
    "causality",
    "continuity",
    "agency",
    "character_consistency",
    "dramatic_impact",
    "novelty",
)

# Verbatim-repeat detection only applies to utterances at least this long
# (normalized). Short conversational replies repeat naturally and flagging them
# would make the gate a false-positive machine.
DUPLICATE_MIN_CHARS = 40

# How many recent messages the duplicate/near-duplicate checks look back over.
RECENT_WINDOW = 10

# Token-overlap (Jaccard) threshold above which a non-verbatim utterance is
# SUSPECTED of being a degenerate near-repeat (confirmed selectively by LLM).
NEAR_DUP_JACCARD = 0.8

# Phrases that flatly negate a participant's freedom of choice. Deliberately
# small and unambiguous — this is the agency check the 4c pressure guard reuses.
AGENCY_NEGATION_PHRASES = (
    "you have no choice",
    "you cannot refuse",
    "you can't refuse",
    "you cannot choose",
    "you can't choose",
    "you must obey",
    "you must comply",
    "your choice doesn't matter",
    "your choice does not matter",
    "there is nothing you can do",
    "you are not allowed to decide",
    "no matter what you decide, the outcome is already fixed",
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def check_agency(text: str) -> Optional[str]:
    """Return a reason string if ``text`` flatly negates a participant's
    freedom of choice, else None. Shared by the 4a gate and the 4c
    adaptive-pressure guard (pressure that negates choice is rejected)."""
    low = _normalize(text)
    for phrase in AGENCY_NEGATION_PHRASES:
        if phrase in low:
            return f"utterance negates participant choice: {phrase!r}"
    return None


def heuristic_check(
    utterance: str,
    speaker_name: str,
    agent_names: List[str],
    conversation: List[Dict[str, Any]],
    citation_context: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Run the deterministic checks in hierarchy order over one candidate
    utterance. Pure and cheap — no LLM call, no state mutation.

    Returns ``{"result": "ok"|"violation"|"suspect", "principle": str|None,
    "reason": str|None}``. ``"suspect"`` means a fuzzy signal that should be
    confirmed by :func:`llm_confirm` before being treated as a violation.
    """
    others = [n for n in agent_names if n != speaker_name]

    # coherence (Phase 5i): a citation the speaker cannot legitimately make —
    # a document nobody retrieved, or another persona's document asserted without
    # crediting them. Ranked with coherence because a false citation corrupts the
    # exported record, which is the artifact this tool exists to produce.
    # Skipped entirely when no citation context is supplied (pre-5i behavior).
    if citation_context is not None:
        from matrix_studio.citations import analyse_citations, citation_violation

        cites = analyse_citations(
            utterance, speaker_name, agent_names, citation_context
        )
        reason = citation_violation(cites)
        if reason:
            return {
                "result": "violation",
                "principle": "citation_integrity",
                "reason": reason,
            }

    # coherence: the model emitted dialogue AS another cast member, breaking
    # the one-speaker-per-turn frame the sim runs on.
    first_line = utterance.strip().splitlines()[0] if utterance.strip() else ""
    for other in others:
        if re.match(rf"^\s*{re.escape(other)}\s*:", first_line, re.IGNORECASE):
            return {
                "result": "violation",
                "principle": "coherence",
                "reason": f"speaker {speaker_name!r} emitted dialogue attributed to {other!r}",
            }

    recent = conversation[-RECENT_WINDOW:]
    norm = _normalize(utterance)

    # continuity: verbatim repeat of a recent message (timeline loop). Only for
    # long-enough utterances (see DUPLICATE_MIN_CHARS).
    if len(norm) >= DUPLICATE_MIN_CHARS:
        for msg in recent:
            if _normalize(str(msg.get("content", ""))) == norm:
                return {
                    "result": "violation",
                    "principle": "continuity",
                    "reason": (
                        f"utterance verbatim-repeats turn {msg.get('turn')} "
                        f"by {msg.get('speaker')!r}"
                    ),
                }

    # agency: flat negation of a participant's freedom of choice.
    agency_reason = check_agency(utterance)
    if agency_reason:
        return {"result": "violation", "principle": "agency", "reason": agency_reason}

    # character consistency: first-person claim to BE another cast member.
    for other in others:
        if re.search(rf"\bi\s+am\s+{re.escape(other)}\b", utterance, re.IGNORECASE):
            return {
                "result": "violation",
                "principle": "character_consistency",
                "reason": f"speaker {speaker_name!r} claims to be {other!r}",
            }

    # causality (fuzzy, suspect-only): a near-verbatim repeat — degenerate
    # generation that does not follow from the prior state. Confirmed by a
    # selective LLM check, never treated as a violation on its own.
    if len(norm) >= DUPLICATE_MIN_CHARS:
        toks = _tokens(utterance)
        if toks:
            for msg in recent:
                prior = str(msg.get("content", ""))
                if _normalize(prior) == norm:
                    continue  # verbatim handled above
                ptoks = _tokens(prior)
                if not ptoks:
                    continue
                jaccard = len(toks & ptoks) / len(toks | ptoks)
                if jaccard >= NEAR_DUP_JACCARD:
                    return {
                        "result": "suspect",
                        "principle": "causality",
                        "reason": (
                            f"near-verbatim overlap ({jaccard:.2f}) with turn "
                            f"{msg.get('turn')} by {msg.get('speaker')!r}"
                        ),
                    }

    return {"result": "ok", "principle": None, "reason": None}


async def llm_confirm(
    utterance: str,
    speaker_name: str,
    principle: str,
    reason: str,
    conversation: List[Dict[str, Any]],
    settings,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Selective LLM confirmation of a SUSPECTED violation (cost control: only
    called when a fuzzy heuristic raised suspicion). One small JSON call.

    Returns ``{"violation": bool, "tokens_in", "tokens_out", "cost_usd"}``.
    On any error the suspicion is dropped (``violation: False``) — fail-open,
    so a broken checker never spirals a run into regeneration.
    """
    recent = conversation[-RECENT_WINDOW:]
    conv_text = "\n".join(
        f"{m.get('speaker')}: {m.get('content')}" for m in recent
    )
    prompt = f"""You are a strict simulation-consistency validator.

Recent transcript:
{conv_text}

Candidate next utterance by {speaker_name}:
{utterance}

A heuristic suspects a violation of the "{principle}" principle: {reason}

Judge ONLY whether this candidate utterance genuinely violates that principle
(e.g. it is a degenerate repeat that does not advance the conversation, rather
than a legitimate deliberate echo). Respond with ONLY a JSON object:
{{"violation": true or false}}"""

    try:
        kwargs: Dict[str, Any] = dict(
            model=model or settings.litellm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=50,
            response_format={"type": "json_object"},
        )
        response = await litellm.acompletion(**kwargs)
        raw = response.choices[0].message.content.strip()
        # Tolerant parse. A bare json.loads here meant the gate silently dropped
        # EVERY heuristic suspicion against a model that fences its JSON: the
        # JSONDecodeError hit the fail-open handler below and became
        # violation: False, so the selective confirmation had never confirmed
        # anything. See matrix_studio/jsonio.py.
        parsed = extract_json_object(raw)
        if parsed is None:
            logger.warning(
                "Validation LLM confirm returned unparseable output (suspicion dropped): %r",
                raw[:200],
            )
            return {"violation": False, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
        violation = bool(parsed.get("violation", False))
        usage = response.usage
        tokens_in = usage.prompt_tokens if usage else 0
        tokens_out = usage.completion_tokens if usage else 0
        cost_usd = 0.0
        if hasattr(response, "_hidden_params") and "response_cost" in response._hidden_params:
            cost_usd = response._hidden_params["response_cost"]
        return {
            "violation": violation,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
        }
    except Exception as e:  # noqa: BLE001 - fail-open: checker must never break a run
        logger.warning("Validation LLM confirm failed (suspicion dropped): %s", e)
        return {"violation": False, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}


async def validate_utterance(
    utterance: str,
    speaker_name: str,
    agent_names: List[str],
    conversation: List[Dict[str, Any]],
    settings,
    model: Optional[str] = None,
    citation_context: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Full pre-emit validation of one candidate utterance: heuristics first, a
    selective LLM confirmation only on suspicion.

    Returns a verdict dict:
    ``{"ok": bool, "principle": str|None, "reason": str|None,
       "method": "heuristic"|"llm"|None,
       "llm_tokens_in", "llm_tokens_out", "llm_cost_usd"}``
    (LLM usage fields are 0 when no confirmation call was made.)
    """
    verdict = heuristic_check(
        utterance, speaker_name, agent_names, conversation,
        citation_context=citation_context,
    )
    usage = {"llm_tokens_in": 0, "llm_tokens_out": 0, "llm_cost_usd": 0.0}

    if verdict["result"] == "ok":
        return {"ok": True, "principle": None, "reason": None, "method": None, **usage}

    if verdict["result"] == "violation":
        return {
            "ok": False,
            "principle": verdict["principle"],
            "reason": verdict["reason"],
            "method": "heuristic",
            **usage,
        }

    # suspect -> selective LLM confirmation.
    confirm = await llm_confirm(
        utterance,
        speaker_name,
        verdict["principle"],
        verdict["reason"],
        conversation,
        settings,
        model=model,
    )
    usage = {
        "llm_tokens_in": confirm["tokens_in"],
        "llm_tokens_out": confirm["tokens_out"],
        "llm_cost_usd": confirm["cost_usd"],
    }
    if confirm["violation"]:
        return {
            "ok": False,
            "principle": verdict["principle"],
            "reason": verdict["reason"],
            "method": "llm",
            **usage,
        }
    return {"ok": True, "principle": None, "reason": None, "method": "llm", **usage}
