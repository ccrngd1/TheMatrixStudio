# SPDX-License-Identifier: Apache-2.0
"""
Branch primitive — the Phase 2a fork-and-resume service.

A branch is a NEW run that copies a parent run's history up to and including a
fork turn ``from_turn`` and then RESUMES generating forward from
``from_turn + 1`` as its own timeline. The parent run is NEVER modified or
re-run (the core immutability invariant).

This module owns the pure, DB-facing pieces of that operation:

  * ``reconstruct_at_turn`` — rebuild the exact engine state (agents +
    conversation) as of a turn by REPLAYING the parent's event log. Replay is
    used (rather than trusting a per-turn snapshot) because it is always
    available and correct — imported runs (e.g. the ``bridge-kibble`` fixture)
    carry only a completion snapshot, and every run has a full event log. It
    also tolerates the two historical ``agent.response`` payload shapes
    (``message`` from the live engine, ``content`` from imports).
  * ``create_branch_run`` — synchronously create the new run row (with
    ``parent_run_id`` / ``branch_turn`` and a memorable codename) so it is
    immediately resolvable, watchable, and visible in history.
  * ``execute_branch`` — the background half: copy the parent event log up to
    the fork, seed a snapshot at the fork, then resume generation forward via
    the additive ``resume_simulation`` engine entry.

NO mutation is applied at the fork in Phase 2a (that is Phase 2b); a branch is a
clean "continue forward from turn N" fork. Non-determinism forward of the fork
is expected and correct — we never re-run the original.
"""

import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from matrix_studio.engine import resume_simulation
from matrix_studio.engine.simulator import OnEvent
from matrix_studio.naming import generate_run_name
from matrix_studio.settings import get_settings
from matrix_studio.state import (
    AgentState,
    CognitionConfig,
    PendingThread,
    RetrievalConfig,
    SimSnapshot,
)
from matrix_studio.storage import Database

logger = logging.getLogger(__name__)


def _parse_config(run: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort parse of a run row's config_json."""
    raw = run.get("config_json")
    if not raw:
        return {}
    try:
        cfg = json.loads(raw)
        return cfg if isinstance(cfg, dict) else {}
    except json.JSONDecodeError:
        return {}


def _load_cast(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Load a run's cast (persona name + real stored persona text + goals)."""
    try:
        cast = json.loads(run["cast_json"])
        return cast if isinstance(cast, list) else []
    except (KeyError, json.JSONDecodeError):
        return []


def branch_budget(parent_run: Dict[str, Any], from_turn: int) -> int:
    """
    Resolve the branch's turn budget (``max_messages``).

    A branch inherits the parent's configured budget so it plays by the same
    rules. If the fork point is at or past that budget (which would generate
    ZERO new turns — a dead branch), we extend the budget by a fresh allotment
    from ``from_turn`` so the fork always moves forward. This is the only place
    the branch's budget can differ from the parent's, and it never touches the
    parent.
    """
    settings = get_settings()
    cfg = _parse_config(parent_run)
    base = cfg.get("max_messages") or settings.max_messages
    if from_turn >= base:
        return from_turn + base
    return base


async def reconstruct_at_turn(
    db: Database, parent_run: Dict[str, Any], from_turn: int
) -> Tuple[str, Dict[str, "AgentState"], List[Dict[str, Any]], List["PendingThread"]]:
    """
    Reconstruct the exact engine state as of ``from_turn`` for ``parent_run`` by
    replaying its event log (read-only — the parent is never touched).

    Returns ``(topic, agents, conversation, pending_threads)`` where ``agents``
    is a name-> :class:`AgentState` dict seeded from the run's cast (real
    persona/goals) and populated with the per-agent conversation history +
    accumulated token/cost as of the fork, ``conversation`` is the transcript up
    to and including ``from_turn``, and ``pending_threads`` is the Phase 4b
    ledger replayed from ``thread.opened/resolved/abandoned`` events (each
    event payload carries the full thread entry, so replay is lossless; [] for
    runs that never used threads).

    ``agent.response`` payloads are tolerated in both shapes: the live engine
    writes ``message`` + token/cost fields; imported runs write ``content`` and
    may omit tokens/cost (treated as 0).
    """
    topic = parent_run.get("topic", "")
    cast = _load_cast(parent_run)

    agents: Dict[str, AgentState] = {}
    for persona in cast:
        agent = AgentState(
            name=persona["name"],
            persona=persona.get("persona", ""),
            goals=persona.get("goals", []),
        )
        agents[agent.name] = agent

    conversation: List[Dict[str, Any]] = []
    pending_threads: List[PendingThread] = []
    threads_by_id: Dict[str, PendingThread] = {}

    # Replay only up to and including the fork turn.
    events = await db.get_events(parent_run["id"], from_turn=0, to_turn=from_turn)
    for event in events:
        etype = event["event_type"]
        if etype not in (
            "agent.response", "thread.opened", "thread.resolved", "thread.abandoned"
        ):
            continue
        payload = event.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        payload = payload or {}

        # Phase 4b: fold thread events into the ledger.
        if etype == "thread.opened":
            fields: Dict[str, Any] = {
                "description": payload.get("description", ""),
                "thread_type": payload.get("thread_type", "setup"),
                "origin_turn": int(payload.get("origin_turn") or event["turn"]),
                "origin_agent": payload.get("origin_agent"),
            }
            if payload.get("id"):
                fields["id"] = payload["id"]
            thread = PendingThread(**fields)
            pending_threads.append(thread)
            threads_by_id[thread.id] = thread
            continue
        if etype in ("thread.resolved", "thread.abandoned"):
            thread = threads_by_id.get(payload.get("id", ""))
            if thread is not None:
                thread.status = "resolved" if etype == "thread.resolved" else "abandoned"
                thread.resolved_turn = int(payload.get("resolved_turn") or event["turn"])
            continue

        speaker = payload.get("speaker") or event.get("agent_name")
        if not speaker:
            continue
        content = payload.get("message")
        if content is None:
            content = payload.get("content", "")

        message = {"speaker": speaker, "content": content, "turn": event["turn"]}
        conversation.append(message)

        # A speaker may not be in the declared cast for legacy data; add it so
        # the reconstructed state stays faithful to the transcript.
        if speaker not in agents:
            agents[speaker] = AgentState(name=speaker, persona="", goals=[])
        agent = agents[speaker]
        agent.conversation_history.append(message)
        if len(agent.conversation_history) > 50:
            agent.conversation_history = agent.conversation_history[-50:]
        agent.total_tokens_in += int(payload.get("tokens_in") or 0)
        agent.total_tokens_out += int(payload.get("tokens_out") or 0)
        agent.total_cost_usd += float(payload.get("cost_usd") or 0.0)

    return topic, agents, conversation, pending_threads


async def create_branch_run(
    db: Database,
    parent_run: Dict[str, Any],
    from_turn: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    gen_model: Optional[str] = None,
    mutation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Synchronously create the new branch run row (so it is immediately resolvable
    by id/name, watchable over WS, and listed in history) and return its
    metadata. Resolves a memorable codename (reuse Phase 1 naming; a
    user-supplied name is honoured and de-duplicated). Sets ``parent_run_id`` /
    ``branch_turn`` and records the parent config's model so downstream analysis
    defaults match the parent.

    The heavy work (event copy + resume) is done separately in
    ``execute_branch`` on a background task — this function does NOT block on
    generation and does NOT touch the parent.
    """
    import uuid

    branch_run_id = str(uuid.uuid4())
    topic = parent_run.get("topic", "")
    cast = _load_cast(parent_run)
    cast_names = [c.get("name", "") for c in cast]
    parent_label = parent_run.get("name") or parent_run["id"][:8]

    # Resolve the branch's GENERATION model:
    #   1. an explicit user override (gen_model) wins;
    #   2. else inherit the parent's own configured model — BUT only if the
    #      parent was not imported (an imported run's stored model is a legacy,
    #      possibly-EOL string, so we drop it and use the settings default);
    #   3. else None -> the engine's current settings default.
    parent_cfg = _parse_config(parent_run)
    parent_imported = bool(parent_cfg.get("imported"))
    resolved_model: Optional[str] = (gen_model or "").strip() or None
    if resolved_model is None and not parent_imported:
        resolved_model = parent_cfg.get("model") or None

    # Resolve a codename. A user-supplied name is honoured (de-duplicated);
    # otherwise generate one from the topic. Naming uses the resolved (valid)
    # generation model — never the parent's possibly-EOL model — so branches of
    # imported runs get proper LLM codenames instead of a wordlist fallback.
    # Naming never blocks a branch.
    supplied = (name or "").strip().lower() or None
    name_source: Optional[str] = "user" if supplied else None
    if supplied and await db.name_exists(supplied):
        base = supplied
        for suffix in range(2, 100):
            candidate = f"{base}-{suffix}"
            if not await db.name_exists(candidate):
                supplied = candidate
                break

    if supplied:
        codename = supplied
        slug = supplied
    else:
        naming = await generate_run_name(
            topic=topic,
            cast_names=cast_names,
            model=resolved_model,
            name_exists=db.name_exists,
        )
        codename = naming["name"]
        slug = naming["slug"]
        name_source = naming["source"]

    # Default description records the lineage; a supplied one wins. Editable
    # later, same as any run.
    if not description:
        description = f"Branch of {parent_label} @ turn {from_turn}"

    # Carry the parent's config forward (so budget/summary settings match), but
    # drop the imported flag (a branch is a freshly generated timeline) and
    # replace the model with the resolved one (dropped entirely when None, so
    # analysis + generation fall back to the current settings default).
    cfg = dict(parent_cfg)
    cfg.pop("imported", None)
    cfg.pop("source", None)
    cfg.pop("model", None)
    if resolved_model:
        cfg["model"] = resolved_model
    cfg["max_messages"] = branch_budget(parent_run, from_turn)
    # Phase 2b: record the mutation applied at the fork (if any) so the branch is
    # self-describing and the tree UI can label the edge. Always dropped from a
    # parent's carried config first (a branch's mutation is its own).
    cfg.pop("branch_mutation", None)
    if mutation:
        cfg["branch_mutation"] = mutation

    await db.create_run(
        run_id=branch_run_id,
        topic=topic,
        cast=cast,
        name=codename,
        description=description,
        slug=slug,
        config=cfg,
        parent_run_id=parent_run["id"],
        branch_turn=from_turn,
    )

    return {
        "run_id": branch_run_id,
        "name": codename,
        "slug": slug,
        "name_source": name_source,
        "description": description,
        "topic": topic,
        "parent_run_id": parent_run["id"],
        "parent_name": parent_run.get("name"),
        "branch_turn": from_turn,
        "status": "running",
        "max_messages": cfg["max_messages"],
        "model": resolved_model,
        "mutation": mutation,
    }


async def execute_branch(
    db: Database,
    parent_run: Dict[str, Any],
    branch_run_id: str,
    from_turn: int,
    max_messages: int,
    on_event: Optional[OnEvent] = None,
    model: Optional[str] = None,
    mutation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Background half of a branch: reconstruct state at the fork, copy the parent's
    event log up to and including ``from_turn`` into the branch, seed a snapshot
    at the fork, then RESUME generating forward via the additive engine entry.

    Reads the parent only; all writes go to ``branch_run_id``. The branch emits
    the normal event stream + per-turn checkpoints under its own id, so
    live-watch and replay work with zero new machinery.
    """
    await db.update_run_status(branch_run_id, "running")

    topic, agents, conversation, pending_threads = await reconstruct_at_turn(
        db, parent_run, from_turn
    )

    # Copy the parent's event log up to and including the fork (preserving
    # turn/seq), so the branch replays byte-for-byte identically up to the fork.
    copied = await db.copy_events_upto(parent_run["id"], branch_run_id, from_turn)
    logger.info(
        "Branch %s: copied %d parent events up to turn %d",
        branch_run_id,
        copied,
        from_turn,
    )

    # Seed a snapshot at the fork so the branch has a checkpoint at from_turn
    # (running) even before it generates its first new turn.
    await db.save_snapshot(
        SimSnapshot(
            run_id=branch_run_id,
            turn=from_turn,
            topic=topic,
            agents=agents,
            conversation=conversation,
            pending_threads=pending_threads,
            status="running",
            created_at=int(time.time()),
            total_turns=from_turn,
        )
    )

    # Continue the per-run seq after the copied events so replay ordering stays
    # total across the copy/generate boundary.
    start_seq = await db.max_seq(branch_run_id) + 1

    # Phase 2b: resolve promote_aside -> inject_message before the engine sees
    # it. Aside resolution needs the DB; doing it here keeps the engine handler
    # DB-agnostic. The resolved mutation is a plain inject_message dict and
    # carries the original thread/message ids for the config record.
    resolved_mutation = mutation
    if mutation and mutation.get("kind") == "promote_aside":
        resolved_mutation = await _resolve_promote_aside(db, mutation, branch_run_id)

    # Phase 4b: a branch continues with the parent's cognition config (carried
    # into the branch's own config by create_branch_run), so cognition state —
    # including the pending-thread ledger reconstructed above — keeps evolving
    # forward. Runs without a cognition config get the disabled default
    # (unchanged pre-4b branch behavior).
    branch_run = await db.get_run(branch_run_id)
    branch_config = _parse_config(branch_run or {})
    cognition = CognitionConfig.from_config(branch_config)

    # Phase 5: a branch inherits the parent's attached documents, otherwise its
    # personas would silently lose the background material they had been
    # reasoning from at the fork. Copied (not shared) so the branch owns its rows
    # and deleting a parent's document cannot alter a recorded branch.
    retrieval = RetrievalConfig.from_config(branch_config)
    if retrieval.enabled:
        copied_docs = await db.copy_documents_to_run(parent_run["id"], branch_run_id)
        if copied_docs:
            logger.info(
                "Branch %s: copied %d parent documents", branch_run_id, copied_docs
            )

    return await resume_simulation(
        run_id=branch_run_id,
        topic=topic,
        agents=agents,
        conversation=conversation,
        from_turn=from_turn,
        start_seq=start_seq,
        max_messages=max_messages,
        db=db,
        on_event=on_event,
        model=model,
        mutation=resolved_mutation,
        cognition=cognition,
        pending_threads=pending_threads,
        retrieval=retrieval,
    )


async def _resolve_promote_aside(
    db: Database,
    mutation: Dict[str, Any],
    branch_run_id: str,
) -> Dict[str, Any]:
    """
    Resolve a ``promote_aside`` mutation: look up the aside thread + target
    message and return a plain ``inject_message`` dict the engine can apply.

    Raises :class:`BranchMutationError` if the thread or message is not found
    or does not belong to the same run as the branch.
    """
    thread_id = str(mutation.get("thread_id", "")).strip()
    message_id = mutation.get("message_id")  # integer row id
    if not thread_id:
        raise BranchMutationError("promote_aside.thread_id is required")
    if message_id is None:
        raise BranchMutationError("promote_aside.message_id is required")

    thread = await db.get_thread(thread_id)
    if not thread:
        raise BranchMutationError(f"promote_aside: thread {thread_id!r} not found")

    messages = await db.get_thread_messages(thread_id)
    target = next((m for m in messages if m["id"] == int(message_id)), None)
    if target is None:
        raise BranchMutationError(
            f"promote_aside: message {message_id} not found in thread {thread_id!r}"
        )

    # Use the aside's attributed speaker (the target in the aside) or fall back
    # to a generic attributed label. The injected message is the aside reply text.
    speaker = target.get("speaker") or thread.get("persona_name") or "Aside"
    content = target["content"]

    return {
        "kind": "inject_message",
        "speaker": speaker,
        "content": content,
        "source": "aside",
        # Keep original ids so config.branch_mutation is self-describing.
        "thread_id": thread_id,
        "message_id": int(message_id),
    }


RESUMABLE_STATUSES = {"interrupted", "failed"}


async def resume_run_in_place(
    db: Database,
    run: Dict[str, Any],
    on_event: Optional[OnEvent] = None,
) -> Dict[str, Any]:
    """
    Error-recovery: RESUME an ``interrupted``/``failed`` run forward IN PLACE.

    Unlike a branch (which forks into a NEW run to protect a *completed*,
    canonical timeline), this keeps the run's identity/codename and continues
    generating on the same run id. It is legitimate precisely because a
    resumable run never completed — it is a broken in-progress run, so finishing
    it forward is not rewriting canonical history.

    Steps:
      1. Resolve the last complete checkpoint turn (max per-turn snapshot).
      2. Trim the dangling tail past that turn (partial next turn + the
         ``sim.interrupted`` marker) so the log stays clean/replayable.
      3. Reconstruct engine state at the checkpoint by replaying the log.
      4. Flip status to ``running`` and resume generation forward from the
         checkpoint under the SAME run id.

    A ``complete`` run is never resumed here (guarded by the caller); use a
    branch for that.
    """
    run_id = run["id"]

    # 1. Last complete checkpoint. Absent any checkpoint (interrupted before the
    #    first turn landed), resume from turn 0 (fresh state, empty transcript).
    resume_turn = await db.last_checkpoint_turn(run_id)
    if resume_turn is None:
        resume_turn = 0

    # 2. Trim the incomplete tail past the checkpoint (dangling partial turn +
    #    the interrupt marker) so the run continues cleanly and replay has no
    #    phantom mid-stream terminal event.
    removed = await db.truncate_after_turn(run_id, resume_turn)
    logger.info(
        "Resume %s: trimmed %d dangling event(s) past checkpoint turn %d",
        run_id,
        removed,
        resume_turn,
    )

    # 3. Reconstruct state at the checkpoint (read-only replay of the log).
    topic, agents, conversation, pending_threads = await reconstruct_at_turn(
        db, run, resume_turn
    )

    # Ensure a snapshot exists at the resume point so the run has a checkpoint
    # there even if we resumed from turn 0 (no prior checkpoint).
    if await db.get_snapshot(run_id, turn=resume_turn) is None:
        await db.save_snapshot(
            SimSnapshot(
                run_id=run_id,
                turn=resume_turn,
                topic=topic,
                agents=agents,
                conversation=conversation,
                pending_threads=pending_threads,
                status="running",
                created_at=int(time.time()),
                total_turns=resume_turn,
            )
        )

    # 4. Flip to running and continue forward. Budget is the run's own
    #    configured budget, extended (via the shared branch_budget helper) if the
    #    checkpoint is already at/over it so a resume always moves forward. The
    #    run continues with its own configured model (None -> settings default).
    await db.update_run_status(run_id, "running")
    max_messages = branch_budget(run, resume_turn)
    start_seq = await db.max_seq(run_id) + 1
    resume_cfg = _parse_config(run)
    resume_model = resume_cfg.get("model") or None

    return await resume_simulation(
        run_id=run_id,
        topic=topic,
        agents=agents,
        conversation=conversation,
        from_turn=resume_turn,
        start_seq=start_seq,
        max_messages=max_messages,
        db=db,
        on_event=on_event,
        model=resume_model,
        # Phase 4b: an in-place resume continues with the run's OWN cognition
        # config + replayed thread ledger (a run that used threads keeps them).
        cognition=CognitionConfig.from_config(resume_cfg),
        pending_threads=pending_threads,
        # Phase 5: an in-place resume keeps its own documents — they are already
        # attached to this run_id, so nothing needs copying.
        retrieval=RetrievalConfig.from_config(resume_cfg),
    )
