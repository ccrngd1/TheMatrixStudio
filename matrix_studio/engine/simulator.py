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
from typing import Any, Dict, List, Optional

import litellm

from matrix_studio.avatar import generate_avatar
from matrix_studio.settings import get_settings
from matrix_studio.state import AgentState, MemoryItem, SimSnapshot
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
) -> str:
    """
    Use LLM to select the next speaker.

    Args:
        topic: Conversation topic
        agents: Dict of agent states
        conversation: Conversation history
        last_speaker: Last speaker name or None
        settings: Global settings

    Returns:
        Selected agent name
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

    selection_prompt = f"""You are a conversation moderator. Given the following personas and recent conversation about "{topic}", select who should speak next.

Personas:
{personas_desc}

Recent conversation:
{conv_summary}

Last speaker: {last_speaker or 'None (start of conversation)'}

Respond with ONLY the name of the persona who should speak next. Choose naturally based on conversation flow."""

    messages = [{"role": "user", "content": selection_prompt}]

    try:
        response = await litellm.acompletion(
            model=settings.litellm_model,
            messages=messages,
            temperature=0.3,  # Lower temperature for more consistent selection
            max_tokens=50,
        )

        selected = response.choices[0].message.content.strip()

        # Validate selection
        for name in agent_names:
            if name.lower() in selected.lower():
                return name

        # Fallback: if unclear, pick someone other than last speaker
        candidates = [n for n in agent_names if n != last_speaker]
        return candidates[0] if candidates else agent_names[0]

    except Exception as e:
        logger.error(f"Error selecting speaker: {e}", exc_info=True)
        # Fallback
        candidates = [n for n in agent_names if n != last_speaker]
        return candidates[0] if candidates else agent_names[0]


async def _generate_response(
    speaker_name: str,
    agent: AgentState,
    topic: str,
    conversation: List[Dict[str, Any]],
    settings,
) -> Dict[str, Any]:
    """
    Generate a response from the selected speaker.

    Args:
        speaker_name: Name of speaking agent
        agent: Agent state
        topic: Conversation topic
        conversation: Full conversation history
        settings: Global settings

    Returns:
        Dict with response, tokens, and cost info
    """
    # Build context for the agent
    recent_conv = conversation[-20:] if len(conversation) > 20 else conversation
    conv_text = "\n".join([f"{msg['speaker']}: {msg['content']}" for msg in recent_conv])

    system_message = f"""{agent.persona}

You are participating in a conversation about: {topic}

Your goals: {', '.join(agent.goals) if agent.goals else 'Engage authentically'}

Respond naturally as this character. Keep responses conversational (2-4 sentences)."""

    messages = [
        {"role": "system", "content": system_message},
        {"role": "user", "content": f"Recent conversation:\n{conv_text}\n\nRespond as {speaker_name}:"},
    ]

    try:
        response = await litellm.acompletion(
            model=settings.litellm_model,
            messages=messages,
            temperature=settings.litellm_temperature,
            max_tokens=settings.litellm_max_tokens,
        )

        content = response.choices[0].message.content.strip()

        # Extract usage info
        usage = response.usage
        tokens_in = usage.prompt_tokens if usage else 0
        tokens_out = usage.completion_tokens if usage else 0

        # Estimate cost (litellm sometimes provides this)
        cost_usd = 0.0
        if hasattr(response, "_hidden_params") and "response_cost" in response._hidden_params:
            cost_usd = response._hidden_params["response_cost"]

        return {
            "content": content,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
        }

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
) -> Dict[str, Any]:
    """
    Run a complete simulation from a request dict.

    Args:
        request: Simulation request with topic, cast, and optional config
        db: Optional database for event persistence
        run_id: Optional run ID (generated if not provided)

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
            }
        }
    """
    settings = get_settings()

    # Parse request
    topic = request["topic"]
    cast = request["cast"]
    config = request.get("config", {})
    max_messages = config.get("max_messages", settings.max_messages)
    generate_avatars_flag = config.get("generate_avatars", settings.enable_avatars)

    # Generate run ID
    if run_id is None:
        run_id = str(uuid.uuid4())

    logger.info(f"Starting simulation {run_id}: {topic}")

    # Initialize agents
    agents: Dict[str, AgentState] = {}
    for persona in cast:
        agent = AgentState(
            name=persona["name"],
            persona=persona["persona"],
            goals=persona.get("goals", []),
        )
        agents[agent.name] = agent

    # Generate avatars in parallel
    if generate_avatars_flag:
        logger.info("Generating avatars...")
        avatar_tasks = [
            generate_avatar(agent.name, agent.persona)
            for agent in agents.values()
        ]
        avatars = await asyncio.gather(*avatar_tasks)
        for agent, avatar_b64 in zip(agents.values(), avatars):
            agent.portrait = avatar_b64

    # Create run in database
    if db:
        await db.create_run(
            run_id=run_id,
            topic=topic,
            cast=cast,
            config=config,
        )
        await db.update_run_status(run_id, "running")
        await db.append_event(
            run_id=run_id,
            turn=0,
            seq=0,
            event_type="sim.started",
            payload={"topic": topic, "agent_count": len(agents)},
        )

    # Simulation loop
    conversation: List[Dict[str, Any]] = []
    last_speaker: Optional[str] = None
    turn = 0
    seq = 1

    try:
        while turn < max_messages:
            turn += 1

            # Phase 1: Select next speaker
            speaker_name = await _select_next_speaker(
                topic, agents, conversation, last_speaker, settings
            )

            if db:
                await db.append_event(
                    run_id=run_id,
                    turn=turn,
                    seq=seq,
                    event_type="speaker.selected",
                    agent_name=speaker_name,
                    payload={"speaker": speaker_name, "candidates": list(agents.keys())},
                )
                seq += 1

            # Phase 2: Generate response
            speaker = agents[speaker_name]
            response_data = await _generate_response(
                speaker_name, speaker, topic, conversation, settings
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
            if db:
                await db.append_event(
                    run_id=run_id,
                    turn=turn,
                    seq=seq,
                    event_type="agent.response",
                    agent_name=speaker_name,
                    payload={
                        "speaker": speaker_name,
                        "message": response_data["content"],
                        "tokens_in": response_data["tokens_in"],
                        "tokens_out": response_data["tokens_out"],
                        "cost_usd": response_data["cost_usd"],
                    },
                )
                seq += 1

            last_speaker = speaker_name

            logger.info(f"Turn {turn}/{max_messages}: {speaker_name}: {response_data['content'][:100]}...")

        # Simulation complete
        completion_time = int(time.time())

        if db:
            await db.append_event(
                run_id=run_id,
                turn=turn,
                seq=seq,
                event_type="sim.completed",
                payload={"total_turns": turn, "message_count": len(conversation)},
            )

            # Save completion snapshot
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
            "total_cost_usd": sum(a.total_cost_usd for a in agents.values()),
        }

    except Exception as e:
        logger.error(f"Simulation {run_id} failed: {e}", exc_info=True)

        if db:
            await db.append_event(
                run_id=run_id,
                turn=turn,
                seq=seq,
                event_type="sim.failed",
                payload={"error": str(e)},
            )
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
