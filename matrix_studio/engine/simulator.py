# SPDX-License-Identifier: Apache-2.0
"""
Core simulation engine - hand-rolled async litellm orchestration.

This engine implements a two-phase turn loop:
1. _select_next_speaker(): LLM decides who speaks next
2. _generate_response(): That agent generates their response

No AutoGen - this is a custom async litellm loop with event sourcing.
"""

import asyncio
import json
import logging
import os
import random
import re
import time
import uuid
from collections import Counter
from typing import Any, Awaitable, Callable, Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

# Deferred: importing litellm costs 1.7 s of the API Lambda's 1.9 s import, which
# pushed its init phase past Lambda's hard 10 s limit. The proxy also applies
# `drop_params` and `suppress_debug_info`, which used to be set below and therefore
# depended on import order. See matrix_studio/lazy_litellm.py.
from matrix_studio.lazy_litellm import litellm
from matrix_studio.models import ModelSet, model_for
from matrix_studio import assumptions as assumptions_mod
from matrix_studio import experts as experts_mod

# Type alias for the Phase 1 live-emit callback. It receives one structured
# event dict (same shape as a persisted row) for each event the engine emits.
OnEvent = Callable[[Dict[str, Any]], Awaitable[None]]

from matrix_studio.avatar import avatar_cost_usd, generate_avatar, store_avatar
from matrix_studio.citations import (
    CitationContext,
    analyse_citations,
    provenance_payload,
)
from matrix_studio.settings import get_settings
from matrix_studio.documents import ingest_file, ingest_text
from matrix_studio.jsonio import extract_json_object
from matrix_studio.retrieval import (
    embed_pending_chunks,
    format_documents_block,
    format_unsupported_block,
    retrieve_for_turn,
    standing_query_text,
)
from matrix_studio.personas import (
    effective_persona,
    parse_structured,
    public_persona,
    structured_payload,
)
from matrix_studio.state import (
    THREAD_TYPES,
    AgentState,
    CognitionConfig,
    MemoryItem,
    PendingThread,
    PersonaConfig,
    RetrievalConfig,
    SelectionConfig,
    SimSnapshot,
)
from matrix_studio.storage import Database
from matrix_studio.validation import validate_utterance

logger = logging.getLogger(__name__)

# `litellm.suppress_debug_info` and `litellm.drop_params` were set here. They now
# live in `matrix_studio/lazy_litellm.py`, applied the moment the module resolves —
# setting them here would have forced the very import this defers, and made them
# conditional on some process having imported the simulator, which `naming.py`,
# `analysis.py` and `pressure.py` do not.


class SpeakerChoice(NamedTuple):
    """Who speaks next, and whether anybody actually chose them.

    ``name`` is **None** only when the moderator declined to nominate anyone — intervention H,
    "the discussion has finished". The turn loop decides what to do about that; this function
    does not, because the guards (has everybody spoken, is this the second decline in a row)
    are about the run rather than about one selection.

    ``fallback`` is ``None`` for a real decision and names the degradation otherwise.
    Before this existed the fallback returned a bare name like any other pick, so a
    provider outage and a moderator's judgement were the same event in the transcript —
    and the turn-share skew measured in `docs/SPEAKER-SELECTION-EVALUATION.md` could not
    be attributed between the model and the shrug.
    """

    name: Optional[str]
    reason: Optional[str]
    fallback: Optional[str] = None
    #: What the selection call cost. It was computed by the provider and thrown away, so a run's
    #: reported cost omitted one model call per moderated turn — measured on brainstorm-opus as
    #: 35 Haiku calls, $0.20, charged to nobody.
    cost_usd: float = 0.0


def _fallback_speaker(
    agent_names: List[str],
    last_speaker: Optional[str],
    why: str,
    reason: Optional[str] = None,
) -> SpeakerChoice:
    """Pick a speaker when selection failed — loudly, and without a cast-position bias.

    Two properties, both deliberate:

    - **Random, not ``candidates[0]``.** The old fallback always returned the same
      person for a given last speaker, so a fallback that fired often enough looked
      exactly like a persona the moderator favoured.
    - **A warning, and a named reason on the event.** A silent degradation that returns
      a plausible answer is the failure mode this project keeps hitting.
    """
    candidates = [n for n in agent_names if n != last_speaker] or list(agent_names)
    pick = random.choice(candidates)
    logger.warning(
        "Speaker selection fell back (%s): chose %s at random from %d candidate(s); "
        "last speaker was %s. No model made this choice.",
        why, pick, len(candidates), last_speaker,
    )
    return SpeakerChoice(pick, reason, why)


#: The closing instruction of the selection prompt, and the anchor the fairness block
#: replaces. Named because three prompts and one test now depend on the exact string.
_NATURALLY = "Choose naturally based on conversation flow."


def _fairness_block(
    agent_names: List[str],
    conversation: List[Dict[str, Any]],
    max_messages: Optional[int],
    ceiling: bool = False,
) -> str:
    """Interventions A and B: the participation counts, then the run's fair share.

    **The wording here is byte-identical to the `counts+budget` arm of
    `scripts/eval_speaker_selection.py`, deliberately, and must stay that way.** That arm
    is what was measured across 146 replays; a reworded copy of it in the engine would mean
    the numbers in `docs/SPEAKER-SELECTION-EVALUATION.md` §10–§11 describe a prompt that no
    longer exists. `--check-baseline` asserts the two are the same text.

    That includes the parts a copy-editor would fix. "last spoke 0 turn(s) ago" for the
    persona who just spoke is odd English and it is what the measured arm says.
    """
    seen = [m.get("speaker") for m in conversation]
    lines = []
    for name in agent_names:
        taken = sum(1 for s in seen if s == name)
        since = next(
            (len(seen) - j - 1 for j in range(len(seen) - 1, -1, -1) if seen[j] == name),
            None,
        )
        ago = "has not spoken yet" if since is None else f"last spoke {since} turn(s) ago"
        lines.append(f"- {name}: {taken} turn(s) so far, {ago}")
    block = "Participation so far:\n" + "\n".join(lines) + "\n\n"
    if max_messages and ceiling:
        # `max_messages` is a SAFETY CEILING, not a plan: the operator turned on
        # `stop_when_converged` and set the number high so the run cannot loop for ever.
        # Rendering intervention B's arithmetic against it would be actively misleading —
        # "a fair share is roughly 17 turns each" invites the moderator to pace for a
        # hundred turns and argues directly against the stop it is being offered.
        #
        # So the share arithmetic is dropped and B's actual directive is kept: the measured
        # win in §11 was "do not let a participant fall far behind", not the number.
        #
        # UNMEASURED. Every figure in §10–§11 used the arithmetic form. This wording has
        # not been through the harness, and it only applies to ceiling runs.
        return (
            block
            + f"This conversation may run for up to {max_messages} turns, but it should end "
            "as soon as the discussion is genuinely finished rather than filling the budget. "
            + _NATURALLY[:-1]
            + ", but do not let a participant fall far behind the others without reason."
        )
    if max_messages:
        # Intervention B. Omitted when the run's length is unknown rather than guessed:
        # a fair share computed from the wrong denominator is worse than no fair share,
        # and B is the half of this that the stronger model actually acts on (§11).
        fair = max_messages / max(1, len(agent_names))
        return (
            block
            + f"This conversation runs for {max_messages} turns with {len(agent_names)} "
            f"participants, so a fair share is roughly {fair:.0f} turns each. "
            + _NATURALLY[:-1]
            + ", but do not let a participant fall far behind their share without reason."
        )
    return block + _NATURALLY


#: Intervention H: relevance before fairness, and permission to say nobody.
#:
#: Measured in `docs/SPEAKER-SELECTION-EVALUATION.md` §13–§14: with turns spread evenly the
#: renewal run finished its argument at turn 25 and then spent fifteen turns on "confirmed,
#: nothing to add" — and the padding was NOT misallocation. The five fairness-motivated picks
#: before turn 26 were all substantive; from 26 every turn was filler whoever was chosen,
#: including the persona with the most turns. So the moderator needs a way to answer "nobody",
#: because at that point there was no better pick to make.
_RELEVANCE = (
    " Prefer a participant who is behind ONLY if they have something specific to add to the "
    "point being discussed right now; being overdue is not on its own a reason to speak. If "
    "the discussion has genuinely finished — every position stated, the disagreements either "
    'resolved or explicitly parked, and nobody has anything substantive left — reply with '
    '{"speaker": null, "reason": "<what is finished>"} instead of naming someone.'
)


async def _select_next_speaker(
    topic: str,
    agents: Dict[str, AgentState],
    conversation: List[Dict[str, Any]],
    last_speaker: Optional[str],
    settings,
    model: Optional[str] = None,
    cognition: Optional[CognitionConfig] = None,
    personas: Optional[PersonaConfig] = None,
    selection: Optional[SelectionConfig] = None,
    max_messages: Optional[int] = None,
) -> SpeakerChoice:
    """
    Use LLM to select the next speaker.

    Args:
        topic: Conversation topic
        agents: Dict of agent states
        conversation: Conversation history
        last_speaker: Last speaker name or None
        settings: Global settings
        model: Effective model override (per-run); falls back to the settings
            default when None.
        cognition: Phase 2c cognition config. When disabled (default) the
            selection prompt/call is byte-for-byte the pre-2c behavior and the
            returned reason is None. When enabled the moderator also returns a
            one-line reason (captured into the speaker.selected event).
        personas: Phase 6 structured-persona config. Only the PUBLIC summary of a
            structured persona reaches this prompt — see below.

    Returns:
        A `SpeakerChoice` — ``(name, reason_or_None, fallback_or_None)``.
    """
    # Checked, rather than left to fail somewhere further down, because the failure this
    # prevents is invisible: calling this with `agents` and `topic` swapped used to raise
    # inside the function, get caught by a broad `except Exception`, and return a fallback
    # speaker — no model call, no error, a plausible-looking pick on every turn for ever.
    if not isinstance(agents, dict):
        raise TypeError(
            "_select_next_speaker(topic, agents, ...): agents must be a dict of "
            f"name -> AgentState, got {type(agents).__name__}"
        )
    if not agents:
        raise ValueError("_select_next_speaker: no agents to choose a speaker from")

    agent_names = list(agents.keys())
    personas_on = bool(personas and personas.enabled)

    # Build selection prompt.
    #
    # Phase 6: this uses `public_persona`, NOT the speaker's own persona text. The
    # moderator prompt is the one place every persona's description appears at
    # once, so rendering the private block here would put each persona's withheld
    # `underlying_concern` one prompt away from the whole cast — destroying the
    # thing it exists for (drawing the concern out is the skill being exercised).
    # What the moderator gets is role + what they optimise for, which is what the
    # room can see anyway and is genuinely useful for choosing who speaks next.
    personas_desc = "\n".join(
        [
            f"- {name}: "
            + public_persona(agents[name].persona, agents[name].structured, enabled=personas_on)
            for name in agent_names
        ]
    )

    recent_conv = conversation[-10:] if len(conversation) > 10 else conversation
    conv_summary = "\n".join(
        [f"{msg['speaker']}: {msg['content']}" for msg in recent_conv]
    )

    cognition_on = bool(cognition and cognition.enabled)

    if cognition_on:
        selection_prompt = f"""You are a conversation moderator. Given the following personas and recent conversation about "{topic}", select who should speak next.

Personas:
{personas_desc}

Recent conversation:
{conv_summary}

Last speaker: {last_speaker or 'None (start of conversation)'}

Respond with ONLY a JSON object of the form {{"speaker": "<persona name>", "reason": "<one short sentence on why they should speak next>"}}. Choose naturally based on conversation flow."""
    else:
        selection_prompt = f"""You are a conversation moderator. Given the following personas and recent conversation about "{topic}", select who should speak next.

Personas:
{personas_desc}

Recent conversation:
{conv_summary}

Last speaker: {last_speaker or 'None (start of conversation)'}

Respond with ONLY the name of the persona who should speak next. Choose naturally based on conversation flow."""

    # Interventions A+B, ON by default. Measured across 146 replays on two models: the
    # prompt above alone gives a Gini of turn share of 0.332 (Haiku) / 0.335 (Sonnet) and
    # leaves somebody with zero turns in 6 of 24 replays; with this block, 0.223 / 0.185 and
    # nobody starved. See `docs/SPEAKER-SELECTION-EVALUATION.md` §10–§11.
    #
    # Note it is appended to BOTH prompts. The measurement used the cognition-on one, since
    # that is what every recorded transcript ran; the closing sentence is the same string in
    # both, and there is no reason a cognition-off run should be the unfair one.
    if selection is None or selection.fairness:
        selection_prompt = selection_prompt.replace(
            _NATURALLY,
            _fairness_block(
                agent_names, conversation, max_messages,
                # A run that may stop early is a run whose budget is a ceiling.
                ceiling=bool(selection and selection.stop_when_converged),
            ),
        )

    # Intervention H rides on the cognition-on prompt only: it asks for `{"speaker": null}`,
    # and the cognition-off prompt asks for a bare name, which has no way to express "nobody".
    may_decline = bool(
        cognition_on and (selection is None or selection.stop_when_converged)
    )
    if may_decline:
        selection_prompt += _RELEVANCE

    messages = [{"role": "user", "content": selection_prompt}]

    def _match(text: str) -> Optional[str]:
        for name in agent_names:
            if name.lower() in text.lower():
                return name
        return None

    # NO `max_tokens`. There used to be one — 120 with cognition on, 50 without — and it
    # was a Haiku-era number that quietly became the binding constraint on this call.
    #
    # Measured 2026-09-15 (`docs/SELECTION-MODEL-DEFAULT.md` §6): a reply that hits the cap
    # comes back with `finish_reason="length"`, `completion_tokens=120` and **empty
    # content** — not a partial object — so there is no name to match and the fallback
    # fires. Haiku averages 75 tokens against that cap and Sonnet 5 averages 80–87, which
    # made the 120 look survivable while it was actually costing Sonnet 43% of its picks in
    # replay. Haiku's own margin was ~45 tokens; one more sentence in this prompt would
    # have pushed it over the same cliff.
    #
    # Removing the cap costs nothing in the normal case — output tokens are billed by use,
    # and the observed replies are 40–90 tokens — and Bedrock accepts an omitted maxTokens
    # on both models (verified). The residual risk is a runaway reply billed to the model's
    # own ceiling, which is why `finish_reason` is now recorded below rather than ignored.
    kwargs: Dict[str, Any] = dict(
        # `speaker_selection`, not the conversation model: temperature 0.3 is
        # deliberate and Sonnet 5 would silently drop it. See models.py.
        model=model_for(model, "speaker_selection") or settings.litellm_model,
        messages=messages,
        temperature=0.3,  # Lower temperature for more consistent selection
    )
    if cognition_on:
        kwargs["response_format"] = {"type": "json_object"}

    # ONLY the provider call is guarded, and deliberately so. Everything above and below
    # is this repo's own code: a TypeError there is a defect, and degrading to a fallback
    # speaker would hide it behind a run that still looks like it worked. A throttle, a
    # bad model id or a credential expiry is a different thing — genuinely external, and
    # the run should keep going while saying loudly that nobody chose.
    try:
        response = await litellm.acompletion(**kwargs)
        raw = (response.choices[0].message.content or "").strip()
        finish = getattr(response.choices[0], "finish_reason", None)
        call_cost = float(
            (getattr(response, "_hidden_params", None) or {}).get("response_cost") or 0.0
        )
    except Exception as e:
        logger.error(f"Speaker selection call failed: {e}", exc_info=True)
        return _fallback_speaker(agent_names, last_speaker, "call_failed")

    reason: Optional[str] = None
    selected = raw
    declined = False
    if cognition_on:
        # Tolerant parse: this model wraps JSON in a markdown fence, which a bare
        # json.loads rejects. See matrix_studio/jsonio.py.
        parsed = extract_json_object(raw)
        if parsed is not None:
            selected = str(parsed.get("speaker", "")).strip() or raw
            r = parsed.get("reason")
            reason = str(r).strip() if r else None
            # Intervention H. An EXPLICIT null is the verdict "nobody has anything left";
            # a MISSING key is a malformed reply, and the two must not be confused — the
            # second would end runs on a parse accident. `"speaker": null` is the contract,
            # so the key has to be present and its value has to be null.
            declined = may_decline and "speaker" in parsed and parsed["speaker"] is None

    if declined:
        logger.info("Moderator declined to nominate: %s", reason)
        return SpeakerChoice(None, reason, None, call_cost)

    # Validate selection
    matched = _match(selected)
    if matched is not None:
        return SpeakerChoice(matched, reason, None, call_cost)

    # The reply named nobody in the cast. The reason (if any) is kept — it says what the
    # moderator was trying to do — but the NAME is ours, and the event will say so.
    #
    # `truncated` and `unresolved` are separate causes because they need opposite fixes:
    # one means the model ran out of room mid-answer (raise the ceiling, or shorten what it
    # is asked to write), the other means it named somebody who is not in the cast (fix the
    # prompt or the resolver). With no `max_tokens` above, `truncated` should now never
    # appear — and that is exactly why it is worth recording if it does.
    why = "truncated" if finish == "length" else "unresolved"
    # The call was made and paid for even though its answer was unusable, so the cost is kept.
    return _fallback_speaker(agent_names, last_speaker, why, reason)._replace(cost_usd=call_cost)


#: A pass declared in prose rather than by setting the field. The backstop exists because
#: the field is the contract and the prose is what a model does when it ignores contracts;
#: when the two disagree the engine logs it, so "the filter ate a real turn" is answerable.
_PROSE_PASS = re.compile(
    r"^\W*(?:i(?:'| a)?m going to |i(?:'ll| will) )?(?:pass|skip)\b"
    r"|^\W*(?:i have |i've got )?nothing (?:to add|further|else)\b"
    r"|^\W*no(?:thing)? comment\b",
    re.IGNORECASE,
)


#: The closing round's instruction. Asks for POSITIONS AND TERMS, not agreement.
#:
#: "Work toward a consensus" was the obvious wording and is the dangerous one. The Phase 6
#: dismissal work measured the same sentence moving a persona's visible behaviour from 0.000
#: to 0.333 depending only on how it was framed, and `distinct_positions` is already the
#: least stable metric in the harness — so an instruction to agree would reliably produce
#: agreement, every run would end resolved, and nothing would distinguish a real resolution
#: from a manufactured one. The last sentence is the guard.
_CLOSING = (
    "\n\nThis is the FINAL round of the conversation. State your position as it now stands, "
    "name specifically what you can accept from what others have proposed, and name what you "
    "cannot accept and why. If your position moved during this conversation, say what moved "
    "it. Do not agree to something you do not agree with in order to close."
)


#: Quote pairs a model wraps a whole reply in: straight, curly, and mixed (it opens curly, closes straight).
_QUOTE_OPEN = "\"\u201c"
_QUOTE_CLOSE = "\"\u201d"


def unwrap_quoted(content: str) -> str:
    """Remove a quote pair that encloses the WHOLE message, and nothing else.

    Sonnet occasionally returns a turn as a quoted string, which renders as a transcription artefact.
    Measured on 2,365 stored messages: 4 were wrapped like that — and 4 more OPEN with a quote that is
    correct, a persona quoting a phrase ('"Continuation, not creation" is a nice phrase but…'). So only
    a wrapper is removed: first and last characters a quote pair, and no quote mark between them, which
    is what tells a wrapper from a message that happens to start or end with a quotation.
    """
    text = (content or "").strip()
    if len(text) < 3 or text[0] not in _QUOTE_OPEN or text[-1] not in _QUOTE_CLOSE:
        return content
    inner = text[1:-1]
    if any(q in inner for q in _QUOTE_OPEN + _QUOTE_CLOSE):
        return content
    return inner.strip()


async def _generate_response(
    speaker_name: str,
    agent: AgentState,
    topic: str,
    conversation: List[Dict[str, Any]],
    settings,
    model: Optional[str] = None,
    # Simultaneous mode only: this persona may decline the turn. Everyone is asked every
    # round, so without this the quiet ones would be forced to invent something — which is
    # the failure the sequential engine spent all of §13 learning to avoid.
    allow_pass: bool = False,
    # The last round, run when a conversation hits its ceiling without finishing. Changes
    # what is asked for, not who is asked.
    closing: bool = False,
    cognition: Optional[CognitionConfig] = None,
    retrieved_memories: Optional[List["MemoryItem"]] = None,
    open_threads: Optional[List["PendingThread"]] = None,
    retrieved_passages: Optional[List[Any]] = None,
    disclose_unsupported: bool = False,
    personas: Optional[PersonaConfig] = None,
    cite_inline: bool = False,
    consultants: str = "",
    assumptions: str = "",
) -> Dict[str, Any]:
    """
    Generate a response from the selected speaker.

    Args:
        speaker_name: Name of speaking agent
        agent: Agent state
        topic: Conversation topic
        conversation: Full conversation history
        settings: Global settings
        model: Effective model override (per-run); falls back to the settings
            default when None.
        cognition: Phase 2c cognition config. When disabled (default) this is
            byte-for-byte the pre-2c plain-text path and the returned dict has
            no rationale/goal_served. When enabled the speaker returns its
            utterance plus a structured first-person rationale + the goal it
            serves (single JSON-mode call); a parse failure degrades gracefully
            to the plain utterance so a bad response never stalls a run.

    Returns:
        Dict with response, tokens, and cost info (plus rationale/goal_served
        when cognition is enabled).
    """
    cognition_on = bool(cognition and cognition.enabled)
    memory_on = bool(cognition_on and cognition.memory)
    goals_dynamic = bool(cognition_on and cognition.goals_dynamic)
    relationships_on = bool(cognition_on and cognition.relationships)
    threads_on = bool(cognition_on and cognition.threads)
    personas_on = bool(personas and personas.enabled)

    # Phase 6: the speaker's own persona text, with its structured identity —
    # background, formative lessons, positions with firmness, the re-tuned
    # dismissal rule — appended. Returns `agent.persona` unchanged when the
    # feature is off or the cast member declared no structured block, so the
    # prompt is byte-identical to pre-Phase-6 in both cases.
    persona_text = effective_persona(
        agent.persona,
        agent.structured,
        enabled=personas_on,
        withhold_concerns=bool(personas.withhold_concerns) if personas else True,
        # NOT bool() — `dismissal_rule` is a named variant ("mandatory" | "retuned"
        # | "blunt" | "off"), and coercing it to a bool collapsed every variant to
        # the default wording. That is silent: an arm would run, produce numbers,
        # and have tested nothing. Caught by
        # test_the_rule_variant_reaches_the_prompt.
        dismissal_rule=personas.dismissal_rule if personas else "mandatory",
        evidence_lean=bool(personas.evidence_lean) if personas else False,
    )

    # Build context for the agent
    recent_conv = conversation[-20:] if len(conversation) > 20 else conversation
    conv_text = "\n".join([f"{msg['speaker']}: {msg['content']}" for msg in recent_conv])

    goals_line = ', '.join(agent.goals) if agent.goals else 'Engage authentically'

    # Phase 2c: surface the retrieved (top-K importance+recency) memories into
    # the prompt. These exact items are the turn's causal memory_refs.
    memory_block = ""
    if memory_on and retrieved_memories:
        lines = "\n".join(f"- {m.content}" for m in retrieved_memories)
        memory_block = f"\n\nWhat you remember so far:\n{lines}"

    # Phase 4b: surface the OPEN pending threads into the prompt so unresolved
    # setups causally influence this turn (the world's memory of its own
    # unfinished business). The exact ids shown here are what the model must
    # cite in thread_updates — the same causal-refs discipline as memory.
    threads_block = ""
    if threads_on and open_threads:
        lines = "\n".join(
            f"- [{t.id}] ({t.thread_type}, opened turn {t.origin_turn}"
            + (f" by {t.origin_agent}" if t.origin_agent else "")
            + f"): {t.description}"
            for t in open_threads
        )
        threads_block = (
            f"\n\nUnresolved threads in this conversation (setups, promises, "
            f"deferred consequences — weave them in or pay them off when natural):\n{lines}"
        )

    # Phase 5: surface the retrieved document passages. Unlike memory/threads
    # this is NOT gated on cognition — attaching background material to a persona
    # is useful with cognition off, so the block is appended in both branches
    # below. The passages shown here are the turn's causal document_refs.
    # Phase 5g: when retrieval ran and returned nothing, optionally ask the
    # persona to flag that it is speaking unsourced. ``disclose_unsupported`` is
    # only ever True when retrieval was actually attempted, so an empty block here
    # cannot be confused with "retrieval is turned off".
    if retrieved_passages:
        documents_block = format_documents_block(retrieved_passages, cite_inline=cite_inline)
    elif disclose_unsupported:
        documents_block = format_unsupported_block()
    else:
        documents_block = ""
    # Consultants the persona may ask (matrix_studio/experts.py). "" when there are none, so a run
    # without them is byte-identical to before.
    documents_block += consultants
    # Working assumptions (matrix_studio/assumptions.py). "" when there are none.
    documents_block += assumptions

    if cognition_on:
        # Compose the JSON schema from the enabled cognition sub-features so the
        # single structured call carries exactly what's turned on.
        fields = [
            '"utterance": "<what you say, in character, 2-4 sentences>"',
            '"rationale": "<one first-person sentence: why you say this now>"',
            '"goal_served": "<which of your goals this advances, verbatim, or \'none\'>"',
        ]
        # The same field set as a JSON schema, which is what actually CONSTRAINS the
        # reply — see the `response_format` comment at the call below for the numbers.
        # Built here, in the same branches that build the prompt text, so a field added
        # to one and not the other is visible in a single screen rather than two.
        properties: Dict[str, Any] = {
            "utterance": {"type": "string"},
            "rationale": {"type": "string"},
            "goal_served": {"type": "string"},
        }
        extra_instr = ""
        if allow_pass:
            # Declared, not detected. A boolean the persona sets is unambiguous; deciding
            # from the prose whether "Nothing here changes my position, but —" is a pass
            # would drop real turns, and that is a silent loss of content.
            fields.append(
                '"pass": <true if you have nothing substantive to add this round, else false>'
            )
            properties["pass"] = {"type": "boolean"}
            extra_instr += (
                "\n\nEveryone in this conversation is asked to respond every round, so it is "
                "entirely normal to have nothing to add. If the discussion has not moved to "
                "anything you have a stake in, or your position is already on the record and "
                "unchanged, set \"pass\": true and leave the utterance empty. Passing is not "
                "a failure and it is not recorded as speech — say something only when you "
                "have something to say."
            )
        if closing:
            extra_instr += _CLOSING
        if memory_on:
            fields.append(
                '"memories": [{"content": "<a short thing you just learned or decided this turn>", '
                '"importance": <0.0-1.0>, "tags": ["<tag>"]}]'
            )
            properties["memories"] = {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "importance": {"type": "number"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                },
            }
            extra_instr += (
                " The memories array holds 0-2 items you genuinely formed this turn "
                "(what you learned/decided); use [] if nothing notable."
            )
        if goals_dynamic:
            fields.append('"goal_update": ["<your full updated goal list>"]')
            properties["goal_update"] = {"type": "array", "items": {"type": "string"}}
            extra_instr += (
                " Set goal_update to your FULL new goal list ONLY if this turn "
                "genuinely changed your goals; otherwise omit it or use null."
            )
        if relationships_on:
            fields.append(
                '"relationship_updates": {"<other participant name>": "<your one-line stance toward them>"}'
            )
            properties["relationship_updates"] = {
                "type": "object", "additionalProperties": {"type": "string"},
            }
            extra_instr += (
                " relationship_updates maps other participants to your updated stance "
                "toward them; use {} if nothing changed."
            )
        if threads_on:
            fields.append(
                '"thread_updates": {"open": [{"description": "<a setup/promise/deferred consequence '
                'you genuinely planted THIS turn>", "thread_type": "setup|promise|faction-action|deferred-consequence"}], '
                '"resolved": ["<id of a listed unresolved thread your utterance genuinely pays off>"], '
                '"abandoned": ["<id of a listed thread that is now genuinely moot>"]}'
            )
            properties["thread_updates"] = {
                "type": "object",
                "properties": {
                    "open": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {"type": "string"},
                                "thread_type": {"type": "string"},
                            },
                        },
                    },
                    "resolved": {"type": "array", "items": {"type": "string"}},
                    "abandoned": {"type": "array", "items": {"type": "string"}},
                },
            }
            extra_instr += (
                " thread_updates records unfinished business: open holds 0-2 threads you truly "
                "planted this turn (use [] if none); resolved/abandoned hold ids ONLY from the "
                "unresolved-threads list above and ONLY if this turn genuinely closes them."
            )
        schema_line = "{" + ", ".join(fields) + "}"
        system_message = f"""{persona_text}

You are participating in a conversation about: {topic}

Your goals: {goals_line}{memory_block}{threads_block}{documents_block}

Respond naturally as this character. Keep responses conversational (2-4 sentences).

Return ONLY a JSON object of the form:
{schema_line}
The rationale must be your genuine reason for this specific turn; do not invent facts.{extra_instr}"""
    else:
        system_message = f"""{persona_text}

You are participating in a conversation about: {topic}

Your goals: {goals_line}{documents_block}

Respond naturally as this character. Keep responses conversational (2-4 sentences)."""

    if conv_text:
        user_content = f"Recent conversation:\n{conv_text}\n\nRespond as {speaker_name}:"
    else:
        # Cold start: no one has spoken yet. Prompt the first speaker to open the
        # conversation rather than react to an empty history (which otherwise makes
        # the model complain there is nothing to respond to).
        user_content = (
            f'You are opening the conversation about "{topic}". No one has spoken yet. '
            f"Start the discussion naturally as {speaker_name} with an opening remark "
            f"that reflects your persona and invites the others in. Do not mention that "
            f"the conversation is empty or that there is nothing to respond to."
        )

    if cognition_on:
        # Repeat the JSON contract as the LAST thing the model reads.
        #
        # `response_format={"type": "json_object"}` is advisory on this Bedrock path, not a
        # constraint. Measured 2026-09-14 against `global.anthropic.claude-sonnet-5` using
        # the engine's own prompt, inside the deployed image, against real Bedrock:
        #
        #   no retrieved passages in the prompt   12/12 replies were JSON
        #   with retrieved passages                4/8  replies were JSON
        #   with retrieved passages + this line    8/8  replies were JSON
        #
        # So it is the RETRIEVED SOURCE MATERIAL that tips it: a few thousand characters of
        # quoted documents, followed by "Respond as <name>:", and the model answers in prose
        # like the transcript it has just read. The schema instruction is already last in the
        # system message and that is not enough — it is thousands of characters upstream by
        # then. On run `3abd39b3` this cost cognition on 20 of 24 turns.
        user_content += (
            "\n\nReturn ONLY the JSON object described in your instructions. Your spoken "
            'words go in the "utterance" field — no prose outside the object.'
        )

    messages = [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_content},
    ]

    try:
        kwargs: Dict[str, Any] = dict(
            # `voice` — the persona speaking, which is the product.
            model=model_for(model, "voice") or settings.litellm_model,
            messages=messages,
            temperature=settings.litellm_temperature,
            max_tokens=settings.litellm_max_tokens,
        )
        if cognition_on:
            # A SCHEMA, not `{"type": "json_object"}`.
            #
            # `json_object` is advisory on this Bedrock path. Measured 2026-09-14 against
            # `global.anthropic.claude-sonnet-5` with this engine's own prompt, 5 samples
            # per arm on the persona that fails hardest (Dr. Morgan, whose retrieved
            # passages tip the model into prose):
            #
            #   system-prompt instruction only          0/5 JSON
            #   + the user-message reminder below       5/5
            #   + this json_schema                      5/5
            #   forced tool use (tools + tool_choice)   5/5
            #   assistant prefill with "{"              rejected by the API, 5/5 errors
            #
            # litellm converts a json_schema into a forced tool for Bedrock, which is a
            # structural constraint rather than an instruction — and it leaves the JSON in
            # `message.content`, so nothing downstream changes. Straight `tools` would move
            # the payload to `tool_calls[0].function.arguments` and break 132 test seams
            # that patch `acompletion` and return content.
            #
            # `required` is only `utterance`: the cognition fields are genuinely optional
            # (a turn may form no memories), and requiring them would invite invention.
            # Not `strict`, and `additionalProperties` is left open, so a model adding a
            # field is tolerated rather than an error.
            #
            # If a provider cannot honour it, `drop_params` removes it and the reminder in
            # the user message is what remains — which measured 5/5 on its own.
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "persona_turn",
                    "schema": {
                        "type": "object",
                        "properties": properties,
                        "required": ["utterance"],
                    },
                },
            }
        response = await litellm.acompletion(**kwargs)

        raw = response.choices[0].message.content.strip()

        content = raw
        rationale: Optional[str] = None
        goal_served: Optional[str] = None
        formed_memories: List[Dict[str, Any]] = []
        goal_update: Optional[List[str]] = None
        relationship_updates: Dict[str, str] = {}
        thread_updates: Dict[str, Any] = {"open": [], "resolved": [], "abandoned": []}
        if cognition_on:
            # Tolerant parse. A bare json.loads here made cognition COMPLETELY INERT
            # against a model that fences its JSON: 30 of 30 turns fell through to the
            # degradation path below, so the transcript carried fenced JSON blobs and
            # the run produced 0 memories, 0 reflections and 0 rationales while costing
            # more than not using cognition at all. See matrix_studio/jsonio.py.
            parsed = extract_json_object(raw)
            if parsed is None:
                # Genuinely unparseable: keep the raw text as the utterance (unchanged
                # pre-existing behaviour) so a bad response never stalls a run.
                content = raw
                # And SAY SO. On run 2d2ac45b cognition was configured on, produced
                # rationale on 4 of 24 turns, and nothing in the event log distinguished
                # "the model returned no rationale" from "we threw the rationale away" —
                # so a whole run looked like cognition was switched off. One boolean on
                # the payload makes the two cases tell themselves apart.
                logger.warning(
                    "Cognition response for %s did not parse (%d chars); its rationale, "
                    "memories and updates are lost for this turn.",
                    speaker_name, len(raw),
                )
            else:
                content = str(parsed.get("utterance", "")).strip() or raw
                rat = parsed.get("rationale")
                rationale = str(rat).strip() if rat else None
                gs = parsed.get("goal_served")
                goal_served = str(gs).strip() if gs else None
                if memory_on:
                    mems = parsed.get("memories")
                    if isinstance(mems, list):
                        for m in mems[:2]:
                            if not isinstance(m, dict):
                                continue
                            c = str(m.get("content", "")).strip()
                            if not c:
                                continue
                            imp = m.get("importance")
                            try:
                                imp = float(imp) if imp is not None else None
                            except (TypeError, ValueError):
                                imp = None
                            tags = m.get("tags")
                            tags = [str(t) for t in tags] if isinstance(tags, list) else []
                            formed_memories.append(
                                {"content": c, "importance": imp, "tags": tags}
                            )
                if goals_dynamic:
                    gu = parsed.get("goal_update")
                    if isinstance(gu, list) and gu:
                        cleaned = [str(g).strip() for g in gu if str(g).strip()]
                        if cleaned:
                            goal_update = cleaned
                if relationships_on:
                    ru = parsed.get("relationship_updates")
                    if isinstance(ru, dict):
                        for other, stance in ru.items():
                            o = str(other).strip()
                            s = str(stance).strip()
                            if o and s:
                                relationship_updates[o] = s
                if threads_on:
                    tu = parsed.get("thread_updates")
                    if isinstance(tu, dict):
                        opened = tu.get("open")
                        if isinstance(opened, list):
                            for t in opened[:2]:
                                if not isinstance(t, dict):
                                    continue
                                desc = str(t.get("description", "")).strip()
                                if not desc:
                                    continue
                                ttype = str(t.get("thread_type", "setup")).strip()
                                if ttype not in THREAD_TYPES:
                                    ttype = "setup"
                                thread_updates["open"].append(
                                    {"description": desc, "thread_type": ttype}
                                )
                        for key in ("resolved", "abandoned"):
                            ids = tu.get(key)
                            if isinstance(ids, list):
                                thread_updates[key] = [
                                    str(i).strip() for i in ids if str(i).strip()
                                ]

        # Extract usage info
        usage = response.usage
        tokens_in = usage.prompt_tokens if usage else 0
        tokens_out = usage.completion_tokens if usage else 0

        # Estimate cost (litellm sometimes provides this)
        cost_usd = 0.0
        if hasattr(response, "_hidden_params") and "response_cost" in response._hidden_params:
            cost_usd = response._hidden_params["response_cost"]

        result: Dict[str, Any] = {
            "content": unwrap_quoted(content),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
        }
        # Additive only when cognition is on, so the cognition-off event/result
        # payloads stay byte-for-byte identical to pre-2c.
        if allow_pass:
            # The field wins; the prose is a backstop for a model that ignored the field.
            declared = bool((parsed or {}).get("pass")) if cognition_on else False
            prose = bool(_PROSE_PASS.search((content or "").strip()[:80]))
            result["passed"] = declared or prose
            if declared != prose:
                logger.info(
                    "%s pass signal: field=%s prose=%s — %r",
                    speaker_name, declared, prose, (content or "")[:90],
                )
        if cognition_on:
            result["rationale"] = rationale
            result["goal_served"] = goal_served
            # Whether the structured reply parsed at all. `rationale=None` is ambiguous
            # on its own — the model may not have sent one, or we may have discarded the
            # whole object — and that ambiguity is what let a run look like cognition was
            # switched off when it was on and being thrown away.
            result["cognition_parsed"] = parsed is not None
        if memory_on:
            result["memories"] = formed_memories
        if goals_dynamic:
            result["goal_update"] = goal_update
        if relationships_on:
            result["relationship_updates"] = relationship_updates
        if threads_on:
            result["thread_updates"] = thread_updates
        return result

    except Exception as e:
        logger.error(f"Error generating response for {speaker_name}: {e}", exc_info=True)
        return {
            "content": f"[Error generating response: {str(e)}]",
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
        }


async def begin_run(
    *,
    run_id: str,
    topic: str,
    cast: List[Dict[str, Any]],
    db: Optional[Database],
    emit: Callable[..., Awaitable[None]],
    next_seq: Callable[[], int],
    generate_avatars_flag: bool,
    personas_cfg: PersonaConfig,
    retrieval: RetrievalConfig,
    # Consultants' definitions (config["experts"]): their pasted documents are ingested here, scoped
    # to the consultant's name, so they exist before the first question is asked.
    experts: Optional[List[Dict[str, Any]]] = None,
    # The operator's working assumptions (config["assumptions"]), recorded once at turn 0.
    assumptions: Optional[List[Any]] = None,
) -> Dict[str, AgentState]:
    """Everything a run does at turn 0, before any turn is generated.

    Extracted from `run_simulation` so that Phase 5's `StartRun` state and the local
    path are the SAME code. They were about to diverge: a slice calls
    `resume_simulation`, which deliberately re-emits none of this, so a Step Functions
    run would otherwise have had its own copy of `sim.started`, the structured-persona
    events, avatar generation and document ingest — four things that must agree
    byte-for-byte with the local path or replay and export stop matching.

    Emits, in this order and at turn 0: `sim.started`, one `persona.structured` per
    agent (only when the feature is on), one `avatar.ready` per avatar, and one
    `document.embedded`. Returns the constructed agents.

    Does NOT write the run row. `run_simulation` still does that for the local/CLI
    path; under Phase 5 `POST /api/runs` writes it synchronously before the execution
    starts, so a client that receives a 201 has something to poll.
    """
    # Initialize agents.
    #
    # Phase 6: a cast member may carry a `structured` block (background,
    # preferences, viewpoints). It is parsed ALWAYS, not only when the feature is
    # enabled, so that a malformed block — a typo'd `firmness`, say — fails loudly
    # at run start instead of being silently ignored until someone turns the flag
    # on and wonders why nothing changed. Whether it reaches a prompt is
    # `personas_cfg.enabled`'s decision, made later in _generate_response.
    agents: Dict[str, AgentState] = {}
    for persona in cast:
        agent = AgentState(
            name=persona["name"],
            persona=persona["persona"],
            goals=persona.get("goals", []),
            structured=parse_structured(persona.get("structured")),
        )
        agents[agent.name] = agent

    # sim.started is emitted at turn 0, seq 0 (Phase 0 parity).
    await emit(
        turn=0,
        seq=next_seq(),
        event_type="sim.started",
        payload={"topic": topic, "agent_count": len(agents)},
    )

    # Phase 6: record which convictions the run was seeded with, once, at turn 0.
    # Emitted only when the feature is actually on, so an event's presence means
    # the structure reached the prompts rather than merely sitting in the cast.
    # `structured_payload` strips `validity` and `underlying_concern` — both are
    # private to the operator by design, and the event log is exported and rendered.
    if personas_cfg.enabled:
        for agent in agents.values():
            payload = structured_payload(agent.structured)
            if payload is None:
                continue
            await emit(
                turn=0,
                seq=next_seq(),
                event_type="persona.structured",
                agent_name=agent.name,
                payload={
                    "agent_name": agent.name,
                    "structured": payload,
                    "withhold_concerns": personas_cfg.withhold_concerns,
                    "dismissal_rule": personas_cfg.dismissal_rule,
                    "evidence_lean": personas_cfg.evidence_lean,
                },
            )

    # Working assumptions, recorded once so the transcript shows them from the start and an export or
    # branch can say what the run was built on. The prompts read them from config, not from here.
    for a in assumptions or []:
        await emit(turn=0, seq=next_seq(), event_type="assumption.made", agent_name=None,
                   payload=a.payload())

    # Generate avatars in parallel. Phase 0 generated them serially before the
    # loop and blocked on all of them; here we still gather() them but emit an
    # `avatar.ready` event as each finishes so a live UI can fill cards in
    # progressively. Avatars remain optional eye-candy — a None result (disabled,
    # no creds, content filter, error) yields a null portrait and never fails
    # the run.
    if generate_avatars_flag:
        logger.info("Generating avatars...")

        async def _make_avatar(agent: AgentState) -> None:
            portrait = await generate_avatar(agent.name, agent.persona)
            # Store the image and carry only its KEY. Inlining the base64 put a
            # megabyte into the append-only log that every replay reads, and into
            # every snapshot via AgentState — measured at 99% of the largest
            # snapshot in a real database.
            agent.portrait_key = store_avatar(portrait)
            # avatar.ready lives outside the turn stream (turn 0); give it its
            # own seq so ordering stays total and replay is deterministic.
            await emit(
                turn=0,
                seq=next_seq(),
                event_type="avatar.ready",
                agent_name=agent.name,
                payload={
                    "agent_name": agent.name, "portrait_key": agent.portrait_key,
                    # Only when an image was actually produced: a disabled, filtered or failed
                    # generation costs nothing and must not be charged.
                    **({"cost_usd": avatar_cost_usd()} if portrait else {}),
                },
            )

        await asyncio.gather(*[_make_avatar(a) for a in agents.values()])

    # Phase 5: ingest documents declared on cast members before the first turn,
    # so a persona's background is available from turn 1. Ingestion is local file
    # I/O only (no LLM, no network) and a failure never fails the run — the
    # persona simply has no background material, which the prompt states honestly.
    if db and retrieval.enabled:
        # Consultants' pasted documents are ingested exactly like a persona's, scoped to the consultant's name.
        await _ingest_cast_documents(
            run_id, list(cast) + [e for e in (experts or []) if isinstance(e, dict)],
            db, emit, next_seq,
        )
        # Phase 5f: embed the freshly ingested chunks when a vector mode is on.
        # Done once here rather than lazily per turn so the per-turn hot path
        # only pays for the query embedding.
        if retrieval.mode in ("vector", "hybrid"):
            stats = await embed_pending_chunks(
                db, run_id, embedding_model=retrieval.embedding_model
            )
            await emit(
                turn=0,
                seq=next_seq(),
                event_type="document.embedded",
                payload=stats,
            )
            if stats.get("error"):
                logger.warning(
                    "Embedding unavailable (%s); retrieval will use lexical search.",
                    stats["error"],
                )
            else:
                logger.info(
                    "Embedded %d chunks with %s ($%.6f)",
                    stats["embedded"], stats["model"], stats["cost_usd"],
                )

    return agents


async def run_simulation(
    request: Dict[str, Any],
    db: Optional[Database] = None,
    run_id: Optional[str] = None,
    on_event: Optional[OnEvent] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    Run a complete simulation from a request dict.

    Args:
        request: Simulation request with topic, cast, and optional config
        db: Optional database for event persistence
        run_id: Optional run ID (generated if not provided)
        on_event: Optional async callback invoked with each structured event as
            it occurs (Phase 1 live-emit seam). It is fired at exactly the same
            points the engine persists via ``db.append_event`` — plus one
            ``avatar.ready`` event per avatar. This is purely additive: it does
            not change persistence, the event schema, the JSON result, or any
            Phase 0 timing/ordering. A failing callback never breaks the run.

    Returns:
        Result dict with run_id, conversation, agents, and metadata

    Request format:
        {
            "topic": "conversation topic",
            "cast": [
                {"name": "Alice", "persona": "...", "goals": ["..."]},
                {"name": "Bob", "persona": "...", "goals": ["..."]}
            ],
            "config": {
                "max_messages": 20,
                "generate_avatars": true
            },
            "name": "optional-codename",
            "description": "optional one-line description"
        }
    """
    settings = get_settings()

    # Parse request
    topic = request["topic"]
    cast = request["cast"]
    config = request.get("config", {})
    max_messages = config.get("max_messages", settings.max_messages)
    generate_avatars_flag = config.get("generate_avatars", settings.enable_avatars)
    cognition = CognitionConfig.from_config(config)
    retrieval = RetrievalConfig.from_config(config)
    personas_cfg = PersonaConfig.from_config(config)
    selection_cfg = SelectionConfig.from_config(config)
    run_name = request.get("name")
    run_description = request.get("description")
    # Who the run belongs to. Absent for a direct engine call (CLI, tests, the
    # measurement scripts), which have no notion of a user; the API always sets it.
    # `create_run`'s own default handles the absent case, and it fails closed —
    # see its docstring for why the read paths do not get that courtesy.
    run_owner_sub = request.get("owner_sub")
    # Ensemble membership, when this run is one member of a fan-out. Pure metadata: the
    # engine reads it only to put it on the row, exactly as it does `owner_sub`, and no
    # turn behaviour depends on it. A member conversation is an ordinary conversation —
    # that is the premise the whole ensemble rests on, since replicates are only
    # comparable if nothing about being a replicate changes how a run behaves.
    run_ensemble_id = request.get("ensemble_id")
    run_ensemble_cell = request.get("ensemble_cell")

    # Generate run ID
    if run_id is None:
        run_id = str(uuid.uuid4())

    logger.info(f"Starting simulation {run_id}: {topic}")

    # Monotonic sequence counter so persisted rows and live events share the
    # same ordering. Kept as a closure so the avatar tasks (which run before the
    # loop's `seq`) and the loop agree on ordering.
    seq_counter = 0

    def _next_seq() -> int:
        nonlocal seq_counter
        s = seq_counter
        seq_counter += 1
        return s

    async def _emit(
        turn: int,
        seq: int,
        event_type: str,
        payload: Dict[str, Any],
        agent_name: Optional[str] = None,
    ) -> None:
        """Persist an event (if a db is present) then push it to the live
        subscriber (if any). Persistence is unchanged from Phase 0; the live
        callback is additive and its failures never break the run."""
        if db:
            await db.append_event(
                run_id=run_id,
                turn=turn,
                seq=seq,
                event_type=event_type,
                agent_name=agent_name,
                payload=payload,
            )
        if on_event is not None:
            event = {
                "run_id": run_id,
                "turn": turn,
                "seq": seq,
                "event_type": event_type,
                "agent_name": agent_name,
                "payload": payload,
            }
            try:
                await on_event(event)
            except Exception as cb_err:  # noqa: BLE001 - live emit must never break a run
                logger.warning("on_event callback failed for %s: %s", event_type, cb_err)

    # Create run in database (before events so the FK/order is sane)
    if db:
        await db.create_run(
            run_id=run_id,
            topic=topic,
            cast=cast,
            name=run_name,
            description=run_description,
            config=config,
            **({"owner_sub": run_owner_sub} if run_owner_sub else {}),
            **({"ensemble_id": run_ensemble_id} if run_ensemble_id else {}),
            **({"ensemble_cell": run_ensemble_cell} if run_ensemble_cell else {}),
        )
        await db.update_run_status(run_id, "running")

    agents = await begin_run(
        run_id=run_id,
        topic=topic,
        cast=cast,
        db=db,
        emit=_emit,
        next_seq=_next_seq,
        generate_avatars_flag=generate_avatars_flag,
        personas_cfg=personas_cfg,
        retrieval=retrieval,
        experts=config.get("experts") or [],
        assumptions=assumptions_mod.from_config(config),
    )

    # Fresh start: no prior turns, no seed conversation.
    # Resolved once and LOGGED once: two roles set a low temperature deliberately and
    # some models discard it, so role/model/temperature is only inspectable if something
    # writes it down. See matrix_studio/models.py.
    _model_set = ModelSet.from_config(config)
    _model_set.log_plan()
    return await _run_turns(
        run_id=run_id,
        topic=topic,
        agents=agents,
        conversation=[],
        last_speaker=None,
        start_turn=0,
        max_messages=max_messages,
        settings=settings,
        db=db,
        emit=_emit,
        next_seq=_next_seq,
        # A ModelSet, not `config["model"]` alone. This is the path the CLI and the local
        # server take, and passing the bare string here would make `config.models`
        # silently do nothing on it while working through the orchestrator — the same
        # config behaving differently depending on how the run was started, which is the
        # worst kind of difference because nothing reports it.
        model=_model_set,
        cognition=cognition,
        retrieval=retrieval,
        personas=personas_cfg,
        selection=selection_cfg,
        should_stop=should_stop,
        experts=experts_mod.from_config(config),
        consult_limit=experts_mod.consult_limit(config),
        assumptions=assumptions_mod.from_config(config),
        dynamic_assumptions=assumptions_mod.dynamic_from_config(config),
    )


async def _ingest_cast_documents(
    run_id: str,
    cast: List[Dict[str, Any]],
    db: Database,
    emit: Callable[..., Awaitable[None]],
    next_seq: Callable[[], int],
) -> int:
    """Ingest cast-declared background documents into the run's index.

    Two sources, because the two callers cannot use the same one:

    - ``"documents": ["./background/spec.pdf", ...]`` — SERVER-readable paths, for
      CLI and example-file workflows.
    - ``"document_texts": [{"title": ..., "text": ...}]`` — inline content, which is
      the only thing a **browser** can supply: it has no access to server paths, and
      the existing upload endpoint only exists after a run has been created, by which
      time turn 1 has already been generated. Cast documents must be indexed before
      the first turn to be usable at all, so they have to arrive with the request.

    Both are extracted, chunked and indexed scoped to that persona.

    Emits ``document.ingested`` per document (or ``document.failed`` with the
    reason) so the operator can see what a persona actually has, and returns the
    number successfully ingested. Never raises.
    """
    ingested = 0
    for member in cast:
        persona_name = member.get("name")
        paths = member.get("documents") or []
        if isinstance(paths, str):
            paths = [paths]

        # Inline documents first: they cost no file I/O and cannot fail on a missing
        # path, so a browser-authored run is never blocked by a bad path elsewhere in
        # the cast.
        for n, entry in enumerate(member.get("document_texts") or [], start=1):
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "")
            if not text.strip():
                continue
            title = str(entry.get("title") or "").strip() or f"pasted-{n}.txt"
            try:
                doc = ingest_text(text, title=title)
                doc_id = await db.add_document(
                    run_id=run_id,
                    title=doc.title,
                    chunks=[c.content for c in doc.chunks],
                    persona_name=persona_name,
                    source_path=None,
                    media_type=doc.media_type,
                    char_count=doc.char_count,
                )
                ingested += 1
                await emit(
                    turn=0,
                    seq=next_seq(),
                    event_type="document.ingested",
                    agent_name=persona_name,
                    payload={
                        "document_id": doc_id,
                        "persona_name": persona_name,
                        "title": doc.title,
                        "media_type": doc.media_type,
                        "char_count": doc.char_count,
                        "chunk_count": len(doc.chunks),
                        "source": "inline",
                    },
                )
                logger.info(
                    "Ingested inline %s for %s (%d chunks, %d chars)",
                    doc.title, persona_name, len(doc.chunks), doc.char_count,
                )
            except Exception as exc:  # noqa: BLE001 - ingestion must never fail a run
                logger.warning("Inline document ingestion failed for %s: %s", title, exc)
                await emit(
                    turn=0,
                    seq=next_seq(),
                    event_type="document.failed",
                    agent_name=persona_name,
                    payload={
                        "persona_name": persona_name,
                        "title": title,
                        "source": "inline",
                        "reason": str(exc),
                    },
                )
        for path in paths:
            try:
                doc = ingest_file(path)
                doc_id = await db.add_document(
                    run_id=run_id,
                    title=doc.title,
                    chunks=[c.content for c in doc.chunks],
                    persona_name=persona_name,
                    source_path=doc.source_path,
                    media_type=doc.media_type,
                    char_count=doc.char_count,
                )
                ingested += 1
                await emit(
                    turn=0,
                    seq=next_seq(),
                    event_type="document.ingested",
                    agent_name=persona_name,
                    payload={
                        "document_id": doc_id,
                        "persona_name": persona_name,
                        "title": doc.title,
                        "media_type": doc.media_type,
                        "char_count": doc.char_count,
                        "chunk_count": len(doc.chunks),
                    },
                )
                logger.info(
                    "Ingested %s for %s (%d chunks, %d chars)",
                    doc.title, persona_name, len(doc.chunks), doc.char_count,
                )
            except Exception as exc:
                logger.warning("Document ingestion failed for %s: %s", path, exc)
                await emit(
                    turn=0,
                    seq=next_seq(),
                    event_type="document.failed",
                    agent_name=persona_name,
                    payload={
                        "persona_name": persona_name,
                        "path": str(path),
                        "error": str(exc),
                    },
                )
    return ingested


def _retrieve_memories(agent: AgentState, k: int) -> List[MemoryItem]:
    """Top-K memory retrieval by importance + recency (Phase 2c v1, no embeddings).

    Ranks the agent's ``memory_stream`` by a blend of importance (default 0.5
    when unscored) and recency (later timestamp ranks higher), returning at most
    ``k`` items. Pure, deterministic, and fully inspectable — the returned items
    are exactly the turn's causal ``memory_refs``. ``k <= 0`` or an empty stream
    returns [].
    """
    if k <= 0 or not agent.memory_stream:
        return []
    ranked = sorted(
        agent.memory_stream,
        key=lambda m: (
            m.importance if m.importance is not None else 0.5,
            m.timestamp,
        ),
        reverse=True,
    )
    return ranked[:k]


async def _reflect(
    agent: AgentState,
    topic: str,
    settings,
    model: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Phase 2c reflection: condense the agent's recent memories into ONE
    higher-level belief (first person, one sentence). Returns a dict with the
    belief ``content`` + token/cost usage, or None if there is nothing to
    reflect on / the call fails (reflection must never break a run).
    """
    recent = agent.memory_stream[-8:]
    if not recent:
        return None
    mem_text = "\n".join(f"- {m.content}" for m in recent)
    prompt = (
        f"You are {agent.name}. Based ONLY on these recent memories, state ONE "
        f"higher-level belief or conclusion you now hold about the discussion on "
        f'"{topic}". One sentence, first person. Do not invent facts beyond the memories.\n\n'
        f"Memories:\n{mem_text}\n\nYour belief:"
    )
    try:
        response = await litellm.acompletion(
            model=model_for(model, "reflection") or settings.litellm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=settings.litellm_temperature,
            max_tokens=120,
        )
        content = response.choices[0].message.content.strip()
        usage = response.usage
        tokens_in = usage.prompt_tokens if usage else 0
        tokens_out = usage.completion_tokens if usage else 0
        cost_usd = 0.0
        if hasattr(response, "_hidden_params") and "response_cost" in response._hidden_params:
            cost_usd = response._hidden_params["response_cost"]
        return {
            "content": content,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
        }
    except Exception as e:  # noqa: BLE001 - reflection must never break a run
        logger.error(f"Error during reflection for {agent.name}: {e}", exc_info=True)
        return None


async def _consult(
    expert: Any, question: str, asked_by: str, turn: int, topic: str,
    conversation: List[Dict[str, Any]], *, run_id: str, db: Any, retrieval: Any, model: Any,
    settings: Any, emit: Callable[..., Awaitable[None]], next_seq: Callable[[], int],
    ledger: List[list], names: List[str],
) -> None:
    """Answer one consultation and put the answer on the record (matrix_studio/experts.py).

    Retrieval runs over the CONSULTANT's own sources — resolved by name, like a persona's — with the
    question as the query. The answer is recorded as `expert.answered` and appended to the conversation
    as the consultant's message, so later speakers read it and may credit it. Never raises.
    """
    passages: List[Any] = []
    query = question
    try:
        passages, query, _floor, _failures = await retrieve_for_turn(
            db, run_id, expert.name, "", [{"speaker": asked_by, "content": question}],
            k=retrieval.k, max_chars=retrieval.max_chars, recent_turns=1,
            term_limit=retrieval.term_limit, max_df_ratio=retrieval.max_df_ratio,
            score_ratio=retrieval.score_ratio, mode=retrieval.mode,
            embedding_model=retrieval.embedding_model, rrf_k=retrieval.rrf_k,
            min_similarity=retrieval.min_similarity,
        )
    except Exception as exc:  # noqa: BLE001 — a failed lookup is answered as "not in my sources"
        logger.warning("Consultant %s retrieval failed on %s: %s", expert.name, run_id, exc)
    if passages:
        await emit(
            turn=turn, seq=next_seq(), event_type="document.retrieved", agent_name=expert.name,
            payload={
                "speaker": expert.name, "query": query, "consultant": True,
                "passages": [
                    {"chunk_id": p.chunk_id, "document_id": p.document_id, "title": p.title,
                     "ordinal": p.ordinal, "score": round(p.score, 4), "chars": len(p.content),
                     **({"authority": p.authority} if p.authority else {}),
                     **({"origin": p.origin} if p.origin else {})}
                    for p in passages
                ],
                "total_chars": sum(len(p.content) for p in passages),
            },
        )
    result = await experts_mod.answer(
        expert, question, asked_by, topic, passages,
        model=model_for(model, "voice") or settings.litellm_model, settings=settings,
    )
    cites = analyse_citations(result["answer"], expert.name, names, CitationContext.build(own_passages=passages))
    for c in cites:
        if c.kind == "firsthand":
            ledger.append([expert.name, c.title])
    await emit(
        turn=turn, seq=next_seq(), event_type="expert.answered", agent_name=expert.name,
        payload={
            "expert": expert.name, "speaker": expert.speaker, "asked_by": asked_by,
            "question": question, "answer": result["answer"],
            "document_refs": [p.chunk_id for p in passages],
            **({"citation_provenance": provenance_payload(cites)} if cites else {}),
            "tokens_in": result["tokens_in"], "tokens_out": result["tokens_out"],
            "cost_usd": result["cost_usd"],
            **({"error": result["error"]} if result["error"] else {}),
        },
    )
    conversation.append({
        "speaker": expert.speaker, "content": result["answer"], "turn": turn,
        "consultant": True, "expert": expert.name, "asked_by": asked_by, "question": question,
    })


async def _propose_assumption(
    topic: str, conversation: List[Dict[str, Any]], ledger: List[Any], completed_turns: int, *,
    model: Any, settings: Any, emit: Callable[..., Awaitable[None]], next_seq: Callable[[], int],
    consultants: Sequence[Any] = (),
) -> Optional[Any]:
    """Ask the moderator whether a gap is blocking the room, and record an assumption if so.

    Every check is recorded (`assumption.checked`, with its cost) whatever it decided, so the spend is
    counted and "the moderator looked and found no gap" is distinguishable from "it never looked".
    Never raises: a failed check is a turn without a new assumption.
    """
    parsed: Optional[Dict[str, Any]] = None
    cost = 0.0
    error: Optional[str] = None
    try:
        response = await litellm.acompletion(
            model=model_for(model, "speaker_selection") or settings.litellm_model,
            messages=assumptions_mod.propose_messages(topic, conversation, ledger, consultants=consultants),
            temperature=0.2,
            response_format={"type": "json_object"},
            drop_params=True,
        )
        parsed = extract_json_object((response.choices[0].message.content or "").strip())
        cost = float((getattr(response, "_hidden_params", None) or {}).get("response_cost") or 0.0)
    except Exception as exc:  # noqa: BLE001 — a failed check must not end the run
        logger.warning("Assumption check after turn %d failed: %s", completed_turns, exc)
        error = str(exc)[:300]
    proposal, why_not = assumptions_mod.parse_proposal(
        parsed, conversation[-assumptions_mod.RECENT_MESSAGES:], ledger,
    )
    kind: Optional[str] = None
    if proposal is not None:
        # The second call (`assumptions.KINDS`): only a FACT is recorded. A failed or unreadable
        # classification is not a FACT, so it records nothing — the safe direction for a feature whose
        # measured failure was assuming part of the answer.
        try:
            response = await litellm.acompletion(
                model=model_for(model, "speaker_selection") or settings.litellm_model,
                messages=assumptions_mod.classify_messages(topic, proposal["statement"]),
                temperature=0.0,
                response_format={"type": "json_object"},
                drop_params=True,
            )
            kind, why = assumptions_mod.parse_kind(
                extract_json_object((response.choices[0].message.content or "").strip())
            )
            cost += float((getattr(response, "_hidden_params", None) or {}).get("response_cost") or 0.0)
        except Exception as exc:  # noqa: BLE001
            kind, why = "UNKNOWN", f"classification failed: {type(exc).__name__}"
        if kind != "FACT":
            why_not = f"classified {kind}: {why}"
            proposal = None
    raw = (parsed or {}).get("assumption") if isinstance(parsed, dict) else None
    await emit(
        turn=completed_turns, seq=next_seq(), event_type="assumption.checked", agent_name=None,
        payload={"after_turn": completed_turns, "proposed": proposal is not None,
                 "gap": (proposal or {}).get("gap", ""), "cost_usd": cost,
                 # A proposal the engine discarded is recorded with its reason and what it said, so the
                 # trigger rule's false positives are countable rather than invisible.
                 **({"rejected": why_not, "proposal": raw} if proposal is None and isinstance(raw, dict) else {}),
                 **({"kind": kind} if kind else {}),
                 **({"error": error} if error else {})},
    )
    if proposal is None:
        return None
    made = assumptions_mod.Assumption(
        assumptions_mod.next_id(ledger), proposal["statement"], proposal["basis"],
        assumptions_mod.MODERATOR, completed_turns,
    )
    await emit(turn=completed_turns, seq=next_seq(), event_type="assumption.made", agent_name=None,
               payload={**made.payload(), "gap": proposal["gap"], "asks": proposal["asks"]})
    return made


async def rebuild_read_before(db: Any, run_id: str, up_to_turn: int) -> Dict[str, Set[Tuple[str, int]]]:
    """Each speaker's retrieved ``(title, ordinal)`` pairs up to and including ``up_to_turn``, from the
    run's own ``document.retrieved`` events. A read failure returns {}, which is the old "this turn
    only" behaviour — a missed recall, never a wrongly accepted citation."""
    out: Dict[str, Set[Tuple[str, int]]] = {}
    try:
        rows = await db.get_events(run_id, to_turn=up_to_turn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not rebuild earlier retrievals for %s: %s", run_id, exc)
        return out
    for row in rows:
        if row.get("event_type") != "document.retrieved":
            continue
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                continue
        who = (payload or {}).get("speaker") or row.get("agent_name")
        for p in (payload or {}).get("passages") or []:
            if who and p.get("title") is not None and p.get("ordinal") is not None:
                out.setdefault(who, set()).add((str(p["title"]), int(p["ordinal"])))
    return out


async def _run_turns(
    *,
    run_id: str,
    topic: str,
    agents: Dict[str, AgentState],
    conversation: List[Dict[str, Any]],
    last_speaker: Optional[str],
    start_turn: int,
    max_messages: int,
    settings,
    db: Optional[Database],
    emit: Callable[..., Awaitable[None]],
    next_seq: Callable[[], int],
    model: Optional[str] = None,
    cognition: Optional[CognitionConfig] = None,
    pending_threads: Optional[List[PendingThread]] = None,
    retrieval: Optional[RetrievalConfig] = None,
    personas: Optional[PersonaConfig] = None,
    selection: Optional[SelectionConfig] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    firsthand_citations: Optional[List[List[str]]] = None,
    turn_budget: Optional[int] = None,
    # Consultants (matrix_studio/experts.py) and the run's consultation cap. Empty = none.
    experts: Optional[List[Any]] = None,
    consult_limit: int = 0,
    # Working assumptions every persona reasons from (matrix_studio/assumptions.py). Empty = none.
    assumptions: Optional[List[Any]] = None,
    # Whether, and how often, the moderator may add one when a gap blocks the room. None = never.
    dynamic_assumptions: Optional[Any] = None,
    # Intervention H's streak, carried in because ONE TURN PER CALL is what ships: a
    # counter local to this function is reset on every turn under Step Functions, which
    # made the two-declines-in-a-row guard unsatisfiable in production while passing every
    # in-process test. See `SimSnapshot.decline_streak`.
    decline_streak: int = 0,
) -> Dict[str, Any]:
    """
    Shared turn loop + completion/failure handling for both a fresh run and a
    resumed branch. Generates turns ``start_turn + 1 .. max_messages``.

    ``should_stop`` is polled AFTER each turn is emitted and checkpointed, so an
    operator's stop lets the turn in flight finish and be persisted, and only
    prevents the NEXT one. Cancelling mid-call would throw away tokens that have
    already been paid for and leave a partial turn to trim on resume; waiting one
    turn costs at most one turn and keeps the log clean and resumable.

    Phase 4b: ``pending_threads`` is the global setups-&-payoffs ledger ([] for
    a fresh run; the replayed ledger for a branch/resume). Open threads are fed
    into each turn's generation prompt (causally real), updated from the
    speaker's structured ``thread_updates``, emitted as ``thread.opened`` /
    ``thread.resolved`` / ``thread.abandoned`` events, and carried on every
    snapshot.

    ``start_turn`` is the number of turns already present (0 for a fresh run;
    the fork's ``from_turn`` for a resumed branch, whose earlier turns were
    replayed/copied by the branch service). This keeps the fresh-start Phase 0
    path behaviorally identical — it simply calls this with ``start_turn=0`` and
    an empty seed conversation.

    Phase 2a additive behavior: after each turn the engine persists a FULL
    ``SimSnapshot`` (``status="running"``) and emits an additive
    ``checkpoint.saved`` event so any turn's exact state can be reconstructed.

    Storage note (Phase 2a, CC-approved default): we persist a full snapshot per
    turn rather than deltas. Runs are short (≤ a few dozen turns), so the storage
    cost is small and reconstruction is O(1) (load one row) instead of replaying
    the event log. Delta-encoding is deferred; revisit only if long runs make
    storage a problem.
    """
    turn = start_turn
    if pending_threads is None:
        pending_threads = []
    # Phase 5i: ``(speaker, document_title)`` for every FIRST-HAND citation made so
    # far. A second-hand attribution ("Priya cited X as saying Y") is only accepted
    # when the credited participant appears here, which is what makes crediting
    # verifiable rather than merely plausible.
    # Seeded from the caller, NOT always empty. This was `= []` unconditionally, which
    # meant a resumed or branched run forgot who had cited what first-hand and treated
    # every legitimate second-hand credit as unverifiable. See
    # `SimSnapshot.firsthand_citations` for why that matters more under Phase 5.
    ledger: List[list] = [list(pair) for pair in (firsthand_citations or [])]
    # What each speaker has retrieved so far, as (title, ordinal): what they have READ, so recalling
    # it later is first-hand (`CitationContext.earlier`). Rebuilt from the run's own event log rather
    # than carried in the snapshot — one read per call, and correct for a resumed run and for a branch,
    # whose parent's events are copied up to the fork. Filled only when retrieval can run.
    read_before: Dict[str, Set[Tuple[str, int]]] = (
        await rebuild_read_before(db, run_id, start_turn)
        if retrieval and retrieval.enabled and db is not None else {}
    )

    # How many turns THIS call may generate, as distinct from `max_messages`, which is
    # the run's total budget. `None` means "as many as the run's budget allows", which
    # is every existing caller and the unchanged local/CLI behaviour.
    #
    # This is what lets Step Functions own the loop without the loop being rewritten:
    # a turn Lambda passes 1, gets one turn, and receives a NON-terminal status so the
    # state machine decides what happens next. Extracting a single iteration into a
    # standalone function was the alternative, and it means moving 680 lines of closure
    # state — mechanical, large, and exactly the kind of change that silently drops one
    # of the seven things a turn carries.
    generated = 0

    # Intervention H state. `converged` is set when the moderator has declined to nominate
    # anybody and both guards allow it; `declines` counts CONSECUTIVE declines, so a single
    # odd judgement costs a turn rather than a run.
    converged: Optional[Dict[str, Any]] = None
    declines = decline_streak
    # The working-assumption ledger for this call: what it was handed, plus any the moderator adds.
    ledger_assumptions: List[Any] = list(assumptions or [])

    # Simultaneous mode. A ROUND is one turn: every persona is asked, against the state as
    # it stood when the round opened, and the ones who pass never reach the transcript.
    #
    # Implemented by driving the EXISTING per-turn body once per persona rather than by
    # writing a second one. The body is ~350 lines of retrieval, validation, cognition,
    # citations and cost accounting, and a parallel copy of it would drift — the sequential
    # path is also the one every measurement in the design doc describes, so it is left
    # byte-for-byte alone.
    method = selection.method if selection else "moderated"
    # Rounds are used by three of the four methods, and the two dimensions are independent:
    # WHETHER everyone speaks this turn, and WHETHER they can see each other while doing it.
    #
    #   rotation       rounds, sighted   — cumulative, equal turn share by construction
    #   simultaneous   rounds, blind     — concurrent, measurably more parallel
    #   hybrid         rounds, blind, for the opening N; then moderated
    #   moderated      no rounds
    #
    # The closing round is always a round and always blind, in every method.
    always_rounds = method in ("rotation", "simultaneous")
    hybrid_opening = (
        selection.hybrid_opening_rounds if selection and method == "hybrid" else 0
    )
    # Only `simultaneous` is blind for the WHOLE run. Hybrid's blindness comes from
    # `in_opening` and the closing round's from `in_closing`, so listing either here
    # would be dead logic — a mutant that dropped "hybrid" from this set changed nothing.
    blind_method = method == "simultaneous"
    # The closing round. A run that hits its ceiling stops mid-argument — the simultaneous
    # renewal run ended with four personas all answering the same question, because the
    # budget ran out rather than because anything finished. One final round, asked of
    # everybody at once, turns an arbitrary cutoff into an ending.
    #
    # It runs AFTER the ceiling (turn `max_messages + 1`), not inside it: taking a normal
    # round for it would leave the cutoff exactly as arbitrary as before. And only when the
    # run reached the ceiling — a converged run has already had every persona say it had
    # nothing to add, and asking again would contradict that.
    closing_enabled = bool(selection and selection.closing_round)
    in_closing = False
    # Derived, not carried: a closing round is the only thing that can push `turn` past the
    # ceiling, so `turn > max_messages` means it has already run. That matters because the
    # deployed turn loop runs ONE turn per Lambda invocation, and a flag in this function
    # would be re-initialised on every slice — the same defect that made the decline streak
    # inert in production, found again the same way (a live run that did nothing).
    closing_done = turn > max_messages
    closing_empty = False
    round_queue: List[str] = []
    #: Whether the round currently in progress is blind. Held for the round rather than
    #: recomputed per speaker, so a phase change cannot take effect halfway through one.
    round_blind = False
    #: What the personas in this round can see. Frozen at the top of the round, which is
    #: the whole semantic difference: nobody in a round reads anybody else in it.
    round_state: List[Dict[str, Any]] = []
    round_spoke: List[str] = []
    round_passed: List[str] = []

    try:
        # `round_queue or` is what lets a round span iterations: in simultaneous mode the
        # turn number does not advance between the personas of a round, so re-checking the
        # turn cap mid-round would end the run after its FIRST speaker on the last round.
        # `generated` only ticks when a round closes, so the per-call budget is unaffected.
        while (
            round_queue
            or turn < max_messages
            # `converged is None` is belt-and-braces: a convergence `break`s out of this
            # loop, so the condition is never re-evaluated after one and a mutant removing
            # this changes nothing measurable. It stays as a statement of intent for whoever
            # replaces that `break` — at which point it becomes the thing doing the work.
            or (closing_enabled and not closing_done and converged is None)
        ) and (turn_budget is None or generated < turn_budget):
            # The closing round opens only once the conversation proper is over, so it is
            # decided here rather than by the loop condition — which has to keep letting a
            # round in progress finish.
            if (
                not round_queue
                and turn >= max_messages
                and closing_enabled
                and not closing_done
            ):
                in_closing = True
            # Rounds are used by the simultaneous METHOD and by the closing round in either
            # method: closing statements are naturally concurrent, and it means one
            # implementation rather than a sequential variant nobody measured.
            # A round IN PROGRESS is a round, whatever the phase now says. Deciding this
            # from `turn` alone was a bug worth remembering: `turn` is incremented when a
            # round opens, so on the second opening round of a hybrid run `turn` already
            # equalled `hybrid_opening`, the phase flipped to moderated after the FIRST
            # speaker, and `[Ben, Cy]` were abandoned in the queue — which then kept the
            # loop condition true and ran the conversation to turn 20 of a 4-turn budget.
            in_opening = bool(hybrid_opening and turn < hybrid_opening)
            starting_round = not round_queue and (always_rounds or in_opening or in_closing)
            rounds_now = bool(round_queue) or starting_round
            if starting_round:
                # Blindness belongs to the ROUND, so it is decided once here and held for
                # the round's duration: hybrid's opening rounds are blind and its moderated
                # turns are not, and the closing round is blind in every method — a final
                # statement written after reading the others' final statements is a reply,
                # not a closing statement.
                round_blind = (always_rounds and blind_method) or in_opening or in_closing
                # Cast order is arbitrary and that is fine — in a blind round the order
                # affects only which survivor is written first.
                round_queue = list(agents.keys())
                round_state = list(conversation)
                round_spoke, round_passed = [], []
                turn += 1
            elif not rounds_now:
                turn += 1
                generated += 1
            blind = rounds_now and round_blind

            # Working assumptions made by the moderator (matrix_studio/assumptions.py), checked at the
            # start of a moderated turn or a round, never inside one and never in the closing round.
            # Before selection, so the turn it enables already reasons from it.
            if (
                dynamic_assumptions is not None
                and (starting_round or not rounds_now)
                and not in_closing
                and assumptions_mod.due(dynamic_assumptions, turn - 1, ledger_assumptions)
            ):
                made = await _propose_assumption(
                    topic, conversation, ledger_assumptions, turn - 1,
                    model=model, settings=settings, emit=emit, next_seq=next_seq,
                    # Only while consultations remain: a room that can no longer ask should assume.
                    consultants=(experts or []) if (
                        experts and consult_limit - experts_mod.consults_used(conversation) > 0
                    ) else [],
                )
                if made is not None:
                    ledger_assumptions.append(made)

            # Phase 1: Select next speaker — unless nobody selects. In simultaneous mode
            # the queue IS the answer, so the selection call is skipped entirely: no
            # moderator, no fairness prompt, no decline, and none of §10–§17 applies.
            if rounds_now:
                choice = SpeakerChoice(round_queue.pop(0), None, None)
            else:
                choice = await _select_next_speaker(
                    topic, agents, conversation, last_speaker, settings,
                    model=model, cognition=cognition, personas=personas,
                    selection=selection,
                    # The run's TOTAL budget, not this call's turn_budget: intervention B
                    # is about pacing across the whole conversation, and a Step Functions
                    # turn Lambda that passed its own budget of 1 would tell the moderator
                    # every persona's fair share was one turn.
                    max_messages=max_messages,
                )

            # Intervention H: the moderator said nobody has anything left to add.
            #
            # Two deterministic guards before that is allowed to end a run, because the
            # asymmetry is severe: fifteen turns of "confirmed, nothing to add" cost about
            # $0.30, and fifteen turns of argument cut short cost the whole run.
            if choice.name is None:
                declines += 1
                spoken = {m.get("speaker") for m in conversation}
                everyone_spoke = all(n in spoken for n in agents)
                honoured = everyone_spoke and declines >= 2
                await emit(
                    turn=turn,
                    seq=next_seq(),
                    event_type="speaker.declined",
                    payload={
                        "reason": choice.reason,
                        "consecutive": declines,
                        "everyone_spoke": everyone_spoke,
                        "honoured": honoured,
                    },
                )
                if honoured:
                    # This turn produced no message, so it is not a turn.
                    turn -= 1
                    generated -= 1
                    converged = {"reason": choice.reason, "at_turn": turn}
                    logger.info(
                        "Simulation %s converged at turn %d of %d: %s",
                        run_id, turn, max_messages, choice.reason,
                    )
                    break
                # Overridden. The reason the guard exists is that somebody has not been
                # heard from, so the override calls on the least-heard persona rather than
                # drawing at random — and marks the turn, because the moderator did not
                # choose this speaker.
                counted = Counter(m.get("speaker") for m in conversation)
                pool = [n for n in agents if n != last_speaker] or list(agents)
                pick = min(pool, key=lambda n: (counted.get(n, 0), n))
                logger.warning(
                    "Moderator declined at turn %d but %s; calling on %s instead",
                    turn,
                    "not everybody has spoken" if not everyone_spoke
                    else "this is only the first decline",
                    pick,
                )
                choice = SpeakerChoice(pick, choice.reason, "declined_override", choice.cost_usd)
            else:
                declines = 0

            speaker_name, selection_reason = choice.name, choice.reason

            # speaker.selected payload is additive-only: the reason key appears
            # only when cognition produced one, so cognition-off runs stay
            # byte-for-byte identical to pre-2c.
            speaker_payload: Dict[str, Any] = {
                "speaker": speaker_name,
                "candidates": list(agents.keys()),
            }
            # Additive, and only in the mode where it is true. Without it a reader of the
            # log would see `speaker.selected` on every turn of a run in which nothing
            # selected anybody.
            if rounds_now:
                speaker_payload["method"] = method
                speaker_payload["round"] = turn
            if in_closing:
                speaker_payload["closing"] = True
            if selection_reason:
                speaker_payload["reason"] = selection_reason
            # Present ONLY when nobody chose. Reading a transcript, `selection_fallback`
            # is the difference between "the moderator picked them" and "the call failed
            # and we drew a name" — which the turn shares cannot otherwise distinguish.
            if choice.fallback:
                speaker_payload["selection_fallback"] = choice.fallback
            # Every model call's cost lands on the event it produced, so a run's cost is the sum
            # of its events and the display and the monthly cap cannot disagree. Omitted at zero
            # (a round-robin or rounds turn makes no call), so those payloads are unchanged.
            if choice.cost_usd:
                speaker_payload["cost_usd"] = choice.cost_usd
            await emit(
                turn=turn,
                seq=next_seq(),
                event_type="speaker.selected",
                agent_name=speaker_name,
                payload=speaker_payload,
            )

            # Phase 2: Generate response
            speaker = agents[speaker_name]
            # Phase 2c: retrieve this speaker's top-K memories (importance +
            # recency). The retrieved items ARE the turn's causal memory_refs.
            cognition_on = bool(cognition and cognition.enabled)
            memory_on = bool(cognition_on and cognition.memory)
            goals_dynamic = bool(cognition_on and cognition.goals_dynamic)
            relationships_on = bool(cognition_on and cognition.relationships)
            threads_on = bool(cognition_on and cognition.threads)
            reflect_every = cognition.reflection_every if cognition_on else 0
            retrieved = (
                _retrieve_memories(speaker, cognition.retrieval_k)
                if memory_on else []
            )
            # Phase 4b: the OPEN threads fed into this turn's prompt are the
            # turn's causal thread context (mirrors memory_refs).
            open_threads = (
                [t for t in pending_threads if t.status == "open"]
                if threads_on else []
            )
            # Phase 5i: a second-hand citation is only legitimate if the credited
            # participant genuinely cited that document first-hand earlier. That
            # ledger is accumulated here as the run proceeds.
            # Phase 5: retrieve this speaker's supporting document passages,
            # scoped to its own slice. Independent of cognition, and skipped
            # entirely (zero queries, zero prompt change) when disabled.
            retrieval_on = bool(retrieval and retrieval.enabled and db is not None)
            passages: List[Any] = []
            assumptions_prompt = assumptions_mod.assumptions_block(ledger_assumptions)
            # Consultants need retrieval (they answer from their sources) and the run's remaining cap.
            consult_remaining = (consult_limit - experts_mod.consults_used(conversation)) if experts else 0
            consult_prompt = (
                experts_mod.consultants_block(experts, consult_remaining)
                if (experts and retrieval_on) else ""
            )
            if retrieval_on:
                passages, doc_query, floor_rejected, kb_failures = await retrieve_for_turn(
                    db, run_id, speaker_name, topic, conversation,
                    k=retrieval.k, max_chars=retrieval.max_chars,
                    recent_turns=retrieval.recent_turns,
                    term_limit=retrieval.term_limit,
                    max_df_ratio=retrieval.max_df_ratio,
                    score_ratio=retrieval.score_ratio,
                    mode=retrieval.mode,
                    embedding_model=retrieval.embedding_model,
                    rrf_k=retrieval.rrf_k,
                    min_similarity=retrieval.min_similarity,
                    # PERSONA-RESEARCH.md §3. Passed from the config rather than defaulted
                    # in the engine: a floor the run asked for and the engine did not pass
                    # would be a setting that looks enabled and does nothing, which is how
                    # three earlier features in this project shipped inert.
                    authority_floor=retrieval.authority_floor,
                    # §9.5. Built here because the engine holds the speaker's structured block;
                    # "" when the flag is off, which is byte-identical to before.
                    standing_text=(
                        standing_query_text(agents[speaker_name].structured)
                        if retrieval.standing_query else ""
                    ),
                )
                if passages:
                    await emit(
                        turn=turn,
                        seq=next_seq(),
                        event_type="document.retrieved",
                        agent_name=speaker_name,
                        payload={
                            "speaker": speaker_name,
                            "query": doc_query,
                            "passages": [
                                {
                                    "chunk_id": p.chunk_id,
                                    "document_id": p.document_id,
                                    "title": p.title,
                                    "ordinal": p.ordinal,
                                    "score": round(p.score, 4),
                                    "chars": len(p.content),
                                    # What KIND of source, and whether a human chose it.
                                    # Omitted when absent so a run with no research produces
                                    # a byte-identical payload to one recorded before these
                                    # existed.
                                    **({"authority": p.authority} if p.authority else {}),
                                    **({"origin": p.origin} if p.origin else {}),
                                }
                                for p in passages
                            ],
                            "total_chars": sum(len(p.content) for p in passages),
                            # PERSONA-RESEARCH.md §5.1. The per-collection floor reserves a
                            # slot for a COLLECTION, not for a kind of thing in one — so once
                            # research writes into a curated collection, the operator's own
                            # document competes with the searcher's finds and can be pushed
                            # out entirely. Measured on run 602ddffe: a persona's hand-picked
                            # source material lost all three slots to researched passages.
                            #
                            # Recorded per turn rather than left for a reader to count,
                            # because the whole problem is that it is invisible. Emitted only
                            # when research contributed, so nothing changes for runs without.
                            **({"researched_passages": sum(
                                1 for p in passages if p.is_researched)}
                               if any(p.is_researched for p in passages) else {}),
                            **({"floor_rejected": floor_rejected} if floor_rejected else {}),
                            # Phase 6: a knowledge base whose index could not be queried.
                            # In the event log rather than only the Lambda's logs, because
                            # the consequence is visible in the transcript: the merged
                            # top-k came from a smaller pool, so a passage that would have
                            # ranked first is absent and something worse took its place.
                            **({"kb_failures": kb_failures} if kb_failures else {}),
                        },
                    )
                elif retrieval.disclose_unsupported:
                    # Retrieval ran and found nothing. Record it so the transcript
                    # claim and the event log agree — the log is authoritative,
                    # since the in-prompt request is something a model can ignore.
                    await emit(
                        turn=turn,
                        seq=next_seq(),
                        event_type="document.unsupported",
                        agent_name=speaker_name,
                        payload={
                            "speaker": speaker_name,
                            "query": doc_query,
                            # Distinguishes "nothing matched at all" from "matched
                            # only below the similarity floor" — different causes,
                            # different fixes.
                            **({"floor_rejected": floor_rejected} if floor_rejected else {}),
                        },
                    )
            # Only ask for a disclosure when retrieval genuinely ran and came back
            # empty; a retrieval-off run must be byte-for-byte unchanged.
            disclose = bool(
                retrieval_on and not passages and retrieval.disclose_unsupported
            )
            # `round_state` is the frozen view in simultaneous mode, and IS the semantic
            # difference between the two modes: passing `conversation` here would let the
            # second persona in a round read the first, which is a rotation wearing this
            # mode's name.
            visible = round_state if blind else conversation
            response_data = await _generate_response(
                speaker_name, speaker, topic, visible, settings,
                model=model, cognition=cognition, retrieved_memories=retrieved,
                open_threads=open_threads, retrieved_passages=passages,
                disclose_unsupported=disclose, personas=personas,
                allow_pass=rounds_now,
                closing=in_closing,
                cite_inline=bool(retrieval_on and retrieval.cite_inline),
                consultants=consult_prompt,
                assumptions=assumptions_prompt,
            )

            # Phase 4a: pre-emit priority-hierarchy validation gate. The
            # candidate utterance is CHECKED (never edited) before it is
            # committed as agent.response. On a violation the whole turn is
            # regenerated (retry budget, default 1); a still-violating final
            # attempt is emitted as-is with a validation.flagged event. With
            # validation_enabled=False this block is skipped entirely —
            # byte-for-byte pre-4a behavior (regression-locked by test).
            citation_ctx = (
                CitationContext.build(
                    own_passages=passages, prior_firsthand=ledger,
                    earlier=read_before.get(speaker_name, ()),
                )
                if retrieval_on else None
            )
            if retrieval_on and passages:
                read_before.setdefault(speaker_name, set()).update(
                    (str(p.title), int(p.ordinal)) for p in passages
                )

            if settings.validation_enabled:
                attempt = 0
                while True:
                    candidate = response_data["content"]
                    # The engine's own error marker is not model output; there
                    # is nothing to validate (and nothing to regenerate from).
                    if candidate.startswith("[Error generating response"):
                        break
                    verdict = await validate_utterance(
                        candidate,
                        speaker_name,
                        list(agents.keys()) + [e.name for e in (experts or [])],
                        conversation,
                        settings,
                        model=model,
                        citation_context=citation_ctx,
                    )
                    # A selective LLM confirmation is a real cost — attribute
                    # it to the speaker like every other call this turn.
                    speaker.total_tokens_in += verdict["llm_tokens_in"]
                    speaker.total_tokens_out += verdict["llm_tokens_out"]
                    speaker.total_cost_usd += verdict["llm_cost_usd"]
                    checked_payload: Dict[str, Any] = {
                        "speaker": speaker_name,
                        "attempt": attempt,
                        "passed": verdict["ok"],
                    }
                    if verdict["method"] is not None:
                        checked_payload["method"] = verdict["method"]
                    if not verdict["ok"]:
                        checked_payload["principle"] = verdict["principle"]
                        checked_payload["reason"] = verdict["reason"]
                    # What this check cost, including — when it rejects and a regeneration
                    # follows — the attempt it threw away. Both were already added to the
                    # speaker's snapshot total below, and neither was on any EVENT, so the UI
                    # (which sums events) under-reported every regenerated turn. The attempt
                    # that is finally KEPT is costed on its `agent.response`, not here, so
                    # nothing is counted twice.
                    rejected = (not verdict["ok"]) and attempt < settings.validation_retry_budget
                    check_cost = float(verdict.get("llm_cost_usd") or 0.0) + (
                        float(response_data.get("cost_usd") or 0.0) if rejected else 0.0
                    )
                    if check_cost:
                        checked_payload["cost_usd"] = check_cost
                    await emit(
                        turn=turn,
                        seq=next_seq(),
                        event_type="validation.checked",
                        agent_name=speaker_name,
                        payload=checked_payload,
                    )
                    if verdict["ok"]:
                        break
                    if attempt >= settings.validation_retry_budget:
                        # Budget exhausted: emit the last attempt as-is,
                        # flagged. NEVER rewritten — that would fabricate
                        # cognition.
                        await emit(
                            turn=turn,
                            seq=next_seq(),
                            event_type="validation.flagged",
                            agent_name=speaker_name,
                            payload={
                                "speaker": speaker_name,
                                "principle": verdict["principle"],
                                "reason": verdict["reason"],
                                "attempts": attempt + 1,
                            },
                        )
                        break
                    # Reject-and-regenerate: the rejected attempt's real cost
                    # still counts (it happened); its cognition is discarded
                    # wholesale with the utterance (nothing from it is kept).
                    speaker.total_tokens_in += response_data["tokens_in"]
                    speaker.total_tokens_out += response_data["tokens_out"]
                    speaker.total_cost_usd += response_data["cost_usd"]
                    attempt += 1
                    response_data = await _generate_response(
                        speaker_name, speaker, topic, visible, settings,
                        allow_pass=rounds_now, closing=in_closing,
                        model=model, cognition=cognition,
                        retrieved_memories=retrieved,
                        open_threads=open_threads,
                        # Same passages as the rejected attempt: the regeneration
                        # is of the utterance, not of the retrieval, so re-querying
                        # would change the causal context mid-turn.
                        retrieved_passages=passages,
                        disclose_unsupported=disclose,
                        personas=personas,
                        cite_inline=bool(retrieval_on and retrieval.cite_inline),
                        consultants=consult_prompt,
                        assumptions=assumptions_prompt,
                    )

            # A pass never reaches the transcript. Emitted, so the run can say who was
            # asked and declined — the whole convergence signal in this mode is "everybody
            # passed", and that is only auditable if each pass is on the record.
            if rounds_now and response_data.get("passed"):
                round_passed.append(speaker_name)
                await emit(
                    turn=turn,
                    seq=next_seq(),
                    event_type="agent.passed",
                    agent_name=speaker_name,
                    payload={
                        "speaker": speaker_name,
                        "round": turn,
                        # The tokens were spent whether or not anything was said, and the
                        # per-content cost of this mode rises as the room quietens. Not
                        # recording it would make a converging run look free.
                        "tokens_in": response_data.get("tokens_in", 0),
                        "tokens_out": response_data.get("tokens_out", 0),
                        "cost_usd": response_data.get("cost_usd", 0.0),
                    },
                )
                speaker.total_tokens_in += response_data.get("tokens_in", 0)
                speaker.total_tokens_out += response_data.get("tokens_out", 0)
                speaker.total_cost_usd += response_data.get("cost_usd", 0.0)
                if not round_queue:
                    generated += 1
                    if in_closing:
                        in_closing, closing_done = False, True
                        if not round_spoke:
                            # Everybody declined their closing statement. The turn number is
                            # KEPT even though the round produced no messages, because
                            # `turn > max_messages` is how the next Lambda invocation knows
                            # the closing round already happened — decrementing it made the
                            # next slice open another one, for ever. Recorded on
                            # `sim.completed` instead so the count is not silently wrong.
                            closing_empty = True
                    elif not round_spoke:
                        # Nobody had anything to add. Not a judgement by a moderator — six
                        # personas were each asked and each declined, which is the strongest
                        # form this signal takes anywhere in the engine.
                        turn -= 1
                        converged = {
                            "reason": "every participant passed this round",
                            "at_turn": turn,
                        }
                        logger.info(
                            "Simulation %s converged at round %d of %d: all %d passed",
                            run_id, turn, max_messages, len(round_passed),
                        )
                        break
                continue

            # A consultation request is an instruction to the engine, not speech: strip it from the
            # message before it is recorded, and answer it after the message is on the record.
            ask = None
            if consult_prompt and consult_remaining > 0:
                cleaned, ask = experts_mod.parse_ask(response_data["content"], experts)
                if ask:
                    response_data["content"] = cleaned

            # Update conversation
            message = {
                "speaker": speaker_name,
                "content": response_data["content"],
                "turn": turn,
            }
            conversation.append(message)

            # Update agent state
            speaker.conversation_history.append(message)
            if len(speaker.conversation_history) > 50:
                speaker.conversation_history = speaker.conversation_history[-50:]

            speaker.total_tokens_in += response_data["tokens_in"]
            speaker.total_tokens_out += response_data["tokens_out"]
            speaker.total_cost_usd += response_data["cost_usd"]

            # Log event
            response_payload: Dict[str, Any] = {
                "speaker": speaker_name,
                "message": response_data["content"],
                "tokens_in": response_data["tokens_in"],
                "tokens_out": response_data["tokens_out"],
                "cost_usd": response_data["cost_usd"],
            }
            # Additive cognition fields only when present, so cognition-off
            # agent.response payloads stay byte-for-byte identical to pre-2c.
            if response_data.get("rationale") is not None:
                response_payload["rationale"] = response_data["rationale"]
            if response_data.get("goal_served") is not None:
                response_payload["goal_served"] = response_data["goal_served"]
            # Recorded only when it is FALSE, so a healthy run's payloads stay as they
            # were and the flag reads as an exception report rather than as noise. A
            # reader counting these gets the parse-failure rate for the run.
            if response_data.get("cognition_parsed") is False:
                response_payload["cognition_parsed"] = False
            # Additive, and the reason a reader can trust the ending: a final position
            # stated under the closing instruction is a different artefact from a turn in
            # the middle of an argument, and the analysis layer should be able to tell.
            if in_closing:
                response_payload["closing"] = True
            # Phase 2c memory: the retrieved memory ids are the causal refs that
            # were in-context for this turn (present only when memory is on).
            if memory_on:
                response_payload["memory_refs"] = [m.id for m in retrieved]
            # Phase 4b: the open-thread ids that were in-context for this turn
            # (the causal analogue of memory_refs; present only when threads on).
            if threads_on:
                response_payload["thread_refs"] = [t.id for t in open_threads]
            # Phase 5: the document chunk ids that were in-context for this turn.
            # Present only when retrieval actually returned something, so a
            # retrieval-off run's payload is byte-for-byte unchanged.
            if passages:
                response_payload["document_refs"] = [p.chunk_id for p in passages]
            # Phase 5i: how this turn came by its evidence. Makes an evidence
            # chain machine-readable — a claim can be traced back through the
            # participant who surfaced a document to the document itself, instead
            # of the hop being invisible in the record.
            if citation_ctx is not None:
                cites = analyse_citations(
                    response_data["content"], speaker_name,
                    list(agents.keys()) + [e.name for e in (experts or [])], citation_ctx,
                )
                if cites:
                    response_payload["citation_provenance"] = provenance_payload(cites)
                    for c in cites:
                        if c.kind == "firsthand":
                            ledger.append([speaker_name, c.title])
            await emit(
                turn=turn,
                seq=next_seq(),
                event_type="agent.response",
                agent_name=speaker_name,
                payload=response_payload,
            )

            if ask:
                await _consult(
                    ask[0], ask[1], speaker_name, turn, topic, conversation,
                    run_id=run_id, db=db, retrieval=retrieval, model=model, settings=settings,
                    emit=emit, next_seq=next_seq, ledger=ledger,
                    names=list(agents.keys()) + [e.name for e in experts],
                )

            # Phase 2c: form new memories AFTER the turn and append them to the
            # speaker's memory_stream (rides the per-turn snapshot). Each formed
            # memory emits an additive memory.formed event.
            if memory_on:
                for m in response_data.get("memories", []) or []:
                    item = MemoryItem(
                        timestamp=int(time.time()),
                        content=m["content"],
                        importance=m.get("importance"),
                        tags=m.get("tags", []),
                    )
                    speaker.memory_stream.append(item)
                    await emit(
                        turn=turn,
                        seq=next_seq(),
                        event_type="memory.formed",
                        agent_name=speaker_name,
                        payload={
                            "agent": speaker_name,
                            "id": item.id,
                            "content": item.content,
                            "importance": item.importance,
                            "tags": item.tags,
                        },
                    )

            # Phase 2c: dynamic goals — apply the speaker's self goal update.
            if goals_dynamic:
                new_goals = response_data.get("goal_update")
                if new_goals and new_goals != speaker.goals:
                    before = list(speaker.goals)
                    speaker.goals = list(new_goals)
                    await emit(
                        turn=turn,
                        seq=next_seq(),
                        event_type="goal.updated",
                        agent_name=speaker_name,
                        payload={
                            "agent": speaker_name,
                            "before": before,
                            "after": speaker.goals,
                        },
                    )

            # Phase 2c: relationships — apply the speaker's stance updates toward
            # OTHER real participants (never itself, never a non-member).
            if relationships_on:
                for other, stance in (response_data.get("relationship_updates") or {}).items():
                    if other == speaker_name or other not in agents:
                        continue
                    speaker.relationships[other] = stance
                    await emit(
                        turn=turn,
                        seq=next_seq(),
                        event_type="relationship.updated",
                        agent_name=speaker_name,
                        payload={
                            "agent": speaker_name,
                            "other": other,
                            "stance": stance,
                        },
                    )

            # Phase 4b: pending threads — apply the speaker's thread updates.
            # Opens append new PendingThread entries; resolves/abandons only
            # accept ids of threads that were genuinely OPEN and IN-CONTEXT
            # this turn (never a fabricated payoff of an unseen thread).
            if threads_on:
                updates = response_data.get("thread_updates") or {}
                in_context_ids = {t.id for t in open_threads}
                for spec in updates.get("open", []):
                    thread = PendingThread(
                        description=spec["description"],
                        thread_type=spec["thread_type"],
                        origin_turn=turn,
                        origin_agent=speaker_name,
                    )
                    pending_threads.append(thread)
                    await emit(
                        turn=turn,
                        seq=next_seq(),
                        event_type="thread.opened",
                        agent_name=speaker_name,
                        payload={
                            "id": thread.id,
                            "description": thread.description,
                            "thread_type": thread.thread_type,
                            "origin_turn": thread.origin_turn,
                            "origin_agent": thread.origin_agent,
                        },
                    )
                for status, key, event_type in (
                    ("resolved", "resolved", "thread.resolved"),
                    ("abandoned", "abandoned", "thread.abandoned"),
                ):
                    for tid in updates.get(key, []):
                        if tid not in in_context_ids:
                            continue
                        thread = next(
                            (t for t in pending_threads
                             if t.id == tid and t.status == "open"),
                            None,
                        )
                        if thread is None:
                            continue
                        thread.status = status
                        thread.resolved_turn = turn
                        await emit(
                            turn=turn,
                            seq=next_seq(),
                            event_type=event_type,
                            agent_name=speaker_name,
                            payload={
                                "id": thread.id,
                                "description": thread.description,
                                "thread_type": thread.thread_type,
                                "origin_turn": thread.origin_turn,
                                "resolved_turn": turn,
                            },
                        )

            # Phase 2c: reflection — every N turns the speaker condenses recent
            # memories into a higher-level belief (a MemoryItem tagged
            # 'reflection'), emitted as agent.reflected. Only fires when
            # reflection_every > 0 (ON by default when cognition is enabled).
            if reflect_every and turn % reflect_every == 0:
                belief = await _reflect(speaker, topic, settings, model=model)
                if belief and belief.get("content"):
                    speaker.total_tokens_in += belief.get("tokens_in", 0)
                    speaker.total_tokens_out += belief.get("tokens_out", 0)
                    speaker.total_cost_usd += belief.get("cost_usd", 0.0)
                    item = MemoryItem(
                        timestamp=int(time.time()),
                        content=belief["content"],
                        importance=0.9,
                        tags=["reflection", "belief"],
                    )
                    speaker.memory_stream.append(item)
                    await emit(
                        turn=turn,
                        seq=next_seq(),
                        event_type="agent.reflected",
                        agent_name=speaker_name,
                        payload={
                            "agent": speaker_name,
                            "id": item.id,
                            "belief": item.content,
                            "tokens_in": belief.get("tokens_in", 0),
                            "tokens_out": belief.get("tokens_out", 0),
                            "cost_usd": belief.get("cost_usd", 0.0),
                        },
                    )

            # Phase 2a: per-turn checkpoint — persist a full running snapshot for
            # this turn so state at turn N is reconstructable, then emit an
            # additive checkpoint.saved event (no existing consumer requires it).
            if db:
                await db.save_snapshot(
                    SimSnapshot(
                        run_id=run_id,
                        turn=turn,
                        topic=topic,
                        agents=agents,
                        conversation=conversation,
                        pending_threads=pending_threads,
                        firsthand_citations=ledger,
                        decline_streak=declines,
                        status="running",
                        created_at=int(time.time()),
                        total_turns=turn,
                    )
                )
            await emit(
                turn=turn,
                seq=next_seq(),
                event_type="checkpoint.saved",
                payload={"turn": turn},
            )

            last_speaker = speaker_name

            logger.info(f"Turn {turn}/{max_messages}: {speaker_name}: {response_data['content'][:100]}...")

            # A survivor. The round is only over when the queue is empty, and only then
            # does the run's per-call budget tick — a slice generates a whole round, not
            # a sixth of one, or `turn_budget=1` under Step Functions would stop mid-round
            # and the next invocation would open a fresh one with the earlier speakers lost.
            if rounds_now:
                round_spoke.append(speaker_name)
                if not round_queue:
                    generated += 1
                    logger.info(
                        "%s %d of %d: %d spoke, %d passed (%s)",
                        "Closing round" if in_closing else "Round",
                        turn, max_messages, len(round_spoke), len(round_passed),
                        ", ".join(round_passed) or "nobody",
                    )
                    if in_closing:
                        in_closing, closing_done = False, True

            # Operator stop, checked BEFORE the cost cap: if both would end the run
            # on the same turn, "you stopped it" is the more informative answer,
            # since a cap that was also reached would have stopped it anyway.
            if should_stop is not None and should_stop():
                completion_time = int(time.time())
                total_cost = sum(a.total_cost_usd for a in agents.values())
                await emit(
                    turn=turn,
                    seq=next_seq(),
                    event_type="sim.stopped",
                    payload={
                        "total_turns": turn,
                        "message_count": len(conversation),
                        "total_cost_usd": total_cost,
                    },
                )
                if db:
                    await db.save_snapshot(SimSnapshot(
                        run_id=run_id,
                        turn=turn,
                        topic=topic,
                        agents=agents,
                        conversation=conversation,
                        pending_threads=pending_threads,
                        firsthand_citations=ledger,
                        decline_streak=declines,
                        status="stopped",
                        created_at=completion_time,
                        completed_at=completion_time,
                        total_turns=turn,
                    ))
                    await db.update_run_status(run_id, "stopped", completion_time)
                logger.info(
                    "Simulation %s stopped by operator after turn %d ($%.4f)",
                    run_id, turn, total_cost,
                )
                return {
                    "run_id": run_id,
                    "status": "stopped",
                    "topic": topic,
                    "conversation": conversation,
                    "agents": {n: a.model_dump() for n, a in agents.items()},
                    "total_turns": turn,
                    "total_cost_usd": total_cost,
                }

            # Phase 3: check cost cap AFTER each turn (additive; when cap is 0 this
            # adds zero overhead). The cap acts on accumulated REAL cost_usd; when
            # litellm reports no cost for a call we count $0 for that call (no
            # fabrication). Cap-reached stops generation and ends in a terminal
            # "capped" status (so WebSocket closes).
            cap = settings.max_run_cost_usd
            if cap > 0:
                total_cost = sum(a.total_cost_usd for a in agents.values())
                if total_cost >= cap:
                    completion_time = int(time.time())
                    await emit(
                        turn=turn,
                        seq=next_seq(),
                        event_type="sim.capped",
                        payload={
                            "total_turns": turn,
                            "message_count": len(conversation),
                            "total_cost_usd": total_cost,
                            "cap_usd": cap,
                        },
                    )
                    if db:
                        snapshot = SimSnapshot(
                            run_id=run_id,
                            turn=turn,
                            topic=topic,
                            agents=agents,
                            conversation=conversation,
                            pending_threads=pending_threads,
                            firsthand_citations=ledger,
                            status="capped",
                            created_at=completion_time,
                            completed_at=completion_time,
                            total_turns=turn,
                        )
                        await db.save_snapshot(snapshot)
                        await db.update_run_status(run_id, "capped", completion_time)
                    logger.info(f"Simulation {run_id} capped at ${total_cost:.4f} (cap ${cap})")
                    return {
                        "run_id": run_id,
                        "status": "capped",
                        "topic": topic,
                        "conversation": conversation,
                        "agents": {name: agent.model_dump() for name, agent in agents.items()},
                        "total_turns": turn,
                        "total_cost_usd": total_cost,
                        "cap_usd": cap,
                    }

        # The loop exited. Two reasons are possible now, and conflating them would be
        # the worst bug in this phase: the run reached its budget (terminal), or THIS
        # CALL spent its per-invocation budget while the run has turns left
        # (non-terminal). Falling through to `sim.completed` in the second case would
        # write a terminal event and a `complete` status onto a run that is still
        # going — after which the state machine's next turn appends events past a
        # completion marker, and replay sees a run that finished twice.
        #
        # Nothing is emitted and no status is written here: the run is mid-flight and
        # its last turn already checkpointed itself. The caller gets `running`, which
        # is what `CheckContinue` routes on.
        # `converged is None` matters as much as the turn count: a converged run has turns
        # left in its budget by definition, so without this it would return `running`, and
        # the state machine would call the next turn Lambda and the run would never end.
        # A pending closing round is unfinished work exactly like a remaining turn: the slice
        # that completes the last round spends its budget and returns here, and reporting
        # `complete` would end the run before the closing round ever opened.
        closing_pending = closing_enabled and not closing_done and converged is None
        if (turn < max_messages or closing_pending) and converged is None:
            return {
                "run_id": run_id,
                "status": "running",
                "topic": topic,
                "conversation": conversation,
                "agents": {n: a.model_dump() for n, a in agents.items()},
                "total_turns": turn,
                "total_cost_usd": sum(a.total_cost_usd for a in agents.values()),
                # The ledger travels back so a caller that is NOT reloading from the
                # snapshot (an in-process batch loop) can hand it to the next call.
                "firsthand_citations": [list(p) for p in ledger],
            }

        # Simulation complete
        completion_time = int(time.time())
        total_cost = sum(a.total_cost_usd for a in agents.values())

        await emit(
            turn=turn,
            seq=next_seq(),
            event_type="sim.completed",
            payload={
                "total_turns": turn,
                "message_count": len(conversation),
                "total_cost_usd": total_cost,
                # Additive, and only when it happened. A `converged` STATUS was the first
                # design and was rejected: `TERMINAL_STATUSES` is duplicated in
                # `orchestration`, `storage.dynamo` and the SPA's `runStatus.ts`, and one
                # copy missing an entry has already caused two bugs of exactly that shape
                # (a stream that never ends, a button that never appears). What a reader
                # actually needs is not a new lifecycle state but the reason this run has
                # 26 turns when it asked for 40 — which is a payload field.
                # An empty closing round is a real outcome — everybody was asked for a
                # final position and nobody had one — and it is the only case where a turn
                # number counts a round that produced no messages.
                **({"closing_round_empty": True} if closing_empty else {}),
                **({"converged": True,
                    "converged_at_turn": converged["at_turn"],
                    "converged_reason": converged["reason"],
                    "turns_unused": max_messages - turn} if converged else {}),
            },
        )

        if db:
            # Save completion snapshot (retained unchanged from Phase 0; this is
            # the turn=final, status="complete" snapshot the analysis layer reads).
            snapshot = SimSnapshot(
                run_id=run_id,
                turn=turn,
                topic=topic,
                agents=agents,
                conversation=conversation,
                pending_threads=pending_threads,
                firsthand_citations=ledger,
                status="complete",
                created_at=completion_time,
                completed_at=completion_time,
                total_turns=turn,
            )
            await db.save_snapshot(snapshot)
            await db.update_run_status(run_id, "complete", completion_time)

        logger.info(f"Simulation {run_id} complete: {turn} turns")

        # Build result
        return {
            "run_id": run_id,
            "status": "complete",
            **({"converged": converged} if converged else {}),
            "topic": topic,
            "conversation": conversation,
            "agents": {name: agent.model_dump() for name, agent in agents.items()},
            "total_turns": turn,
            "total_cost_usd": total_cost,
        }

    except Exception as e:
        logger.error(f"Simulation {run_id} failed: {e}", exc_info=True)

        await emit(
            turn=turn,
            seq=next_seq(),
            event_type="sim.failed",
            payload={"error": str(e)},
        )
        if db:
            await db.update_run_status(run_id, "failed")

        return {
            "run_id": run_id,
            "status": "failed",
            "error": str(e),
            "topic": topic,
            "conversation": conversation,
            "agents": {name: agent.model_dump() for name, agent in agents.items()},
            "total_turns": turn,
        }


class BranchMutationError(ValueError):
    """A branch mutation references state that does not exist at the fork, or is
    otherwise invalid. Surfaced by the API as HTTP 422."""


async def _apply_branch_mutation(
    *,
    mutation: Dict[str, Any],
    run_id: str,
    from_turn: int,
    topic: str,
    agents: Dict[str, AgentState],
    conversation: List[Dict[str, Any]],
    max_messages: int,
    db: Optional[Database],
    emit: Callable[..., Awaitable[None]],
    next_seq: Callable[[], int],
    pending_threads: Optional[List[PendingThread]] = None,
    settings=None,
    model: Optional[str] = None,
) -> tuple[int, int]:
    """
    Apply ONE Phase 2b branch mutation to the reconstructed fork state, in place.

    Returns the ``(start_turn, max_messages)`` the forward loop should use.

    Mutation kinds implemented here (Phase 2b step 1):
      - ``inject_message`` {speaker, content}: append a message turn at
        ``from_turn + 1`` (persisted as a real branch ``agent.response`` event +
        running snapshot, flagged ``injected``), so the group sees it going
        forward. Bumps start_turn and budget by 1 so the injection does not
        consume a generation slot.
      - ``continue`` {add_budget}: no state change; extend the turn budget so the
        group keeps talking.

    State-only mutations (edit_goal / add_persona / remove_persona) and
    ``promote_aside`` arrive in later 2b steps.

    Phase 4c adds ``adaptive_pressure`` (EXPERIMENTAL, opt-in via
    settings.adaptive_pressure_enabled): observe run-level signals at the fork,
    generate ONE narrator-voiced world event under the hard agency guard, and
    inject it as a branch turn (same mechanics as inject_message, source
    "pressure") after emitting a ``pressure.applied`` audit event. A pressure
    text the guard rejects (after 1 retry) rejects the WHOLE intervention.

    The parent run is never touched: all writes here target ``run_id`` (the
    branch). Raises :class:`BranchMutationError` on invalid input.
    """
    kind = mutation.get("kind")

    if kind == "adaptive_pressure":
        from matrix_studio.pressure import (
            PressureRejectedError,
            generate_pressure,
            observe_signals,
        )

        if settings is None:
            settings = get_settings()
        if not settings.adaptive_pressure_enabled:
            raise BranchMutationError(
                "adaptive_pressure is experimental and disabled "
                "(set ADAPTIVE_PRESSURE_ENABLED=true to opt in)"
            )

        signals = observe_signals(
            conversation=conversation,
            pending_threads=pending_threads or [],
            from_turn=from_turn,
            max_messages=max_messages,
        )
        focus = str(mutation.get("focus", "")).strip() or None
        try:
            pressure = await generate_pressure(
                topic=topic,
                conversation=conversation,
                signals=signals,
                participants=list(agents.keys()),
                settings=settings,
                model=model,
                focus=focus,
            )
        except PressureRejectedError as e:
            # Rejected outright — nothing was emitted, nothing rewritten.
            raise BranchMutationError(str(e)) from e

        inject_turn = from_turn + 1
        # Audit event first: the observed signals + attempts, verbatim, so the
        # intervention is fully explainable in the event log.
        await emit(
            turn=inject_turn,
            seq=next_seq(),
            event_type="pressure.applied",
            agent_name="Narrator",
            payload={
                "signals": signals,
                "focus": focus,
                "attempts": pressure["attempts"],
                "tokens_in": pressure["tokens_in"],
                "tokens_out": pressure["tokens_out"],
                "cost_usd": pressure["cost_usd"],
            },
        )
        # Then the world event itself, as a real injected narrator turn (same
        # shape/mechanics as inject_message, so replay/UI need zero new logic).
        resolved = {
            "kind": "inject_message",
            "speaker": "Narrator",
            "content": pressure["content"],
            "source": "pressure",
        }
        if mutation.get("add_budget") is not None:
            resolved["add_budget"] = mutation["add_budget"]
        return await _apply_branch_mutation(
            mutation=resolved,
            run_id=run_id,
            from_turn=from_turn,
            topic=topic,
            agents=agents,
            conversation=conversation,
            max_messages=max_messages,
            db=db,
            emit=emit,
            next_seq=next_seq,
            pending_threads=pending_threads,
            settings=settings,
            model=model,
        )

    if kind == "continue":
        add_budget = int(mutation.get("add_budget", 0))
        if add_budget < 1:
            raise BranchMutationError("continue.add_budget must be >= 1")
        # Explicit "continue for exactly add_budget more turns from the fork" —
        # computed from from_turn directly so it is deterministic and does not
        # compound branch_budget's dead-branch auto-extension.
        return from_turn, from_turn + add_budget

    if kind == "inject_message":
        speaker = str(mutation.get("speaker", "")).strip()
        content = str(mutation.get("content", "")).strip()
        if not speaker:
            raise BranchMutationError("inject_message.speaker is required")
        if not content:
            raise BranchMutationError("inject_message.content is required")

        inject_turn = from_turn + 1
        message = {"speaker": speaker, "content": content, "turn": inject_turn}
        conversation.append(message)

        # If the injected speaker is an existing persona, thread it into their
        # own history so their later turns are aware of having "said" it. An
        # injected narrator/user speaker simply becomes a feed entry.
        if speaker in agents:
            agents[speaker].conversation_history.append(message)

        source = str(mutation.get("source", "user"))
        # Persist as a real branch turn so replay + live-watch render it. Uses
        # the existing agent.response shape (zero tokens/cost — no LLM call) with
        # an additive ``injected`` flag the UI can style; no existing consumer
        # requires the flag.
        await emit(
            turn=inject_turn,
            seq=next_seq(),
            event_type="agent.response",
            agent_name=speaker,
            payload={
                "speaker": speaker,
                "message": content,
                "tokens_in": 0,
                "tokens_out": 0,
                "cost_usd": 0.0,
                "injected": True,
                "source": source,
            },
        )
        if db:
            await db.save_snapshot(
                SimSnapshot(
                    run_id=run_id,
                    turn=inject_turn,
                    topic=topic,
                    agents=agents,
                    conversation=conversation,
                    pending_threads=pending_threads or [],
                    status="running",
                    created_at=int(time.time()),
                    total_turns=inject_turn,
                )
            )
        await emit(
            turn=inject_turn,
            seq=next_seq(),
            event_type="checkpoint.saved",
            payload={"turn": inject_turn},
        )

        # The injection occupies turn from_turn+1; resume generating after it.
        # The new round's length is configurable via add_budget (number of
        # LLM-generated discussion turns after the injection); when omitted it
        # defaults to the branch's inherited budget (the original run's budget).
        add_budget = mutation.get("add_budget")
        if add_budget is not None:
            effective_max = inject_turn + int(add_budget)
        else:
            # +1 so the injected (non-generated) turn does not eat a gen slot.
            effective_max = max_messages + 1
        return inject_turn, effective_max

    if kind == "edit_goal":
        persona_name = str(mutation.get("persona_name", "")).strip()
        goals = mutation.get("goals")
        if not persona_name:
            raise BranchMutationError("edit_goal.persona_name is required")
        if not isinstance(goals, list):
            raise BranchMutationError("edit_goal.goals must be a list")
        if persona_name not in agents:
            raise BranchMutationError(
                f"edit_goal: persona {persona_name!r} not in the cast at turn {from_turn}"
            )
        agents[persona_name] = agents[persona_name].model_copy(
            update={"goals": [str(g) for g in goals]}
        )
        await _save_mutation_snapshot(db, run_id, from_turn, topic, agents, conversation, pending_threads)
        return from_turn, max_messages

    if kind == "add_persona":
        name = str(mutation.get("name", "")).strip()
        persona_text = str(mutation.get("persona", "")).strip()
        goals = mutation.get("goals", [])
        if not name:
            raise BranchMutationError("add_persona.name is required")
        if not persona_text:
            raise BranchMutationError("add_persona.persona is required")
        if name in agents:
            raise BranchMutationError(
                f"add_persona: {name!r} already in the cast at turn {from_turn}"
            )
        agents[name] = AgentState(
            name=name,
            persona=persona_text,
            goals=[str(g) for g in goals],
        )
        await _save_mutation_snapshot(db, run_id, from_turn, topic, agents, conversation, pending_threads)
        return from_turn, max_messages

    if kind == "remove_persona":
        name = str(mutation.get("name", "")).strip()
        if not name:
            raise BranchMutationError("remove_persona.name is required")
        if name not in agents:
            raise BranchMutationError(
                f"remove_persona: {name!r} not in the cast at turn {from_turn}"
            )
        if len(agents) <= 1:
            raise BranchMutationError(
                "remove_persona: cannot remove the last persona (\u22651 required)"
            )
        del agents[name]
        await _save_mutation_snapshot(db, run_id, from_turn, topic, agents, conversation, pending_threads)
        return from_turn, max_messages

    if kind in ("replace_assumption", "withdraw_assumption"):
        # Working assumptions (matrix_studio/assumptions.py). The branch copied its parent's log up to the
        # fork, so the ledger there is read from it; the change is ONE event at the fork, and every turn
        # after reads the ledger with it applied (`assumptions.from_events`: a later event with the same
        # id replaces, a withdrawal removes). No persona line is rewritten — the reason assumptions are
        # events and not speech.
        aid = str(mutation.get("assumption_id") or "").strip()
        ledger = assumptions_mod.from_events(
            await db.get_events(run_id, to_turn=from_turn) if db is not None else []
        )
        current = next((a for a in ledger if a.id == aid), None)
        if current is None:
            raise BranchMutationError(
                f"{kind}: no assumption {aid!r} is in force at turn {from_turn}"
                + (f" (in force: {', '.join(a.id for a in ledger)})" if ledger else "")
            )
        if kind == "withdraw_assumption":
            await emit(turn=from_turn, seq=next_seq(), event_type="assumption.withdrawn", agent_name=None,
                       payload={"id": aid, "statement": current.statement, "turn": from_turn})
            return from_turn, max_messages
        statement = " ".join(str(mutation.get("statement") or "").split())[:assumptions_mod.MAX_STATEMENT_CHARS]
        if not statement:
            raise BranchMutationError("replace_assumption.statement is required")
        replaced = assumptions_mod.Assumption(
            aid, statement, " ".join(str(mutation.get("basis") or "").split())[:assumptions_mod.MAX_BASIS_CHARS],
            assumptions_mod.OPERATOR, from_turn,
        )
        await emit(turn=from_turn, seq=next_seq(), event_type="assumption.made", agent_name=None,
                   payload={**replaced.payload(), "replaces": current.statement,
                            "replaced_source": current.source})
        return from_turn, max_messages

    raise BranchMutationError(f"unknown branch mutation kind: {kind!r}")


async def _save_mutation_snapshot(
    db: Optional["Database"],
    run_id: str,
    turn: int,
    topic: str,
    agents: Dict[str, Any],
    conversation: List[Dict[str, Any]],
    pending_threads: Optional[List[PendingThread]] = None,
) -> None:
    """Re-persist the fork snapshot after a state-only mutation so the stored
    snapshot at ``turn`` reflects the mutation (not the pre-mutation state copied
    from the parent). ``db.save_snapshot`` is INSERT OR REPLACE, so this is an
    in-place overwrite. No-op when db is None."""
    if db is None:
        return
    await db.save_snapshot(
        SimSnapshot(
            run_id=run_id,
            turn=turn,
            topic=topic,
            agents=agents,  # type: ignore[arg-type]
            conversation=conversation,
            pending_threads=pending_threads or [],
            status="running",
            created_at=int(time.time()),
            total_turns=turn,
        )
    )


async def resume_simulation(
    run_id: str,
    topic: str,
    agents: Dict[str, AgentState],
    conversation: List[Dict[str, Any]],
    from_turn: int,
    start_seq: int,
    max_messages: int,
    db: Optional[Database] = None,
    on_event: Optional[OnEvent] = None,
    model: Optional[str] = None,
    mutation: Optional[Dict[str, Any]] = None,
    cognition: Optional[CognitionConfig] = None,
    pending_threads: Optional[List[PendingThread]] = None,
    retrieval: Optional[RetrievalConfig] = None,
    personas: Optional[PersonaConfig] = None,
    # Next-speaker fairness (A+B). None means the default, which is ON — a resumed or
    # branched run must not quietly become the unfair one.
    selection: Optional[SelectionConfig] = None,
    # Intervention H's decline streak as of `from_turn`, read off the snapshot by the
    # caller. 0 for a branch, which starts its own streak.
    decline_streak: int = 0,
    should_stop: Optional[Callable[[], bool]] = None,
    firsthand_citations: Optional[List[List[str]]] = None,
    # Phase 5: turns THIS call may generate (None = the run's whole remaining budget).
    # Passed straight through to `_run_turns`, which returns a non-terminal "running"
    # when the per-call budget rather than the run's budget is what stopped it.
    turn_budget: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Phase 2a branch primitive — RESUME generating forward from a checkpoint.

    Phase 4b: ``pending_threads`` is the ledger reconstructed as of the fork
    (replayed from thread.* events by the branch service); it continues forward
    on the branch exactly like agent state does.

    Additive engine entry (the fresh-start ``run_simulation`` path is untouched).
    The branch service has already: created the new run row (with parent_run_id /
    branch_turn), copied the parent's event log up to and including ``from_turn``
    into this ``run_id``, and seeded a snapshot at ``from_turn``. This function
    seeds the engine state from that checkpoint and generates NEW turns
    ``from_turn + 1 .. max_messages`` under the new ``run_id``, emitting the
    normal event stream + per-turn checkpoints (so live-watch and replay work for
    the branch with zero new machinery).

    It does NOT re-emit ``sim.started`` or regenerate avatars (those events were
    copied from the parent, so the branch replays identically up to the fork). It
    does NOT touch the parent run in any way. Non-determinism forward of the fork
    is expected and correct — we never re-run the original.

    Args:
        run_id: The NEW branch run id (already created by the service).
        topic: Conversation topic (copied from the parent).
        agents: Reconstructed agent states as of ``from_turn`` (with accumulated
            tokens/cost carried forward so the branch's cost continues, not resets).
        conversation: Full transcript as of ``from_turn``.
        from_turn: The fork turn (branch continues from ``from_turn + 1``).
        start_seq: Next per-run seq to use (continues after the copied events).
        max_messages: Turn budget for the branch (inherited from the parent).
        db: Database for event/snapshot persistence.
        on_event: Optional live-emit callback (same additive seam as a fresh run).
    """
    settings = get_settings()

    # Continue the per-run monotonic seq after the copied parent events so replay
    # ordering stays total across the copy/generate boundary.
    seq_counter = start_seq

    def _next_seq() -> int:
        nonlocal seq_counter
        s = seq_counter
        seq_counter += 1
        return s

    async def _emit(
        turn: int,
        seq: int,
        event_type: str,
        payload: Dict[str, Any],
        agent_name: Optional[str] = None,
    ) -> None:
        if db:
            await db.append_event(
                run_id=run_id,
                turn=turn,
                seq=seq,
                event_type=event_type,
                agent_name=agent_name,
                payload=payload,
            )
        if on_event is not None:
            event = {
                "run_id": run_id,
                "turn": turn,
                "seq": seq,
                "event_type": event_type,
                "agent_name": agent_name,
                "payload": payload,
            }
            try:
                await on_event(event)
            except Exception as cb_err:  # noqa: BLE001 - live emit must never break a run
                logger.warning("on_event callback failed for %s: %s", event_type, cb_err)

    # Seed last_speaker from the tail of the copied transcript so the first
    # generated turn's speaker selection sees continuity.
    last_speaker = conversation[-1]["speaker"] if conversation else None

    # Phase 2b: apply a single branch mutation at the fork BEFORE generating
    # forward. This is the only difference between a plain 2a fork and a 2b
    # intervention. The mutation edits the reconstructed (agents, conversation)
    # in place and may persist an injected turn (as a real branch event +
    # snapshot); it returns the effective start_turn + budget for the forward
    # loop. No-op when mutation is None (unchanged 2a behavior).
    effective_from_turn = from_turn
    effective_max = max_messages
    if mutation:
        effective_from_turn, effective_max = await _apply_branch_mutation(
            mutation=mutation,
            run_id=run_id,
            from_turn=from_turn,
            topic=topic,
            agents=agents,
            conversation=conversation,
            max_messages=max_messages,
            db=db,
            emit=_emit,
            next_seq=_next_seq,
            pending_threads=pending_threads,
            settings=settings,
            model=model,
        )
        last_speaker = conversation[-1]["speaker"] if conversation else last_speaker

    logger.info(
        "Resuming simulation %s from turn %d (budget %d turns)",
        run_id,
        effective_from_turn,
        effective_max,
    )

    # Consultants come from the run's STORED config, not from a caller parameter: three callers resume
    # a run (branch, resume, the orchestrator's slice), and a setting each had to remember to pass is
    # the shape of bug that has shipped features inert here before. One read per call.
    expert_list: List[Any] = []
    assumption_list: List[Any] = []
    dynamic = None
    limit = 0
    if db is not None:
        try:
            row = await db.get_run(run_id)
            cfg = json.loads((row or {}).get("config_json") or "{}")
            expert_list, limit = experts_mod.from_config(cfg), experts_mod.consult_limit(cfg)
            # The ledger as of the fork, from the run's own events: the operator's (turn 0) and any the
            # moderator made since. A branch copied its parent's log, so it inherits them — and a
            # later event with the same id is how a fork replaces one.
            # Config only when the log holds no assumption events at all (a run from before they were
            # recorded). An EMPTY ledger from the log is an answer — a fork withdrew the last one — and
            # falling back then would put the withdrawn assumption straight back.
            logged = await db.get_events(run_id, to_turn=effective_from_turn)
            assumption_list = (
                assumptions_mod.from_events(logged)
                if any(str(e.get("event_type", "")).startswith("assumption.") for e in logged)
                else assumptions_mod.from_config(cfg)
            )
            dynamic = assumptions_mod.dynamic_from_config(cfg)
        except Exception as exc:  # noqa: BLE001 — no consultants rather than a failed turn
            logger.warning("Could not read consultants for %s: %s", run_id, exc)

    return await _run_turns(
        run_id=run_id,
        topic=topic,
        agents=agents,
        conversation=conversation,
        last_speaker=last_speaker,
        start_turn=effective_from_turn,
        max_messages=effective_max,
        settings=settings,
        db=db,
        emit=_emit,
        next_seq=_next_seq,
        model=model,
        cognition=cognition,
        pending_threads=pending_threads,
        firsthand_citations=firsthand_citations,
        turn_budget=turn_budget,
        retrieval=retrieval,
        personas=personas,
        selection=selection,
        decline_streak=decline_streak,
        experts=expert_list,
        consult_limit=limit,
        assumptions=assumption_list,
        dynamic_assumptions=dynamic,
        should_stop=should_stop,
    )
