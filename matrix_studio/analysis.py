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
from typing import Any, Dict, List, Optional, Sequence

# Deferred: importing litellm costs 1.7 s and this module's callers include the API
# Lambda, whose read routes never generate. See matrix_studio/lazy_litellm.py.
from matrix_studio.lazy_litellm import litellm

from matrix_studio.settings import get_settings

from matrix_studio.jsonio import extract_json_object

logger = logging.getLogger(__name__)

# The standard structured-summary field set. `overview` is always produced; the
# others are lists (possibly empty). Callers may request a subset via `fields`.
# `overview` comes first because that is where the prompt asks for it; see
# `_summary_system_prompt` for why.
DEFAULT_SUMMARY_FIELDS = [
    "overview",
    "consensus",
    "dissenters",
    "key_ideas",
    "open_questions",
    "evidence_plan",
    "conditional_recommendation",
    "concerns",
]

#: What the analyst writes in an evidence-plan column the conversation never supplied. Fixed wording,
#: because counting it is the measurement: how often a persona asks for evidence without saying what
#: result would move them, or what they expect it to show (BACKLOG "Conversations end in 'it depends'").
NOT_STATED = "not stated"

#: The evidence-plan columns, in the order the brief shows them.
EVIDENCE_PLAN_KEYS = ("data", "asked_by", "decision", "moves_them", "best_guess", "cheapest_way")

#: The `concerns` columns: one row per authored underlying concern. The post-run analysis reads the
#: concerns whether or not the run withheld them (owner decision, 2026-10-02); on a withheld run this is
#: the reveal. `surfaced` and `addressed` are one of `CONCERN_VERDICTS`; `where` is a short quote or turn
#: reference, and `NOT_STATED` when the concern never came up.
CONCERN_KEYS = ("speaker", "concern", "surfaced", "where", "addressed")
CONCERN_VERDICTS = ("yes", "partly", "no")

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

    The overview is asked for FIRST. Until 2026-10-01 it came last, straight
    after the conditional recommendation ("... so lean A"), and the model
    would end its reply on that recommendation and close the object without
    writing an overview. Such a reply parses, so the summary was stored with
    every field but the one the UI shows first. Measured on the latest stored
    summary of each run: 0 of 77 from before the evidence plan and
    conditional recommendation were added (2026-09-28) lacked an overview;
    10 of the 50 after did, untruncated. Replayed on one of those
    transcripts (both of its stored summaries lacked it): the old prompt
    dropped it again, finish_reason "stop"; the overview first, all else
    unchanged, kept it 2 of 2; the overview still last with the field rules
    moved out of the shape dropped it 1 of 2; this prompt kept it 3 of 3.
    First is also the place a truncated reply keeps (`jsonio` salvages the
    leading fields); 2 more of those 50 were truncated and lost it that way.

    The field rules sit AFTER the shape rather than inside it, so the shape
    is a JSON object a model can copy and every line of it is a key to write.
    """
    field_specs = {
        "overview": '"overview": "a 2-4 sentence plain-English overview"',
        "consensus": '"consensus": [ "point the group converged on", ... ]',
        "dissenters": '"dissenters": [ {"speaker": "name", "position": "what they objected to"}, ... ]',
        "key_ideas": '"key_ideas": [ "interesting idea / fact / novel framing surfaced", ... ]',
        "open_questions": '"open_questions": [ "unresolved thread worth pursuing", ... ]',
        "evidence_plan": (
            '"evidence_plan": [ {"data": "the specific data or evidence someone said they needed", '
            '"asked_by": "who asked for it", "decision": "the decision it would unlock", '
            '"moves_them": "the result that would move them, each way", '
            '"best_guess": "what anyone in the conversation expected it to show", '
            '"cheapest_way": "the cheapest way to get it that was mentioned"}, ... ]'
        ),
        "conditional_recommendation": (
            '"conditional_recommendation": "If <the result>, do <A>; if not, do <B>. The cast\'s best '
            'guess is <guess>, so lean <A or B>."'
        ),
        "concerns": (
            '"concerns": [ {"speaker": "name", "concern": "the underlying concern, as listed", '
            '"surfaced": "yes, partly or no", "where": "a short quote or turn reference showing it", '
            '"addressed": "yes, partly or no"}, ... ]'
        ),
    }
    # What a field must and must not contain; see the docstring for why it is not in the shape.
    field_rules = {
        "evidence_plan": (
            f'"evidence_plan": one row per distinct evidence request; any value the conversation did not '
            f'supply is exactly "{NOT_STATED}" — never fill a gap yourself.'
        ),
        "conditional_recommendation": (
            '"conditional_recommendation": built only from the evidence plan; where no best guess was '
            f'stated say the lean is {NOT_STATED}; an empty string if nobody asked for evidence.'
        ),
        "concerns": (
            '"concerns": one row per concern in the analyst-only list, in its order. "surfaced" is '
            "whether that worry came up in the conversation, in anyone's words; \"addressed\" is whether "
            f'anyone answered it or dealt with it; "where" is exactly "{NOT_STATED}" when it never came '
            "up. Judge only from the transcript: the list says what each persona was given, not what "
            "was said."
        ),
    }
    # `fields` may come from a client in any order, so the overview is moved to the front here, not
    # only in DEFAULT_SUMMARY_FIELDS.
    ordered = sorted((f for f in fields if f in field_specs), key=lambda f: f != "overview")
    schema_block = ",\n  ".join(field_specs[f] for f in ordered)
    rules = [field_rules[f] for f in ordered if f in field_rules]
    rules_block = (
        "\n\nWrite every key above"
        + (", the overview included" if "overview" in ordered else "")
        + "; a list with nothing in it is []."
        + "".join(f"\n- {r}" for r in rules)
    )
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
        "no code fence):\n{\n  " + schema_block + "\n}" + rules_block + focus_line
    )


def _coerce_concern(row: Dict[str, Any]) -> Dict[str, str]:
    """One `concerns` row in the fixed shape. A verdict outside yes/partly/no is `NOT_STATED` rather than
    guessed at, so "the analyst did not say" never reads as "no"."""
    out = {k: str(row.get(k) or "").strip() for k in CONCERN_KEYS}
    for k in ("surfaced", "addressed"):
        verdict = out[k].lower().rstrip(".")
        out[k] = verdict if verdict in CONCERN_VERDICTS else NOT_STATED
    out["where"] = out["where"] or NOT_STATED
    out["speaker"] = out["speaker"] or NOT_STATED
    return out


def concerns_note(concerns: Sequence[Dict[str, str]], *, withheld: bool) -> str:
    """The analyst-only list of authored concerns, appended to the summary request.

    Given in BOTH modes (owner decision, 2026-10-02): the analysis is where a hidden agenda is revealed,
    and where a plainly stated one is checked for having actually been said and answered. Labelled as
    not part of the transcript, because the no-fabrication guardrail is about the transcript and a
    concern that never came up must be reported as never coming up, not as said.
    """
    if not concerns:
        return ""
    lines = "\n".join(
        f"- {c.get('speaker')}" + (f" (behind “{c['position']}”)" if c.get("position") else "")
        + f": {c.get('concern')}"
        for c in concerns
    )
    how = (
        "They were told to keep these to themselves unless someone drew them out, so the others never "
        "saw them."
        if withheld else
        "They were told to state these openly when the position they sit behind came up."
    )
    return (
        "\n\nAnalyst-only context, NOT part of the transcript — the underlying concern each persona was "
        f"given behind its positions. {how}\n{lines}\n"
        "For each one, report from the transcript alone whether it came up and whether it was addressed."
    )


def _empty_summary(fields: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for f in fields:
        out[f] = "" if f in ("overview", "conditional_recommendation") else []
    return out


def _coerce_summary(obj: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
    """Keep only requested fields and coerce them to the expected shapes.

    A requested field the reply did not supply still gets its empty value, so
    every reader finds the shape it expects, and is NAMED in ``omitted``, which
    is present only when something was. Without that name an overview the model
    left out was a silent "" that every surface hid, and a reply cut off at the
    budget looked the same as one that was complete: `jsonio` returns the
    fields a truncated reply finished precisely so the missing ones can be
    seen, and padding them erased that. An overview of only whitespace counts
    as omitted too: unlike a list, it is never legitimately empty.
    """
    out = _empty_summary(fields)
    for f in fields:
        # A null is no value, not the string "null".
        if obj.get(f) is None:
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
        elif f == "concerns":
            out[f] = [
                _coerce_concern(it) for it in (val if isinstance(val, list) else [])
                if isinstance(it, dict) and str(it.get("concern") or "").strip()
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
    omitted = [
        f for f in fields
        if obj.get(f) is None or (f == "overview" and not out[f].strip())
    ]
    if omitted:
        out["omitted"] = omitted
    return out


async def generate_summary(
    conversation: List[Dict[str, Any]],
    topic: str,
    fields: Optional[List[str]] = None,
    focus: Optional[str] = None,
    model: Optional[str] = None,
    instructions: Optional[str] = None,
    context: str = "",
    concerns: Optional[Sequence[Dict[str, str]]] = None,
    concerns_withheld: bool = False,
) -> Dict[str, Any]:
    """
    Generate a structured analyst summary of a completed conversation.

    ``instructions`` (optional) REPLACES the default analyst-role framing while
    the guardrails always remain (see ``_summary_system_prompt``). It is
    backward-compatible: omitting it uses the default framing.

    ``concerns`` is the run's authored underlying concerns (``{speaker, position, concern}``), given
    to the analyst as context the transcript does not contain, whether or not the run withheld them
    (owner decision, 2026-10-02). ``concerns_withheld`` says which, and is stored on the payload so
    every surface can label a withheld run's concerns as hidden during it. With no concerns the
    ``concerns`` field is not asked for, and the prompt is exactly what it was before it existed.

    Returns ``{payload, tokens_in, tokens_out, cost_usd, parsed, instructions}``
    where ``payload`` is the structured (or fallback) summary, ``parsed`` is True
    when strict JSON was obtained, and ``instructions`` is the effective
    role-framing text that created it (``None`` when the default was used, so
    callers can persist NULL). On a parse failure it retries ONCE, then falls
    back to a plain-text overview so it never crashes the run/UI.
    """
    fields = fields or list(DEFAULT_SUMMARY_FIELDS)
    if not concerns:
        fields = [f for f in fields if f != "concerns"] or [
            f for f in DEFAULT_SUMMARY_FIELDS if f != "concerns"
        ]
    if "concerns" in fields:
        context = context + concerns_note(concerns or [], withheld=concerns_withheld)
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
        f'The conversation topic was: "{topic}".{context}\n\n'
        f"Transcript:\n{transcript}\n\n"
        "Produce the JSON analysis now."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    def _stamp(payload: Dict[str, Any]) -> Dict[str, Any]:
        # Not model output: whether the concerns were hidden during the run, which decides the label.
        if "concerns" in fields:
            payload["concerns_withheld"] = bool(concerns_withheld)
        return payload

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
                "payload": _stamp(payload),
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
            payload = _coerce_summary(obj, fields)
            if payload.get("omitted"):
                # finish_reason tells a model that left a field out ("stop") from a reply cut off at
                # the budget ("length"); the stored summary cannot.
                logger.warning(
                    "Summary reply did not supply %s (finish_reason=%s, %s tokens out)",
                    ", ".join(payload["omitted"]), result.get("finish_reason"), result["tokens_out"],
                )
            return {
                "payload": _stamp(payload),
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
        "payload": _stamp(payload),
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
