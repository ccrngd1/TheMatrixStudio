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

from matrix_studio.avatar import generate_avatar
from matrix_studio.settings import get_settings
from matrix_studio.state import AgentState, CognitionConfig, MemoryItem, SimSnapshot
from matrix_studio.storage import Database

logger = logging.getLogger(__name__)

# Configure litellm logging
litellm.suppress_debug_info = True


async def _select_next_speaker(
    topic: str,
    agents: Dict[str, AgentState],
    conversation: List[Dict[str, Any]],
    last_speaker: Optional[str],
    settings,
    model: Optional[str] = None,
    cognition: Optional[CognitionConfig] = None,
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

    Returns:
        ``(selected_agent_name, reason_or_None)``
    """
    agent_names = list(agents.keys())

    # Build selection prompt
    personas_desc = "\n".join(
        [f"- {name}: {agents[name].persona}" for name in agent_names]
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
            try:
                parsed = json.loads(raw)
                selected = str(parsed.get("speaker", "")).strip() or raw
                r = parsed.get("reason")
                reason = str(r).strip() if r else None
            except (json.JSONDecodeError, TypeError, AttributeError):
                selected = raw
                reason = None

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

    # Build context for the agent
    recent_conv = conversation[-20:] if len(conversation) > 20 else conversation
    conv_text = "\n".join([f"{msg['speaker']}: {msg['content']}" for msg in recent_conv])

    goals_line = ', '.join(agent.goals) if agent.goals else 'Engage authentically'

    if cognition_on:
        system_message = f"""{agent.persona}

You are participating in a conversation about: {topic}

Your goals: {goals_line}

Respond naturally as this character. Keep responses conversational (2-4 sentences).

Return ONLY a JSON object of the form:
{{"utterance": "<what you say, in character, 2-4 sentences>", "rationale": "<one first-person sentence: why you say this now>", "goal_served": "<which of your goals this advances, verbatim, or 'none'>"}}
The rationale must be your genuine reason for this specific turn; do not invent facts."""
    else:
        system_message = f"""{agent.persona}

You are participating in a conversation about: {topic}

Your goals: {goals_line}

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
        if cognition_on:
            try:
                parsed = json.loads(raw)
                content = str(parsed.get("utterance", "")).strip() or raw
                rat = parsed.get("rationale")
                rationale = str(rat).strip() if rat else None
                gs = parsed.get("goal_served")
                goal_served = str(gs).strip() if gs else None
            except (json.JSONDecodeError, TypeError, AttributeError):
                # Graceful degradation: keep the raw text as the utterance.
                content = raw
                rationale = None
                goal_served = None

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
    run_name = request.get("name")
    run_description = request.get("description")

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

    # Initialize agents
    agents: Dict[str, AgentState] = {}
    for persona in cast:
        agent = AgentState(
            name=persona["name"],
            persona=persona["persona"],
            goals=persona.get("goals", []),
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
        )
        await db.update_run_status(run_id, "running")

    # sim.started is emitted at turn 0, seq 0 (Phase 0 parity).
    await _emit(
        turn=0,
        seq=_next_seq(),
        event_type="sim.started",
        payload={"topic": topic, "agent_count": len(agents)},
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
            agent.portrait = portrait
            # avatar.ready lives outside the turn stream (turn 0); give it its
            # own seq so ordering stays total and replay is deterministic.
            await _emit(
                turn=0,
                seq=_next_seq(),
                event_type="avatar.ready",
                agent_name=agent.name,
                payload={"agent_name": agent.name, "portrait_b64": portrait},
            )

        await asyncio.gather(*[_make_avatar(a) for a in agents.values()])

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
    )


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
) -> Dict[str, Any]:
    """
    Shared turn loop + completion/failure handling for both a fresh run and a
    resumed branch. Generates turns ``start_turn + 1 .. max_messages``.

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

    try:
        while turn < max_messages:
            turn += 1

            # Phase 1: Select next speaker
            speaker_name, selection_reason = await _select_next_speaker(
                topic, agents, conversation, last_speaker, settings,
                model=model, cognition=cognition,
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
            response_data = await _generate_response(
                speaker_name, speaker, topic, conversation, settings,
                model=model, cognition=cognition,
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
            await emit(
                turn=turn,
                seq=next_seq(),
                event_type="agent.response",
                agent_name=speaker_name,
                payload=response_payload,
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

    The parent run is never touched: all writes here target ``run_id`` (the
    branch). Raises :class:`BranchMutationError` on invalid input.
    """
    kind = mutation.get("kind")

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
        await _save_mutation_snapshot(db, run_id, from_turn, topic, agents, conversation)
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
        await _save_mutation_snapshot(db, run_id, from_turn, topic, agents, conversation)
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
        await _save_mutation_snapshot(db, run_id, from_turn, topic, agents, conversation)
        return from_turn, max_messages

    raise BranchMutationError(f"unknown branch mutation kind: {kind!r}")


async def _save_mutation_snapshot(
    db: Optional["Database"],
    run_id: str,
    turn: int,
    topic: str,
    agents: Dict[str, Any],
    conversation: List[Dict[str, Any]],
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
) -> Dict[str, Any]:
    """
    Phase 2a branch primitive — RESUME generating forward from a checkpoint.

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
    )
