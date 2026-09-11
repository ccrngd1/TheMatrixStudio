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
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

import litellm

# Type alias for the Phase 1 live-emit callback. It receives one structured
# event dict (same shape as a persisted row) for each event the engine emits.
OnEvent = Callable[[Dict[str, Any]], Awaitable[None]]

from matrix_studio.avatar import generate_avatar, store_avatar
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
    SimSnapshot,
)
from matrix_studio.storage import Database
from matrix_studio.validation import validate_utterance

logger = logging.getLogger(__name__)

# Configure litellm logging
litellm.suppress_debug_info = True

# Drop provider-unsupported sampling params instead of erroring.
#
# Measured 2026-09-06: `bedrock/global.anthropic.claude-sonnet-5` accepts ONLY
# temperature=1, so every call raised UnsupportedParamsError and the engine wrote
# the error text into the transcript AS THE CHARACTER'S SPEECH:
#
#   "[Error generating response: litellm.UnsupportedParamsError: ... does not
#    support temperature=0.7. Only temperature=1 is supported.]"
#
# The engine passes temperature from settings (0.7), 0.3 for speaker selection and
# 0.0 for the validation gate and reflection, so a model with parameter
# restrictions failed on every path at once. "Provider-agnostic" is a stated
# project goal (PROJECT-SPEC §7); assuming every model accepts our sampling
# params is not provider-agnostic.
#
# Dropping is the right trade here: a slightly different temperature is a far
# smaller loss than a run of error strings, and the alternative — per-model
# capability tables in this codebase — is exactly the provider coupling LiteLLM
# exists to avoid.
litellm.drop_params = True


async def _select_next_speaker(
    topic: str,
    agents: Dict[str, AgentState],
    conversation: List[Dict[str, Any]],
    last_speaker: Optional[str],
    settings,
    model: Optional[str] = None,
    cognition: Optional[CognitionConfig] = None,
    personas: Optional[PersonaConfig] = None,
) -> tuple[str, Optional[str]]:
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
        ``(selected_agent_name, reason_or_None)``
    """
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

    messages = [{"role": "user", "content": selection_prompt}]

    def _match(text: str) -> Optional[str]:
        for name in agent_names:
            if name.lower() in text.lower():
                return name
        return None

    try:
        kwargs: Dict[str, Any] = dict(
            model=model or settings.litellm_model,
            messages=messages,
            temperature=0.3,  # Lower temperature for more consistent selection
            max_tokens=120 if cognition_on else 50,
        )
        if cognition_on:
            kwargs["response_format"] = {"type": "json_object"}
        response = await litellm.acompletion(**kwargs)

        raw = response.choices[0].message.content.strip()

        reason: Optional[str] = None
        selected = raw
        if cognition_on:
            # Tolerant parse: this model wraps JSON in a markdown fence, which a bare
            # json.loads rejects. See matrix_studio/jsonio.py.
            parsed = extract_json_object(raw)
            if parsed is not None:
                selected = str(parsed.get("speaker", "")).strip() or raw
                r = parsed.get("reason")
                reason = str(r).strip() if r else None

        # Validate selection
        matched = _match(selected)
        if matched is not None:
            return matched, reason

        # Fallback: if unclear, pick someone other than last speaker
        candidates = [n for n in agent_names if n != last_speaker]
        return (candidates[0] if candidates else agent_names[0]), reason

    except Exception as e:
        logger.error(f"Error selecting speaker: {e}", exc_info=True)
        # Fallback
        candidates = [n for n in agent_names if n != last_speaker]
        return (candidates[0] if candidates else agent_names[0]), None


async def _generate_response(
    speaker_name: str,
    agent: AgentState,
    topic: str,
    conversation: List[Dict[str, Any]],
    settings,
    model: Optional[str] = None,
    cognition: Optional[CognitionConfig] = None,
    retrieved_memories: Optional[List["MemoryItem"]] = None,
    open_threads: Optional[List["PendingThread"]] = None,
    retrieved_passages: Optional[List[Any]] = None,
    disclose_unsupported: bool = False,
    personas: Optional[PersonaConfig] = None,
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
        documents_block = format_documents_block(retrieved_passages)
    elif disclose_unsupported:
        documents_block = format_unsupported_block()
    else:
        documents_block = ""

    if cognition_on:
        # Compose the JSON schema from the enabled cognition sub-features so the
        # single structured call carries exactly what's turned on.
        fields = [
            '"utterance": "<what you say, in character, 2-4 sentences>"',
            '"rationale": "<one first-person sentence: why you say this now>"',
            '"goal_served": "<which of your goals this advances, verbatim, or \'none\'>"',
        ]
        extra_instr = ""
        if memory_on:
            fields.append(
                '"memories": [{"content": "<a short thing you just learned or decided this turn>", '
                '"importance": <0.0-1.0>, "tags": ["<tag>"]}]'
            )
            extra_instr += (
                " The memories array holds 0-2 items you genuinely formed this turn "
                "(what you learned/decided); use [] if nothing notable."
            )
        if goals_dynamic:
            fields.append('"goal_update": ["<your full updated goal list>"]')
            extra_instr += (
                " Set goal_update to your FULL new goal list ONLY if this turn "
                "genuinely changed your goals; otherwise omit it or use null."
            )
        if relationships_on:
            fields.append(
                '"relationship_updates": {"<other participant name>": "<your one-line stance toward them>"}'
            )
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

    messages = [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_content},
    ]

    try:
        kwargs: Dict[str, Any] = dict(
            model=model or settings.litellm_model,
            messages=messages,
            temperature=settings.litellm_temperature,
            max_tokens=settings.litellm_max_tokens,
        )
        if cognition_on:
            kwargs["response_format"] = {"type": "json_object"}
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
            "content": content,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
        }
        # Additive only when cognition is on, so the cognition-off event/result
        # payloads stay byte-for-byte identical to pre-2c.
        if cognition_on:
            result["rationale"] = rationale
            result["goal_served"] = goal_served
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
    run_name = request.get("name")
    run_description = request.get("description")
    # Who the run belongs to. Absent for a direct engine call (CLI, tests, the
    # measurement scripts), which have no notion of a user; the API always sets it.
    # `create_run`'s own default handles the absent case, and it fails closed —
    # see its docstring for why the read paths do not get that courtesy.
    run_owner_sub = request.get("owner_sub")

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
        )
        await db.update_run_status(run_id, "running")

    # sim.started is emitted at turn 0, seq 0 (Phase 0 parity).
    await _emit(
        turn=0,
        seq=_next_seq(),
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
            await _emit(
                turn=0,
                seq=_next_seq(),
                event_type="persona.structured",
                agent_name=agent.name,
                payload={
                    "agent_name": agent.name,
                    "structured": payload,
                    "withhold_concerns": personas_cfg.withhold_concerns,
                    "dismissal_rule": personas_cfg.dismissal_rule,
                },
            )

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
            await _emit(
                turn=0,
                seq=_next_seq(),
                event_type="avatar.ready",
                agent_name=agent.name,
                payload={"agent_name": agent.name, "portrait_key": agent.portrait_key},
            )

        await asyncio.gather(*[_make_avatar(a) for a in agents.values()])

    # Phase 5: ingest documents declared on cast members before the first turn,
    # so a persona's background is available from turn 1. Ingestion is local file
    # I/O only (no LLM, no network) and a failure never fails the run — the
    # persona simply has no background material, which the prompt states honestly.
    if db and retrieval.enabled:
        await _ingest_cast_documents(run_id, cast, db, _emit, _next_seq)
        # Phase 5f: embed the freshly ingested chunks when a vector mode is on.
        # Done once here rather than lazily per turn so the per-turn hot path
        # only pays for the query embedding.
        if retrieval.mode in ("vector", "hybrid"):
            stats = await embed_pending_chunks(
                db, run_id, embedding_model=retrieval.embedding_model
            )
            await _emit(
                turn=0,
                seq=_next_seq(),
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

    # Fresh start: no prior turns, no seed conversation.
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
        model=config.get("model") or None,
        cognition=cognition,
        retrieval=retrieval,
        personas=personas_cfg,
        should_stop=should_stop,
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
            model=model or settings.litellm_model,
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
    should_stop: Optional[Callable[[], bool]] = None,
    firsthand_citations: Optional[List[List[str]]] = None,
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

    try:
        while turn < max_messages:
            turn += 1

            # Phase 1: Select next speaker
            speaker_name, selection_reason = await _select_next_speaker(
                topic, agents, conversation, last_speaker, settings,
                model=model, cognition=cognition, personas=personas,
            )

            # speaker.selected payload is additive-only: the reason key appears
            # only when cognition produced one, so cognition-off runs stay
            # byte-for-byte identical to pre-2c.
            speaker_payload: Dict[str, Any] = {
                "speaker": speaker_name,
                "candidates": list(agents.keys()),
            }
            if selection_reason:
                speaker_payload["reason"] = selection_reason
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
            if retrieval_on:
                passages, doc_query, floor_rejected = await retrieve_for_turn(
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
                                }
                                for p in passages
                            ],
                            "total_chars": sum(len(p.content) for p in passages),
                            **({"floor_rejected": floor_rejected} if floor_rejected else {}),
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
            response_data = await _generate_response(
                speaker_name, speaker, topic, conversation, settings,
                model=model, cognition=cognition, retrieved_memories=retrieved,
                open_threads=open_threads, retrieved_passages=passages,
                disclose_unsupported=disclose, personas=personas,
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
                    own_passages=passages, prior_firsthand=ledger
                )
                if retrieval_on else None
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
                        list(agents.keys()),
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
                        speaker_name, speaker, topic, conversation, settings,
                        model=model, cognition=cognition,
                        retrieved_memories=retrieved,
                        open_threads=open_threads,
                        # Same passages as the rejected attempt: the regeneration
                        # is of the utterance, not of the retrieval, so re-querying
                        # would change the causal context mid-turn.
                        retrieved_passages=passages,
                        disclose_unsupported=disclose,
                        personas=personas,
                    )

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
                    list(agents.keys()), citation_ctx,
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
    should_stop: Optional[Callable[[], bool]] = None,
    firsthand_citations: Optional[List[List[str]]] = None,
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
        retrieval=retrieval,
        personas=personas,
        should_stop=should_stop,
    )
