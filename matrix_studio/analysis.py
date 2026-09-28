# SPDX-License-Identifier: Apache-2.0
"""
Post-run analysis layer (Phase 1.5) — read-only summary + aside conversations.

Every capability here is a single ``litellm.acompletion`` over a *finished*
run's transcript with a different system prompt:

- ``generate_summary`` — structured analyst summary (consensus / dissenters /
  key_ideas / open_questions / overview). Strict JSON, validated, one retry,
  graceful plain-text fallback — it must never crash the run or the UI.
- ``analyst_reply`` / ``persona_reply`` / ``room_reply`` — one aside turn from
  the analyst, a single persona (using that agent's REAL stored persona text),
  or every persona in the room.

READ-ONLY INVARIANT: nothing in this module writes to the canonical event log,
snapshot, or a run's recorded cost. It only reads a run's transcript/cast and
returns model output + token/cost accounting for the caller to persist into the
additive Phase 1.5 tables (summaries / thread_messages). These are model-
generated ANALYSIS of the transcript, not ground truth or canonical persona
statements — callers label them as such.
"""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

# Deferred: importing litellm costs 1.7 s and this module's callers include the API
# Lambda, whose read routes never generate. See matrix_studio/lazy_litellm.py.
from matrix_studio.lazy_litellm import litellm

from matrix_studio.settings import get_settings

from matrix_studio.jsonio import extract_json_object

logger = logging.getLogger(__name__)

# The standard structured-summary field set. `overview` is always produced; the
# others are lists (possibly empty). Callers may request a subset via `fields`.
DEFAULT_SUMMARY_FIELDS = [
    "consensus",
    "dissenters",
    "key_ideas",
    "open_questions",
    "evidence_plan",
    "conditional_recommendation",
    "overview",
]

#: What the analyst writes in an evidence-plan column the conversation never supplied. Fixed wording,
#: because counting it is the measurement: how often a persona asks for evidence without saying what
#: result would move them, or what they expect it to show (BACKLOG "Conversations end in 'it depends'").
NOT_STATED = "not stated"

#: The evidence-plan columns, in the order the brief shows them.
EVIDENCE_PLAN_KEYS = ("data", "asked_by", "decision", "moves_them", "best_guess", "cheapest_way")

# Max personas contacted for a room aside, to keep a single aside turn bounded
# and its cost predictable (asides cost money — see the honesty gate).
MAX_ROOM_PERSONAS = 12

# The default analyst-role framing for a summary. This is the ONLY part of the
# summary system prompt a user may replace via a custom `instructions` — the
# non-negotiable guardrails in `_summary_system_prompt` (JSON schema block,
# JSON-only response, no-fabrication line) are always appended regardless, so a
# custom prompt can never break JSON parsing or the honesty gate. Exposed via
# GET /api/runs/{ref}/summary as `default_instructions` so the UI can prefill
# the editor and offer "reset to default."
DEFAULT_SUMMARY_INSTRUCTIONS = (
    "You are a neutral analyst summarizing a finished multi-agent "
    "conversation. Read the transcript and produce a STRUCTURED analysis."
)


# --------------------------------------------------------------------------- #
# LLM plumbing (single seam — tests patch this).
# --------------------------------------------------------------------------- #
async def _acompletion(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.4,
    max_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """
    One chat completion. Returns ``{content, tokens_in, tokens_out, cost_usd}``.

    This is the ONLY place analysis code talks to the model, so the whole layer
    is mocked in the test suite by patching this function. Model defaults to the
    run's configured model (passed in) or the global settings default — the EOL
    claude-3-5-sonnet model is never introduced here.
    """
    settings = get_settings()
    from matrix_studio.models import model_for

    resolved_model = model_for(model, "summary") or settings.litellm_model
    response = await litellm.acompletion(
        model=resolved_model,
        messages=messages,
        temperature=temperature,
        # Falls back to the SUMMARY budget, not the per-turn one. A turn is 2-4
        # sentences; an analysis of a whole transcript is not, and sharing
        # litellm_max_tokens truncated a real 24-turn summary mid-value.
        max_tokens=max_tokens or settings.summary_max_tokens,
    )
    content = response.choices[0].message.content or ""
    # A truncated reply comes back EMPTY, not partial, so the only way to tell "the model
    # declined" from "the model ran out of room" is this field. Three separate features
    # today lost hours to that: the 120-token selection cap, the closing-round synthesis,
    # and the ensemble report.
    finish_reason = getattr(response.choices[0], "finish_reason", None)
    usage = getattr(response, "usage", None)
    tokens_in = getattr(usage, "prompt_tokens", 0) if usage else 0
    tokens_out = getattr(usage, "completion_tokens", 0) if usage else 0
    cost_usd = 0.0
    hidden = getattr(response, "_hidden_params", None)
    if isinstance(hidden, dict) and hidden.get("response_cost") is not None:
        cost_usd = float(hidden["response_cost"] or 0.0)
    return {
        "content": content.strip(),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost_usd,
        "finish_reason": finish_reason,
    }


# --------------------------------------------------------------------------- #
# Transcript helpers.
# --------------------------------------------------------------------------- #
def format_transcript(conversation: List[Dict[str, Any]]) -> str:
    """Render a run's conversation as a plain ``Speaker: content`` transcript."""
    lines = []
    for msg in conversation:
        speaker = msg.get("speaker", "?")
        content = msg.get("content", "")
        lines.append(f"{speaker}: {content}")
    return "\n".join(lines)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Kept as a thin alias so existing callers and tests are undisturbed.

    The implementation moved to ``matrix_studio.jsonio`` after the same defect —
    a model fencing its JSON — was found to have silently disabled cognition and
    the Phase 4a validation gate, which had never learned the lesson this
    function encoded. One implementation now, so it cannot be learned twice and
    missed elsewhere.
    """
    return extract_json_object(text)


def _summary_system_prompt(
    fields: List[str],
    focus: Optional[str],
    instructions: Optional[str] = None,
) -> str:
    """
    Build the summary system prompt.

    The analyst-role framing (``DEFAULT_SUMMARY_INSTRUCTIONS``) is REPLACED by a
    user-supplied ``instructions`` when provided; ``focus`` still appends after.
    The non-negotiable GUARDRAILS are ALWAYS appended regardless of any custom
    instructions and cannot be dropped by the user:
      (a) the no-fabrication line (base strictly on the transcript),
      (b) the JSON schema block derived from ``fields``,
      (c) the "respond with ONLY a single JSON object" instruction.
    This keeps structured output parseable and the honesty gate intact even with
    a fully custom prompt.
    """
    field_specs = {
        "consensus": '"consensus": [ "point the group converged on", ... ]',
        "dissenters": '"dissenters": [ {"speaker": "name", "position": "what they objected to"}, ... ]',
        "key_ideas": '"key_ideas": [ "interesting idea / fact / novel framing surfaced", ... ]',
        "open_questions": '"open_questions": [ "unresolved thread worth pursuing", ... ]',
        "evidence_plan": (
            '"evidence_plan": [ {"data": "the specific data or evidence someone said they needed", '
            '"asked_by": "who asked for it", "decision": "the decision it would unlock", '
            '"moves_them": "the result that would move them, each way", '
            '"best_guess": "what anyone in the conversation expected it to show", '
            '"cheapest_way": "the cheapest way to get it that was mentioned"}, ... ]  '
            f'(one row per distinct evidence request; any value the conversation did not supply is '
            f'exactly "{NOT_STATED}" — never fill a gap yourself)'
        ),
        "conditional_recommendation": (
            '"conditional_recommendation": "If <the result>, do <A>; if not, do <B>. The cast\'s best '
            'guess is <guess>, so lean <A or B>." — built only from the evidence plan; where no '
            f'best guess was stated say the lean is {NOT_STATED}; empty string if nobody asked for evidence'
        ),
        "overview": '"overview": "a 2-4 sentence plain-English overview"',
    }
    requested = [field_specs[f] for f in fields if f in field_specs]
    schema_block = ",\n  ".join(requested)
    focus_line = (
        f"\n\nApply this focus when analyzing: {focus.strip()}"
        if focus and focus.strip()
        else ""
    )
    # Custom instructions replace ONLY the analyst-role framing; the default is
    # used when none provided (or an all-whitespace value is given).
    role = (
        instructions.strip()
        if instructions and instructions.strip()
        else DEFAULT_SUMMARY_INSTRUCTIONS
    )
    return (
        role + "\n\n"
        "Base every point strictly on what was actually said — do not invent "
        "content, positions, or speakers. Lists may be empty if nothing "
        "qualifies.\n\n"
        "Respond with ONLY a single JSON object of this exact shape (no prose, "
        "no code fence):\n{\n  " + schema_block + "\n}" + focus_line
    )


def _empty_summary(fields: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for f in fields:
        out[f] = "" if f in ("overview", "conditional_recommendation") else []
    return out


def _coerce_summary(obj: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
    """Keep only requested fields and coerce them to the expected shapes."""
    out = _empty_summary(fields)
    for f in fields:
        if f not in obj:
            continue
        val = obj[f]
        if f in ("overview", "conditional_recommendation"):
            out[f] = val if isinstance(val, str) else json.dumps(val)
        elif f == "evidence_plan":
            out[f] = [
                {k: (str(it.get(k) or "").strip() or NOT_STATED) for k in EVIDENCE_PLAN_KEYS}
                for it in (val if isinstance(val, list) else [])
                if isinstance(it, dict) and str(it.get("data") or "").strip()
            ]
        elif f == "dissenters":
            items = []
            if isinstance(val, list):
                for it in val:
                    if isinstance(it, dict):
                        items.append(
                            {
                                "speaker": str(it.get("speaker", "")),
                                "position": str(
                                    it.get("position", it.get("objection", ""))
                                ),
                            }
                        )
                    elif isinstance(it, str):
                        items.append({"speaker": "", "position": it})
            out[f] = items
        else:  # list of strings
            if isinstance(val, list):
                out[f] = [str(x) for x in val]
            elif isinstance(val, str) and val:
                out[f] = [val]
    return out


async def generate_summary(
    conversation: List[Dict[str, Any]],
    topic: str,
    fields: Optional[List[str]] = None,
    focus: Optional[str] = None,
    model: Optional[str] = None,
    instructions: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate a structured analyst summary of a completed conversation.

    ``instructions`` (optional) REPLACES the default analyst-role framing while
    the guardrails always remain (see ``_summary_system_prompt``). It is
    backward-compatible: omitting it uses the default framing.

    Returns ``{payload, tokens_in, tokens_out, cost_usd, parsed, instructions}``
    where ``payload`` is the structured (or fallback) summary, ``parsed`` is True
    when strict JSON was obtained, and ``instructions`` is the effective
    role-framing text that created it (``None`` when the default was used, so
    callers can persist NULL). On a parse failure it retries ONCE, then falls
    back to a plain-text overview so it never crashes the run/UI.
    """
    fields = fields or list(DEFAULT_SUMMARY_FIELDS)
    transcript = format_transcript(conversation)
    system = _summary_system_prompt(fields, focus, instructions)
    # The effective instructions we persist: NULL (None) when the default was
    # used so the UI knows to fall back to `default_instructions`.
    effective_instructions = (
        instructions.strip()
        if instructions and instructions.strip()
        else None
    )
    user = (
        f'The conversation topic was: "{topic}".\n\n'
        f"Transcript:\n{transcript}\n\n"
        "Produce the JSON analysis now."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    tokens_in = tokens_out = 0
    cost_usd = 0.0
    last_content = ""

    # One attempt + one retry for strict JSON.
    for attempt in range(2):
        try:
            result = await _acompletion(messages, model=model, temperature=0.3)
        except Exception as e:  # noqa: BLE001 - never crash the run/UI
            logger.warning("Summary generation LLM call failed: %s", e)
            payload = _empty_summary(fields)
            if "overview" in fields:
                payload["overview"] = (
                    "Summary generation is unavailable (the analysis model call "
                    "failed). This is a model/analysis error, not part of the run."
                )
            return {
                "payload": payload,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": cost_usd,
                "parsed": False,
                "instructions": effective_instructions,
            }

        tokens_in += result["tokens_in"]
        tokens_out += result["tokens_out"]
        cost_usd += result["cost_usd"]
        last_content = result["content"]

        obj = _extract_json(last_content)
        if obj is not None:
            return {
                "payload": _coerce_summary(obj, fields),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": cost_usd,
                "parsed": True,
                "instructions": effective_instructions,
            }

        if attempt == 0:
            # Nudge the model toward valid JSON on the single retry.
            messages.append({"role": "assistant", "content": last_content})
            messages.append(
                {
                    "role": "user",
                    "content": "That was not valid JSON. Reply with ONLY the JSON "
                    "object described, nothing else.",
                }
            )

    # Graceful plain-text fallback: keep the model's prose as the overview.
    payload = _empty_summary(fields)
    if "overview" in fields:
        payload["overview"] = last_content or "No summary could be generated."
    else:
        # Caller didn't request overview; stash prose so nothing is lost.
        payload["overview"] = last_content
    return {
        "payload": payload,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost_usd,
        "parsed": False,
        "instructions": effective_instructions,
    }


# --------------------------------------------------------------------------- #
# Aside replies (read-only, post-hoc reflection framing).
# --------------------------------------------------------------------------- #
# A shared framing line so persona/room replies never pretend the canonical
# conversation is continuing — every aside is post-hoc reflection.
_ASIDE_FRAMING = (
    "The group conversation has already FINISHED. You are now reflecting on it "
    "afterwards in a private side-discussion with a reviewer. Your reply here "
    "does NOT continue or change the original conversation and the other "
    "participants will not see it. Answer in at most about 150 words: lead with "
    "the point, and offer to expand rather than expanding."
)

#: The output budget for one aside reply. Asides used to fall back to the SUMMARY budget (8,000
#: tokens), and a reply is generated inside the HTTP request, which the deployed API cuts off at 30 s:
#: a persona aside timed out at 30 s on 2026-09-28 (a 504 in the UI) and no persona aside had ever
#: returned on the deployed stack. Measured on the deployed API against a 40-turn, six-persona run:
#: output runs at ~30–35 tokens/s, and at 700 tokens an open question took 25.5 s — past the 24 s
#: server deadline. 450 is ~15 s of output plus reading the transcript, inside the deadline with room.
ASIDE_MAX_TOKENS = 450

#: The budget when the reply is generated by the aside WORKER (`step_handlers.aside`), outside any HTTP
#: request. No 30 s limit there, so the reply may say what the question needs; the brevity instruction
#: still stands, this is the ceiling, not the target.
ASIDE_BACKGROUND_MAX_TOKENS = 1500


def _history_messages(
    thread_history: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, str]]:
    """Convert stored thread messages into chat turns for multi-turn context."""
    out: List[Dict[str, str]] = []
    for m in thread_history or []:
        role = "user" if m.get("role") == "user" else "assistant"
        out.append({"role": role, "content": m.get("content", "")})
    return out


async def analyst_reply(
    user_message: str,
    conversation: List[Dict[str, Any]],
    topic: str,
    thread_history: Optional[List[Dict[str, Any]]] = None,
    model: Optional[str] = None,
    max_tokens: int = 0,
) -> Dict[str, Any]:
    """
    Neutral analyst answering ABOUT the finished conversation, grounded in the
    transcript. Returns ``{speaker, content, tokens_in, tokens_out, cost_usd}``.
    """
    transcript = format_transcript(conversation)
    system = (
        "You are a neutral analyst helping a reviewer understand a finished "
        f'multi-agent conversation about "{topic}". Answer using ONLY the '
        "transcript below — do not invent statements, positions, or facts that "
        "are not supported by it; if the transcript does not address the "
        "question, say so. You are an outside observer, not one of the "
        f"participants.\n\nTranscript:\n{transcript}"
    )
    messages = [{"role": "system", "content": system}]
    messages.extend(_history_messages(thread_history))
    messages.append({"role": "user", "content": user_message})
    result = await _acompletion(messages, model=model, temperature=0.4, max_tokens=max_tokens or ASIDE_MAX_TOKENS)
    return {"speaker": "analyst", **result}


async def persona_reply(
    user_message: str,
    persona_name: str,
    persona_text: str,
    conversation: List[Dict[str, Any]],
    topic: str,
    thread_history: Optional[List[Dict[str, Any]]] = None,
    model: Optional[str] = None,
    max_tokens: int = 0,
) -> Dict[str, Any]:
    """
    A single persona answering IN CHARACTER in an aside, using that agent's REAL
    stored persona text (never invented). Framed as post-hoc reflection.
    """
    transcript = format_transcript(conversation)
    system = (
        f"{persona_text}\n\n"
        f"You are {persona_name}. You took part in a group conversation about "
        f'"{topic}". {_ASIDE_FRAMING}\n\n'
        "Stay in character as yourself. You may expand on, defend, or "
        "fact-check points you made, but ground yourself in what was actually "
        f"said.\n\nFull transcript of the finished conversation:\n{transcript}"
    )
    messages = [{"role": "system", "content": system}]
    messages.extend(_history_messages(thread_history))
    messages.append({"role": "user", "content": user_message})
    result = await _acompletion(messages, model=model, temperature=0.6, max_tokens=max_tokens or ASIDE_MAX_TOKENS)
    return {"speaker": persona_name, **result}


async def room_reply(
    user_message: str,
    cast: List[Dict[str, Any]],
    conversation: List[Dict[str, Any]],
    topic: str,
    thread_history: Optional[List[Dict[str, Any]]] = None,
    model: Optional[str] = None,
    max_tokens: int = 0,
) -> Dict[str, Any]:
    """
    The whole room reacting to a prompt in an aside — one in-character call per
    persona (bounded by MAX_ROOM_PERSONAS), returned INTO the thread only. This
    does NOT resume the canonical run.

    Returns ``{speaker: 'room', content, replies: [...], tokens_in, tokens_out,
    cost_usd}`` where each entry in ``replies`` is a per-persona reply dict and
    ``content`` is a combined transcript-style rendering for storage/display.
    """
    selected = cast[:MAX_ROOM_PERSONAS]
    # CONCURRENTLY. One after another, six personas at ~8 s each could never finish inside the
    # deployed API's 30 s limit; together the room takes about as long as its slowest reply. Order is
    # the cast's, as before, because gather preserves it.
    replies: List[Dict[str, Any]] = list(await asyncio.gather(*(
        persona_reply(
            user_message=user_message,
            persona_name=persona.get("name", "?"),
            persona_text=persona.get("persona", ""),
            conversation=conversation,
            topic=topic,
            thread_history=thread_history,
            model=model,
            max_tokens=max_tokens,
        )
        for persona in selected
    )))
    total_in = sum(r["tokens_in"] for r in replies)
    total_out = sum(r["tokens_out"] for r in replies)
    total_cost = sum(r["cost_usd"] for r in replies)

    combined = "\n\n".join(f"{r['speaker']}: {r['content']}" for r in replies)
    return {
        "speaker": "room",
        "content": combined,
        "replies": replies,
        "tokens_in": total_in,
        "tokens_out": total_out,
        "cost_usd": total_cost,
    }
