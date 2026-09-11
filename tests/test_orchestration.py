# SPDX-License-Identifier: Apache-2.0
"""Phase 5: the slice — one Step Functions iteration of a run's turn loop.

These cover the properties the state machine depends on and cannot check itself:
that a slice generates the turns it was asked for and no more, that its result is
small enough for a 256 KB payload, that a retry does not duplicate a turn, and that
a stop and a budget end the run in the right terminal status.

A run cannot execute at all without this module (Phase 4 is cancelled — Lambda
freezes the sandbox when the handler returns), so "the loop works" is not covered
anywhere else.
"""

import json
from unittest.mock import patch

import pytest

from matrix_studio import orchestration

pytestmark = pytest.mark.asyncio


class _Resp:
    """Minimal litellm response stand-in.

    `_hidden_params["response_cost"]` is where the engine actually reads cost from —
    not `litellm.completion_cost`. A stub without it reports $0 for every call, which
    silently makes any cost-cap assertion untestable.
    """

    def __init__(self, content: str, cost: float = 0.0):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]
        self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5})()
        self._hidden_params = {"response_cost": cost}


CAST = [
    {"name": "Ada", "persona": "an engineer", "goals": ["ship it"]},
    {"name": "Bo", "persona": "a sceptic", "goals": ["find the flaw"]},
]


def _fake_llm(*args, **kwargs):
    return _make_llm()(*args, **kwargs)


def _make_llm(cost: float = 0.0):
    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Ada", "reason": "her turn"}))
        if "consistency validator" in text:
            return _Resp(json.dumps({"violation": False}))
        return _Resp("A short contribution that moves the point along.", cost=cost)
    return fake


async def _make_run(db, run_id, *, max_messages=4, status="pending"):
    await db.create_run(
        run_id=run_id, topic="whether to ship on Friday", cast=CAST,
        config={"max_messages": max_messages, "generate_avatars": False},
    )
    if status != "pending":
        await db.update_run_status(run_id, status)
    return await db.get_run(run_id)


# --------------------------------------------------------------------------- #
# The slice generates exactly its budget
# --------------------------------------------------------------------------- #


async def test_a_slice_generates_one_turn_and_reports_running(db):
    await _make_run(db, "sl-one", max_messages=4)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        out = await orchestration.execute_slice(db, "sl-one", turn=0, turn_budget=1)
    assert out["turn"] == 1
    assert out["status"] == "running"
    assert out["done"] is False
    # One turn means one response, not "at least one".
    events = await db.get_events("sl-one")
    responses = [e for e in events if e["event_type"] == "agent.response"]
    assert len(responses) == 1


async def test_successive_slices_advance_the_turn(db):
    await _make_run(db, "sl-adv", max_messages=4)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        first = await orchestration.execute_slice(db, "sl-adv", turn=0, turn_budget=1)
        second = await orchestration.execute_slice(
            db, "sl-adv", turn=first["turn"], turn_budget=1
        )
    assert (first["turn"], second["turn"]) == (1, 2)
    assert second["status"] == "running"
    # The transcript accumulated across the boundary rather than restarting, which is
    # the whole question a per-turn Lambda raises.
    snap = await db.get_snapshot("sl-adv", 2)
    assert len(snap.conversation) == 2


async def test_the_final_slice_completes_the_run(db):
    await _make_run(db, "sl-fin", max_messages=2)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.execute_slice(db, "sl-fin", turn=0, turn_budget=1)
        last = await orchestration.execute_slice(db, "sl-fin", turn=1, turn_budget=1)
    assert last["status"] == "complete"
    assert last["done"] is True
    run = await db.get_run("sl-fin")
    assert run["status"] == "complete"
    types = {e["event_type"] for e in await db.get_events("sl-fin")}
    assert "sim.completed" in types


async def test_a_mid_run_slice_emits_no_terminal_event(db):
    """The worst available bug in this phase, so it gets its own test.

    Falling through to `sim.completed` when only the PER-CALL budget was spent would
    mark a live run finished, and the next slice would then append turns past a
    completion marker — a log that replays as a run which ended twice.
    """
    await _make_run(db, "sl-mid", max_messages=5)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        out = await orchestration.execute_slice(db, "sl-mid", turn=0, turn_budget=2)
    assert out["turn"] == 2 and out["status"] == "running"
    types = {e["event_type"] for e in await db.get_events("sl-mid")}
    assert not (types & {"sim.completed", "sim.failed", "sim.stopped", "sim.capped"}), (
        f"a mid-run slice wrote a terminal event: {types}"
    )
    assert (await db.get_run("sl-mid"))["status"] == "running"


# --------------------------------------------------------------------------- #
# The payload has to stay small — the 256 KB quota is the reason state is reloaded
# --------------------------------------------------------------------------- #


async def test_the_next_input_is_small_and_carries_no_engine_state(db):
    """A Step Functions state's I/O is capped at 256 KB; snapshots reach 2.2 MB.

    So the payload must not carry the transcript. Asserting the key SET rather than a
    byte size is what makes this hold as the engine grows: a `conversation` key added
    to a slice's result later would pass a size check on a short test conversation and
    fail in production on a long one.
    """
    await _make_run(db, "sl-small", max_messages=3)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        out = await orchestration.execute_slice(db, "sl-small", turn=0, turn_budget=1)
    nxt = orchestration.next_input(out)
    assert set(nxt) == {
        "run_id", "owner_sub", "turn", "max_messages", "total_cost_usd"
    }
    for banned in ("conversation", "agents", "snapshot", "events", "pending_threads"):
        assert banned not in nxt
    assert len(json.dumps(nxt)) < 1024


async def test_next_input_ignores_extra_keys_it_was_not_told_about(db):
    """Built field by field, not copy-and-strip — so a new large key cannot ride along."""
    nxt = orchestration.next_input({
        "run_id": "r", "owner_sub": "u", "turn": 3, "max_messages": 9,
        "total_cost_usd": 0.5,
        "conversation": [{"content": "x" * 100_000}],
        "agents": {"Ada": {"memory_stream": ["huge"] * 1000}},
    })
    assert set(nxt) == {
        "run_id", "owner_sub", "turn", "max_messages", "total_cost_usd"
    }


# --------------------------------------------------------------------------- #
# Idempotence, which is what makes `Retry` safe
# --------------------------------------------------------------------------- #


async def test_a_slice_trims_a_dangling_partial_turn_before_generating(db):
    """A retry must not append a second copy of the turn that failed halfway.

    Simulated by appending events past the checkpoint, which is exactly the state a
    slice that died between `append_event` and `save_snapshot` leaves behind.
    """
    await _make_run(db, "sl-retry", max_messages=4)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.execute_slice(db, "sl-retry", turn=0, turn_budget=1)

    seq = await db.max_seq("sl-retry")
    await db.append_event(
        run_id="sl-retry", turn=2, seq=seq + 1, event_type="speaker.selected",
        agent_name="Ada", payload={"speaker": "Ada", "candidates": ["Ada", "Bo"]},
    )
    await db.append_event(
        run_id="sl-retry", turn=2, seq=seq + 2, event_type="agent.response",
        agent_name="Ada",
        payload={"speaker": "Ada", "message": "a half-written turn",
                 "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0},
    )

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        out = await orchestration.execute_slice(db, "sl-retry", turn=1, turn_budget=1)

    assert out["turn"] == 2
    responses = [
        e for e in await db.get_events("sl-retry")
        if e["event_type"] == "agent.response" and e["turn"] == 2
    ]
    assert len(responses) == 1, (
        f"turn 2 exists {len(responses)} times; a retry duplicated it"
    )
    payload = responses[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["message"] != "a half-written turn", "the stale turn survived"


async def test_a_slice_resumes_from_the_checkpoint_not_the_input_hint(db):
    """The input turn is a hint the machine carries; the checkpoint is the truth.

    Trusting the hint would let a retry resume from a turn that was never persisted.
    """
    await _make_run(db, "sl-hint", max_messages=5)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.execute_slice(db, "sl-hint", turn=0, turn_budget=1)
        # Lie: claim we are at turn 4 when only turn 1 is checkpointed.
        out = await orchestration.execute_slice(db, "sl-hint", turn=4, turn_budget=1)
    assert out["turn"] == 2, "the slice believed the hint over the checkpoint"


# --------------------------------------------------------------------------- #
# Stop and cost cap — the two things §6 moves out of in-memory state
# --------------------------------------------------------------------------- #


async def test_a_requested_stop_ends_the_run_after_the_turn_in_flight(db):
    await _make_run(db, "sl-stop", max_messages=6)
    assert await db.set_stop_requested("sl-stop") is True
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        out = await orchestration.execute_slice(db, "sl-stop", turn=0, turn_budget=1)
    assert out["status"] == "stopped"
    assert out["done"] is True
    # The turn in flight was finished and persisted — a stop is not a cancellation.
    responses = [
        e for e in await db.get_events("sl-stop") if e["event_type"] == "agent.response"
    ]
    assert len(responses) == 1, "the stop discarded the turn it was generating"
    assert (await db.get_run("sl-stop"))["status"] == "stopped"


async def test_the_stop_flag_is_durable_and_clearable(db):
    await _make_run(db, "sl-flag", max_messages=2)
    assert (await db.get_run("sl-flag")).get("stop_requested") in (None, False)
    await db.set_stop_requested("sl-flag")
    assert (await db.get_run("sl-flag"))["stop_requested"] is True
    # Clearing matters: a run stopped once would otherwise stop again one turn into
    # every later resume, which reads as the resume not working.
    await db.set_stop_requested("sl-flag", False)
    assert (await db.get_run("sl-flag"))["stop_requested"] is False


async def test_a_stop_for_a_missing_run_writes_nothing(db):
    """`UpdateItem` upserts, so an unconditional write would manufacture a run."""
    assert await db.set_stop_requested("no-such-run") is False
    assert await db.get_run("no-such-run") is None


async def test_a_cost_cap_terminates_the_run_as_capped(db, monkeypatch):
    monkeypatch.setenv("MAX_RUN_COST_USD", "0.000001")
    import matrix_studio.settings
    matrix_studio.settings._settings = None
    await _make_run(db, "sl-cap", max_messages=6)
    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_make_llm(cost=0.01)):
        out = await orchestration.execute_slice(db, "sl-cap", turn=0, turn_budget=1)
    assert out["status"] == "capped"
    assert (await db.get_run("sl-cap"))["status"] == "capped"


# --------------------------------------------------------------------------- #
# Terminal-state handling
# --------------------------------------------------------------------------- #


async def test_a_slice_on_a_terminal_run_is_a_no_op(db):
    """A retry after the run finished, or a stop that landed between slices."""
    await _make_run(db, "sl-term", max_messages=2, status="complete")
    before = len(await db.get_events("sl-term"))
    out = await orchestration.execute_slice(db, "sl-term", turn=2, turn_budget=1)
    assert out["status"] == "complete" and out["done"] is True
    assert len(await db.get_events("sl-term")) == before, "it generated past the end"


async def test_finalise_is_idempotent(db):
    await _make_run(db, "sl-idem", max_messages=1)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.execute_slice(db, "sl-idem", turn=0, turn_budget=1)
    first = await db.get_run("sl-idem")
    assert first["status"] == "complete"
    again = await orchestration.finalise(db, "sl-idem")
    assert again["status"] == "complete"
    # A second call must not move the completion time.
    assert (await db.get_run("sl-idem"))["completed_at"] == first["completed_at"]


async def test_a_slice_for_a_missing_run_raises(db):
    """Loud, because a state machine holding an id for a run that is gone is a bug
    in the caller, not a state to route on."""
    with pytest.raises(ValueError, match="does not exist"):
        await orchestration.execute_slice(db, "never-created", turn=0)


async def test_a_slice_flips_pending_to_running(db):
    """`POST /api/runs` writes the row as `pending`; the first slice owns the flip."""
    await _make_run(db, "sl-pend", max_messages=3, status="pending")
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.execute_slice(db, "sl-pend", turn=0, turn_budget=1)
    assert (await db.get_run("sl-pend"))["status"] == "running"


# --------------------------------------------------------------------------- #
# State loading
# --------------------------------------------------------------------------- #


async def test_load_state_prefers_the_snapshot_over_replay(db):
    """The snapshot is the O(1) path Phase 2a paid for; replay is O(turn).

    Asserted by watching whether the log is read at all, because both sources return
    equivalent state on a healthy run — so a test comparing their VALUES would pass
    whichever was used, and the cost difference is the whole point.
    """
    await _make_run(db, "sl-load", max_messages=3)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.execute_slice(db, "sl-load", turn=0, turn_budget=1)
    run = await db.get_run("sl-load")
    with patch("matrix_studio.branching.reconstruct_at_turn") as replay:
        topic, agents, conversation, _threads, _ledger = await orchestration.load_state(
            db, run, 1
        )
    replay.assert_not_called()
    assert topic and len(conversation) == 1 and set(agents) == {"Ada", "Bo"}


async def test_load_state_falls_back_to_replay_without_a_snapshot(db):
    """An imported run, or one interrupted before its first checkpoint."""
    await _make_run(db, "sl-noshot", max_messages=3)
    await db.append_event(
        run_id="sl-noshot", turn=1, seq=0, event_type="agent.response",
        agent_name="Ada",
        payload={"speaker": "Ada", "message": "from the log alone",
                 "tokens_in": 3, "tokens_out": 2, "cost_usd": 0.01},
    )
    run = await db.get_run("sl-noshot")
    assert await db.get_snapshot("sl-noshot", 1) is None, "fixture must have no snapshot"
    _topic, agents, conversation, _threads, _ledger = await orchestration.load_state(
        db, run, 1
    )
    assert [m["content"] for m in conversation] == ["from the log alone"]
    # Replay must restore the persona text from the cast, not just the transcript —
    # otherwise the next turn generates for a blank character.
    assert agents["Ada"].persona == "an engineer"
    assert agents["Ada"].total_cost_usd == pytest.approx(0.01)


async def test_load_state_restores_persona_text_a_snapshot_omitted(db):
    """A snapshot's agents carry cost and memory; the cast carries who they are.

    A snapshot written without persona text would otherwise generate the rest of the
    conversation for an empty character, which is a quality failure that raises
    nothing.
    """
    from matrix_studio.state import AgentState, SimSnapshot

    await _make_run(db, "sl-persona", max_messages=3)
    await db.save_snapshot(SimSnapshot(
        run_id="sl-persona", turn=1, topic="whether to ship on Friday",
        agents={"Ada": AgentState(name="Ada", persona="", goals=[])},
        conversation=[{"speaker": "Ada", "content": "hi", "turn": 1}],
        status="running", created_at=0, total_turns=1,
    ))
    run = await db.get_run("sl-persona")
    _topic, agents, _conv, _threads, _ledger = await orchestration.load_state(db, run, 1)
    assert agents["Ada"].persona == "an engineer"
    assert agents["Ada"].goals == ["ship it"]
    # And the cast member missing from the snapshot is added back.
    assert "Bo" in agents


# --------------------------------------------------------------------------- #
# prepare_run — the turn-0 work, shared with the local path
# --------------------------------------------------------------------------- #


async def test_prepare_emits_sim_started(db):
    await _make_run(db, "pr-start", max_messages=3)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        out = await orchestration.prepare_run(db, "pr-start")
    assert out["status"] == "running"
    started = [e for e in await db.get_events("pr-start")
               if e["event_type"] == "sim.started"]
    assert len(started) == 1
    assert started[0]["turn"] == 0


async def test_prepare_is_idempotent(db):
    """A `Retry` on the state must not re-emit sim.started or re-pay for embeddings.

    Re-embedding a corpus is the only genuinely expensive mistake available in this
    phase, so the guard is on the run's own event log rather than on hope.
    """
    await _make_run(db, "pr-idem", max_messages=3)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.prepare_run(db, "pr-idem")
        before = await db.get_events("pr-idem")
        await orchestration.prepare_run(db, "pr-idem")
    after = await db.get_events("pr-idem")
    assert len(after) == len(before), "prepare ran twice and wrote more events"
    started = [e for e in after if e["event_type"] == "sim.started"]
    assert len(started) == 1


async def test_prepare_then_slices_produce_one_ordered_event_log(db):
    """The seq must stay monotonic across the prepare/turn boundary.

    `get_events_after(seq)` relies on it — a client resuming from the highest seq it
    has seen would skip events in a log where seq restarted per state.
    """
    await _make_run(db, "pr-seq", max_messages=2)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.prepare_run(db, "pr-seq")
        await orchestration.execute_slice(db, "pr-seq", turn=0, turn_budget=1)
        await orchestration.execute_slice(db, "pr-seq", turn=1, turn_budget=1)
    seqs = [e["seq"] for e in await db.get_events("pr-seq")]
    assert seqs == sorted(seqs), f"event seqs are out of order: {seqs}"
    assert len(seqs) == len(set(seqs)), f"duplicate seqs across states: {seqs}"


async def test_a_whole_run_completes_across_many_slices(db):
    """The phase's headline acceptance criterion, in miniature.

    Ten turns, one slice each — which is the shape a 40-turn run has and the thing a
    single Lambda cannot do. Asserted on the transcript rather than the status, since
    a status can be right while the conversation was lost.
    """
    await _make_run(db, "pr-full", max_messages=10)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        payload = await orchestration.prepare_run(db, "pr-full")
        guard = 0
        while not payload["done"] and guard < 40:
            guard += 1
            payload = await orchestration.execute_slice(
                db, "pr-full", turn=payload["turn"], turn_budget=1
            )
        assert guard < 40, "the loop did not terminate"
        await orchestration.finalise(db, "pr-full")

    assert payload["status"] == "complete"
    assert payload["turn"] == 10
    snap = await db.get_snapshot("pr-full")
    assert len(snap.conversation) == 10, (
        f"expected 10 turns of transcript, got {len(snap.conversation)}"
    )
    assert snap.status == "complete"
    # Exactly one terminal event, at the end.
    terminals = [e for e in await db.get_events("pr-full")
                 if e["event_type"].startswith("sim.") and e["event_type"] != "sim.started"]
    assert [e["event_type"] for e in terminals] == ["sim.completed"]


# --------------------------------------------------------------------------- #
# The Lambda handlers
# --------------------------------------------------------------------------- #


async def test_handlers_refuse_an_event_with_no_owner(db):
    """A default owner here would attribute a conversation to a shared partition."""
    from matrix_studio import step_handlers

    for name in ("_prepare", "_turn", "_finalise"):
        fn = getattr(step_handlers, name)
        with pytest.raises(ValueError, match="no owner_sub"):
            await fn({"run_id": "x"})


async def test_the_turn_handler_returns_only_machine_sized_keys(db, monkeypatch):
    from matrix_studio import step_handlers
    from tests.support import TEST_OWNER

    await _make_run(db, "h-turn", max_messages=3)
    monkeypatch.setattr(step_handlers, "_bound", lambda _owner: _identity(db))
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        out = await step_handlers._turn(
            {"run_id": "h-turn", "owner_sub": TEST_OWNER, "turn": 0}
        )
    assert set(out) == {
        "run_id", "owner_sub", "turn", "max_messages", "total_cost_usd",
        "status", "done",
    }
    assert out["turn"] == 1 and out["status"] == "running"


async def _identity(db):
    return db


async def test_finalise_generates_the_summary_once(db):
    """`RunManager._runner` used to do this in a background task Lambda never runs.

    Guarded on an existing summary because `maybe_autogenerate_summary` stores a new
    row every call, so a Retry on the Finalise state would pay for a second LLM
    summary and leave two with no way to tell which is current.
    """
    await _make_run(db, "fin-sum", max_messages=1)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.execute_slice(db, "fin-sum", turn=0, turn_budget=1)
    assert (await db.get_run("fin-sum"))["status"] == "complete"

    calls = {"n": 0}

    async def fake_summary(_db, _run_id):
        calls["n"] += 1
        await _db.save_summary(
            run_id=_run_id, payload={"headline": "done"}, kind="generated",
            tokens_in=1, tokens_out=1, cost_usd=0.0,
        )

    with patch("matrix_studio.service.maybe_autogenerate_summary", side_effect=fake_summary):
        await orchestration.finalise(db, "fin-sum")
        await orchestration.finalise(db, "fin-sum")
    assert calls["n"] == 1, f"the summary was generated {calls['n']} times"
    assert len(await db.get_summaries("fin-sum")) == 1


async def test_finalise_does_not_summarise_a_stopped_run(db):
    """A summary of a cut-off conversation would describe it as though it finished."""
    await _make_run(db, "fin-stop", max_messages=6)
    await db.set_stop_requested("fin-stop")
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        out = await orchestration.execute_slice(db, "fin-stop", turn=0, turn_budget=1)
    assert out["status"] == "stopped"
    with patch("matrix_studio.service.maybe_autogenerate_summary") as gen:
        await orchestration.finalise(db, "fin-stop")
    gen.assert_not_called()


# --------------------------------------------------------------------------- #
# POST /api/runs under an orchestrator
# --------------------------------------------------------------------------- #


async def test_create_run_writes_the_row_synchronously_when_orchestrated(
    db, monkeypatch
):
    """The exact Phase 4 failure, guarded.

    On the Phase 1 deployment this route returned 201 with a real generated codename
    and then the run vanished: the row was written by a background task, and Lambda
    freezes the sandbox when the handler returns. The client held a 201 for a run that
    did not exist — nothing to poll, and not even a broken run to resume.

    So: the row must be readable the instant the call returns, with no waiting.
    """
    from matrix_studio.api.manager import RunManager
    from tests.support import TEST_OWNER

    monkeypatch.setenv("TURN_LOOP_ARN", "arn:aws:states:us-east-1:1:stateMachine:sm")
    started = {}

    async def fake_start(run_id, owner_sub, *, max_messages):
        started.update(run_id=run_id, owner_sub=owner_sub, max_messages=max_messages)
        return "arn:aws:states:us-east-1:1:execution:sm:run"

    monkeypatch.setattr(orchestration, "start_execution", fake_start)
    manager = RunManager(db)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        with patch("matrix_studio.naming.generate_run_name", return_value={
            "name": "quiet-harbour", "description": "d", "slug": "quiet-harbour",
            "source": "llm",
        }):
            out = await manager.create_run(
                {"topic": "whether to ship", "cast": CAST,
                 "config": {"max_messages": 7, "generate_avatars": False}},
                owner_sub=TEST_OWNER,
            )

    assert out["status"] == "pending"
    row = await db.get_run(out["run_id"])
    assert row is not None, "the run row was not written synchronously"
    assert row["status"] == "pending"
    assert row["topic"] == "whether to ship"
    # And the execution was started, with the budget it needs to know.
    assert started["run_id"] == out["run_id"]
    assert started["owner_sub"] == TEST_OWNER
    assert started["max_messages"] == 7


async def test_create_run_starts_no_execution_without_an_orchestrator(db, monkeypatch):
    """Local dev keeps the background-task path: uvicorn is one long-lived process."""
    from matrix_studio.api.manager import RunManager
    from tests.support import TEST_OWNER

    monkeypatch.delenv("TURN_LOOP_ARN", raising=False)
    calls = []
    monkeypatch.setattr(
        orchestration, "start_execution",
        lambda *a, **k: calls.append(a) or None,
    )
    manager = RunManager(db)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        with patch("matrix_studio.naming.generate_run_name", return_value={
            "name": "still-water", "description": "d", "slug": "still-water",
            "source": "llm",
        }):
            out = await manager.create_run(
                {"topic": "t", "cast": CAST,
                 "config": {"max_messages": 1, "generate_avatars": False}},
                owner_sub=TEST_OWNER,
            )
    assert out["status"] == "running"
    assert calls == [], "an execution was started with no state machine configured"
    await manager.shutdown()


async def test_start_execution_names_the_execution_after_the_run(db, monkeypatch):
    """Two executions on one run id would both append to the same event log.

    Deriving the name from the run id makes Step Functions itself refuse the second,
    which is the only place that check can be race-free.
    """
    monkeypatch.setenv("TURN_LOOP_ARN", "arn:aws:states:us-east-1:1:stateMachine:sm")
    seen = {}

    class FakeSfn:
        def start_execution(self, **kwargs):
            seen.update(kwargs)
            return {"executionArn": "arn:exec"}

    monkeypatch.setattr("boto3.client", lambda *a, **k: FakeSfn())
    arn = await orchestration.start_execution("abc-123", "user-1", max_messages=5)
    assert arn == "arn:exec"
    assert seen["name"] == "run-abc-123"
    payload = json.loads(seen["input"])
    assert payload == {
        "run_id": "abc-123", "owner_sub": "user-1", "turn": 0,
        "max_messages": 5, "total_cost_usd": 0.0, "mode": "fresh",
    }


async def test_a_resume_does_not_reuse_the_run_derived_execution_name(db, monkeypatch):
    """Execution names are unique for 90 days, so reuse would silently do nothing.

    `ExecutionAlreadyExists` is treated as success — it means the run is already
    executing — so a resume named after the run would return None, generate no turns,
    and leave the run at `running` for ever with no execution behind it.

    Safe for a resume specifically because the concurrency guard is the run's own
    status, which `resume_run` checks and flips synchronously before starting.
    """
    monkeypatch.setenv("TURN_LOOP_ARN", "arn:aws:states:us-east-1:1:stateMachine:sm")
    names = []

    class FakeSfn:
        def start_execution(self, **kwargs):
            names.append(kwargs["name"])
            return {"executionArn": "arn:exec"}

    monkeypatch.setattr("boto3.client", lambda *a, **k: FakeSfn())
    await orchestration.start_execution("abc-123", "u", max_messages=0, mode="resume")
    await orchestration.start_execution("abc-123", "u", max_messages=0, mode="resume")
    assert all(n != "run-abc-123" for n in names), names
    assert all(n.startswith("resume-abc-123-") for n in names), names


async def test_a_branch_keeps_the_run_derived_execution_name(db, monkeypatch):
    """A branch has a fresh run id, so the name is unique AND still guards a retry."""
    monkeypatch.setenv("TURN_LOOP_ARN", "arn:aws:states:us-east-1:1:stateMachine:sm")
    seen = {}

    class FakeSfn:
        def start_execution(self, **kwargs):
            seen.update(kwargs)
            return {"executionArn": "arn:exec"}

    monkeypatch.setattr("boto3.client", lambda *a, **k: FakeSfn())
    await orchestration.start_execution(
        "branch-9", "u", max_messages=8, mode="branch",
        extra={"parent_run_id": "p1", "from_turn": 3},
    )
    assert seen["name"] == "run-branch-9"
    payload = json.loads(seen["input"])
    assert payload["mode"] == "branch"
    assert payload["extra"] == {"parent_run_id": "p1", "from_turn": 3}


async def test_a_duplicate_execution_is_not_an_error(db, monkeypatch):
    """It means the run is already executing, which is the desired end state."""
    from botocore.exceptions import ClientError

    monkeypatch.setenv("TURN_LOOP_ARN", "arn:aws:states:us-east-1:1:stateMachine:sm")

    class FakeSfn:
        def start_execution(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ExecutionAlreadyExists", "Message": "x"}},
                "StartExecution",
            )

    monkeypatch.setattr("boto3.client", lambda *a, **k: FakeSfn())
    assert await orchestration.start_execution("r", "u", max_messages=1) is None


async def test_other_start_execution_errors_do_raise(db, monkeypatch):
    """A missing state machine or a denied call must not look like success —
    the run would sit at `pending` with nothing explaining why."""
    from botocore.exceptions import ClientError

    monkeypatch.setenv("TURN_LOOP_ARN", "arn:aws:states:us-east-1:1:stateMachine:sm")

    class FakeSfn:
        def start_execution(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                "StartExecution",
            )

    monkeypatch.setattr("boto3.client", lambda *a, **k: FakeSfn())
    with pytest.raises(ClientError):
        await orchestration.start_execution("r", "u", max_messages=1)


async def test_the_stop_route_works_with_no_live_task_in_this_process(db, monkeypatch):
    """The deployed case: the turn is generated by a worker Lambda.

    `request_stop`'s in-memory `self._tasks` check would refuse every stop on the
    deployed system while working perfectly on a laptop — the worst shape a bug can
    have, since local testing confirms it.
    """
    from matrix_studio.api.manager import RunManager

    await _make_run(db, "st-remote", max_messages=5, status="running")
    manager = RunManager(db)
    run = await db.get_run("st-remote")
    assert "st-remote" not in manager._tasks, "fixture must have no local task"

    out = await manager.request_stop_durable(run)
    assert out["stop_requested"] is True
    assert (await db.get_run("st-remote"))["stop_requested"] is True


async def test_a_stop_requested_during_a_turn_ends_the_run_at_that_turn(db):
    """The documented contract: the turn in flight finishes, the NEXT is prevented.

    The engine's `should_stop` closes over the run row read at the slice's start, so a
    stop arriving mid-turn is invisible to it — the slice returns `running`, the state
    machine loops, and one more turn is generated. Measured on the deployed stack:
    asking during turn 3 produced a log ending at turn 4.

    Simulated by setting the flag from inside the LLM stub, i.e. while the turn is
    being generated, which is the only moment that reproduces it.
    """
    await _make_run(db, "sl-midstop", max_messages=10)

    set_during = {"done": False}

    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Ada", "reason": "her turn"}))
        if "consistency validator" in text:
            return _Resp(json.dumps({"violation": False}))
        if not set_during["done"]:
            set_during["done"] = True
            # Mid-turn, exactly like an operator clicking stop while a turn runs.
            import asyncio as _a
            _a.get_event_loop().create_task(db.set_stop_requested("sl-midstop"))
        return _Resp("A short contribution.")

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        out = await orchestration.execute_slice(db, "sl-midstop", turn=0, turn_budget=1)
        # The state machine would loop while the status is `running`. Under the bug it
        # does exactly that and generates a second turn, so the loop is reproduced here
        # rather than asserted away.
        while out["status"] == "running":
            out = await orchestration.execute_slice(
                db, "sl-midstop", turn=out["turn"], turn_budget=1
            )

    assert out["status"] == "stopped", out
    assert (await db.get_run("sl-midstop"))["status"] == "stopped"
    # THE assertion. Both the fixed and the broken version end up `stopped`; they
    # differ only in how many turns were generated first, so a status check alone is
    # vacuous — it passed with the re-read removed.
    responses = [
        e for e in await db.get_events("sl-midstop")
        if e["event_type"] == "agent.response"
    ]
    assert len(responses) == 1, (
        f"the stop arrived during turn 1, so the run must end at turn 1; "
        f"{len(responses)} turns were generated"
    )
    # And the log carries the marker, not just the row: replay and export read the
    # log, so a run with no terminal event replays as one still going.
    assert [
        e["event_type"] for e in await db.get_events("sl-midstop")
        if e["event_type"].startswith("sim.") and e["event_type"] != "sim.started"
    ] == ["sim.stopped"]


async def test_stop_now_writes_the_terminal_event_and_snapshot(db):
    """`finalise` writes a status but no event; a log with no terminal marker
    replays as a run that is still going."""
    await _make_run(db, "sl-stopnow", max_messages=5)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.execute_slice(db, "sl-stopnow", turn=0, turn_budget=1)
    out = await orchestration.stop_now(db, "sl-stopnow", turn=1)
    assert out["status"] == "stopped"
    snap = await db.get_snapshot("sl-stopnow", 1)
    assert snap.status == "stopped"
    assert len(snap.conversation) == 1
    types = [e["event_type"] for e in await db.get_events("sl-stopnow")]
    assert types.count("sim.stopped") == 1
    seqs = [e["seq"] for e in await db.get_events("sl-stopnow")]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)), seqs


# --------------------------------------------------------------------------- #
# Branch and resume on the shared loop. §6 says they "need no new machinery"
# because both already reconstruct-and-generate-forward; only the first state
# differs. These check that claim rather than trusting it.
# --------------------------------------------------------------------------- #


async def test_a_branch_prepares_and_then_runs_on_the_shared_loop(db):
    from matrix_studio import branching

    await _make_run(db, "br-parent", max_messages=3)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.prepare_run(db, "br-parent")
        for t in range(3):
            await orchestration.execute_slice(db, "br-parent", turn=t, turn_budget=1)

        parent = await db.get_run("br-parent")
        meta = await branching.create_branch_run(db, parent, from_turn=2)
        branch_id = meta["run_id"]

        prepared = await orchestration.prepare(
            db, branch_id, mode="branch",
            parent_run_id="br-parent", from_turn=2,
        )
        assert prepared["turn"] == 2
        # The parent's log was copied, so the branch replays identically to the fork.
        copied = [
            e for e in await db.get_events(branch_id)
            if e["event_type"] == "agent.response"
        ]
        assert len(copied) == 2, f"expected the parent's first 2 turns, got {len(copied)}"
        fork = await db.get_snapshot(branch_id, 2)
        assert fork is not None and len(fork.conversation) == 2

        payload = prepared
        guard = 0
        while not payload["done"] and guard < 20:
            guard += 1
            payload = await orchestration.execute_slice(
                db, branch_id, turn=payload["turn"], turn_budget=1
            )

    assert payload["status"] == "complete", payload
    branch = await db.get_run(branch_id)
    assert branch["status"] == "complete"
    # The branch generated forward from the fork rather than restarting.
    snap = await db.get_snapshot(branch_id)
    assert len(snap.conversation) == payload["turn"] >= 3
    # And the PARENT is untouched — the immutability invariant.
    assert (await db.get_run("br-parent"))["status"] == "complete"
    assert len(
        [e for e in await db.get_events("br-parent") if e["event_type"] == "agent.response"]
    ) == 3


async def test_branch_prepare_is_idempotent_where_it_actually_matters(db):
    """A Retry on the prepare state must not double the branch's DOCUMENTS or mutation.

    Counting events proves nothing: `copy_events_upto` preserves turn and seq, so
    re-copying overwrites the same sort keys. The first version of this test counted
    events and stayed green with the guard removed.

    Documents are the sharp edge — `copy_documents_to_run` mints a NEW id and a new S3
    object per copy, on purpose, so a second prepare gives the branch two of everything
    and its personas retrieve every passage twice.
    """
    from matrix_studio import branching

    await db.create_run(
        run_id="br-idem", topic="t", cast=CAST,
        config={"max_messages": 2, "generate_avatars": False,
                "retrieval": {"enabled": True, "k": 2, "max_chars": 500, "mode": "fts"}},
    )
    await db.add_document(
        run_id="br-idem", title="brief.md",
        chunks=["egress inspection is the evidence the auditor wants"],
        text="egress inspection is the evidence the auditor wants",
    )
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.prepare_run(db, "br-idem")
        for t in range(2):
            await orchestration.execute_slice(db, "br-idem", turn=t, turn_budget=1)
        parent = await db.get_run("br-idem")
        meta = await branching.create_branch_run(db, parent, from_turn=1)
        bid = meta["run_id"]
        await orchestration.prepare(db, bid, mode="branch",
                                   parent_run_id="br-idem", from_turn=1)
        docs_after_first = len(await db.list_documents(bid))
        events_after_first = len(await db.get_events(bid))
        await orchestration.prepare(db, bid, mode="branch",
                                    parent_run_id="br-idem", from_turn=1)

    assert docs_after_first == 1, f"the branch should hold one document, got {docs_after_first}"
    assert len(await db.list_documents(bid)) == docs_after_first, (
        "the second prepare copied the parent's documents again, so the branch now "
        "holds duplicates with different ids"
    )
    assert len(await db.get_events(bid)) == events_after_first


async def test_a_resume_prepares_with_an_extended_budget(db):
    """`branch_budget` extends the budget when the checkpoint is at or past it.

    That number used to be a local variable. Under the machine the next turn is a
    different Lambda reading the run row, so an unpersisted budget means the slice sees
    `turn >= max_messages`, finalises immediately, and the resume generates nothing
    while reporting `complete`.
    """
    await _make_run(db, "rs-budget", max_messages=2)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.prepare_run(db, "rs-budget")
        for t in range(2):
            await orchestration.execute_slice(db, "rs-budget", turn=t, turn_budget=1)
    # The run is at its budget. Mark it interrupted, as a died-mid-run would be.
    await db.update_run_status("rs-budget", "interrupted")

    prepared = await orchestration.prepare_resume(db, "rs-budget")
    assert prepared["turn"] == 2
    assert prepared["max_messages"] > 2, (
        f"the resume budget was not extended: {prepared['max_messages']}"
    )
    run = await db.get_run("rs-budget")
    assert int(run["budget"]) == prepared["max_messages"], "the budget was not persisted"
    assert run["status"] == "running"

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        out = await orchestration.execute_slice(db, "rs-budget", turn=2, turn_budget=1)
    assert out["turn"] == 3, "the resume generated no new turn"
    assert out["status"] == "running"


async def test_budget_of_prefers_the_row_over_the_config(db):
    assert orchestration.budget_of(
        {"budget": 12, "config_json": json.dumps({"max_messages": 4})}
    ) == 12
    assert orchestration.budget_of(
        {"config_json": json.dumps({"max_messages": 4})}
    ) == 4
    # Neither present falls back to the settings default rather than to zero, which
    # would finalise every run before its first turn.
    assert orchestration.budget_of({}) > 0


async def test_a_resume_clears_a_previous_stop(db):
    """A run stopped once would otherwise stop one turn into every later resume."""
    await _make_run(db, "rs-stop", max_messages=6)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.prepare_run(db, "rs-stop")
        await db.set_stop_requested("rs-stop")
        out = await orchestration.execute_slice(db, "rs-stop", turn=0, turn_budget=1)
    assert out["status"] == "stopped"
    assert (await db.get_run("rs-stop"))["stop_requested"] is True

    await orchestration.prepare_resume(db, "rs-stop")
    assert (await db.get_run("rs-stop"))["stop_requested"] is False
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        out = await orchestration.execute_slice(db, "rs-stop", turn=1, turn_budget=1)
    assert out["status"] == "running", "the resume stopped again on its first turn"


async def test_prepare_rejects_an_unknown_mode(db):
    """Loud, because a typo'd mode would otherwise silently run the fresh path and
    re-emit sim.started onto a branch."""
    await _make_run(db, "pm-bad", max_messages=1)
    with pytest.raises(ValueError, match="unknown prepare mode"):
        await orchestration.prepare(db, "pm-bad", mode="branchh")


async def test_the_branch_route_starts_an_execution_when_orchestrated(db, monkeypatch):
    """The branch's row was always written synchronously, so it never showed the
    Phase 4 symptom — it was created, named, and then never generated a turn."""
    from matrix_studio.api.manager import RunManager
    from tests.support import TEST_OWNER

    await _make_run(db, "br-route", max_messages=3)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.prepare_run(db, "br-route")
        await orchestration.execute_slice(db, "br-route", turn=0, turn_budget=1)

    monkeypatch.setenv("TURN_LOOP_ARN", "arn:aws:states:us-east-1:1:stateMachine:sm")
    started = {}

    async def fake_start(run_id, owner_sub, *, max_messages, mode="fresh", extra=None):
        started.update(run_id=run_id, mode=mode, extra=extra or {})
        return "arn:exec"

    monkeypatch.setattr(orchestration, "start_execution", fake_start)
    manager = RunManager(db)
    parent = await db.get_run("br-route")
    parent["owner_sub"] = TEST_OWNER
    with patch("matrix_studio.naming.generate_run_name", return_value={
        "name": "forked-path", "description": "d", "slug": "forked-path", "source": "llm",
    }):
        meta = await manager.create_branch(parent, from_turn=1)

    assert started["mode"] == "branch"
    assert started["run_id"] == meta["run_id"]
    assert started["extra"]["parent_run_id"] == "br-route"
    assert started["extra"]["from_turn"] == 1
    # No background task was created — that path is the local one.
    assert meta["run_id"] not in manager._tasks


async def test_the_resume_route_starts_an_execution_when_orchestrated(db, monkeypatch):
    from matrix_studio.api.manager import RunManager

    await _make_run(db, "rs-route", max_messages=4, status="interrupted")
    monkeypatch.setenv("TURN_LOOP_ARN", "arn:aws:states:us-east-1:1:stateMachine:sm")
    started = {}

    async def fake_start(run_id, owner_sub, *, max_messages, mode="fresh", extra=None):
        started.update(run_id=run_id, mode=mode)
        return "arn:exec"

    monkeypatch.setattr(orchestration, "start_execution", fake_start)
    manager = RunManager(db)
    out = await manager.resume_run(await db.get_run("rs-route"))
    assert out["status"] == "running"
    assert started == {"run_id": "rs-route", "mode": "resume"}
    # The status flip stays synchronous: a duplicate resume must be refused by the row.
    assert (await db.get_run("rs-route"))["status"] == "running"
    assert "rs-route" not in manager._tasks


# --------------------------------------------------------------------------- #
# A resumed or forked run must not carry a log that says it is finished.
#
# Found on the deployed stack, not here: resuming a run whose log already ended
# produced TWO `sim.completed` events. `truncate_after_turn` removes events PAST
# the checkpoint, and a terminal event sits AT the last turn, so it survived by
# one. Nothing raised, because `reconstruct_at_turn` ignores `sim.*` — but the
# viewer marks a run done on the first terminal event and stops polling, so the
# resumed turns never appear.
# --------------------------------------------------------------------------- #


async def test_a_resumed_run_has_exactly_one_terminal_event(db):
    """Reachable through the UI: stop a run, then resume it."""
    await _make_run(db, "rs-term", max_messages=2)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.prepare_run(db, "rs-term")
        for t in range(2):
            await orchestration.execute_slice(db, "rs-term", turn=t, turn_budget=1)
    assert (await db.get_run("rs-term"))["status"] == "complete"
    assert _terminals(await db.get_events("rs-term")) == ["sim.completed"]

    await db.update_run_status("rs-term", "interrupted")
    prepared = await orchestration.prepare_resume(db, "rs-term")
    assert _terminals(await db.get_events("rs-term")) == [], (
        "the old terminal event survived the resume"
    )

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        payload = prepared
        guard = 0
        while not payload["done"] and guard < 20:
            guard += 1
            payload = await orchestration.execute_slice(
                db, "rs-term", turn=payload["turn"], turn_budget=1
            )
    assert _terminals(await db.get_events("rs-term")) == ["sim.completed"], (
        "the resumed run's log claims to have finished twice"
    )


async def test_a_stopped_then_resumed_run_has_one_terminal_event(db):
    """The path a user actually takes, rather than a contrived interrupt."""
    await _make_run(db, "rs-sr", max_messages=6)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.prepare_run(db, "rs-sr")
        await db.set_stop_requested("rs-sr")
        out = await orchestration.execute_slice(db, "rs-sr", turn=0, turn_budget=1)
    assert out["status"] == "stopped"
    assert _terminals(await db.get_events("rs-sr")) == ["sim.stopped"]

    prepared = await orchestration.prepare_resume(db, "rs-sr")
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        payload = prepared
        guard = 0
        while not payload["done"] and guard < 20:
            guard += 1
            payload = await orchestration.execute_slice(
                db, "rs-sr", turn=payload["turn"], turn_budget=1
            )
    assert _terminals(await db.get_events("rs-sr")) == ["sim.completed"], (
        "a stopped-then-resumed run kept its sim.stopped alongside the new completion"
    )


async def test_a_branch_forked_at_the_parents_last_turn_drops_the_inherited_marker(db):
    """`copy_events_upto(last_turn)` brings the parent's terminal event with it."""
    from matrix_studio import branching

    await _make_run(db, "br-term", max_messages=2)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.prepare_run(db, "br-term")
        for t in range(2):
            await orchestration.execute_slice(db, "br-term", turn=t, turn_budget=1)
        parent = await db.get_run("br-term")
        # Fork at the parent's LAST turn, which is where the marker gets copied.
        meta = await branching.create_branch_run(db, parent, from_turn=2)
        bid = meta["run_id"]
        prepared = await orchestration.prepare(
            db, bid, mode="branch", parent_run_id="br-term", from_turn=2
        )
        assert _terminals(await db.get_events(bid)) == [], (
            "the branch inherited the parent's completion marker"
        )
        payload = prepared
        guard = 0
        while not payload["done"] and guard < 20:
            guard += 1
            payload = await orchestration.execute_slice(
                db, bid, turn=payload["turn"], turn_budget=1
            )
    assert _terminals(await db.get_events(bid)) == ["sim.completed"]
    # The parent keeps its own marker — clearing is per-run, not shared.
    assert _terminals(await db.get_events("br-term")) == ["sim.completed"]


async def test_clear_terminal_events_leaves_everything_else_alone(db):
    """It must not take the conversation with it."""
    await _make_run(db, "ct-safe", max_messages=2)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_llm):
        await orchestration.prepare_run(db, "ct-safe")
        for t in range(2):
            await orchestration.execute_slice(db, "ct-safe", turn=t, turn_budget=1)
    before = await db.get_events("ct-safe")
    n = await db.clear_terminal_events("ct-safe")
    after = await db.get_events("ct-safe")
    assert n == 1
    assert len(after) == len(before) - 1
    assert [e["event_type"] for e in after if e["event_type"] == "agent.response"] == \
        [e["event_type"] for e in before if e["event_type"] == "agent.response"]
    assert any(e["event_type"] == "sim.started" for e in after), (
        "sim.started is not terminal and must survive"
    )


def _terminals(events):
    return [
        e["event_type"] for e in events
        if e["event_type"].startswith("sim.") and e["event_type"] != "sim.started"
    ]


# --------------------------------------------------------------------------- #
# The status vocabulary is duplicated across two languages, and drift between
# the copies has already caused two bugs. This pins them together.
# --------------------------------------------------------------------------- #


def _ts_string_list(source: str, name: str) -> set:
    """Extract a `const NAME = [...]` string array from the TypeScript source.

    Parsed rather than substring-searched. A substring check ("is 'sim.stopped' in the
    file") is satisfied by the name appearing in a comment, which is exactly how the
    fixture-vs-stack test in infra/ was once too weak to notice a mismatch.
    """
    import re

    m = re.search(rf"{name}\s*=\s*(?:new Set\()?\[(.*?)\]", source, re.DOTALL)
    assert m, f"could not find {name} in runStatus.ts"
    return set(re.findall(r"'([^']+)'", m.group(1)))


def _run_status_ts() -> str:
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent / "frontend" / "src" / "lib" / "runStatus.ts"
    )
    assert path.exists(), f"{path} is missing"
    return path.read_text()


async def test_the_frontend_agrees_on_terminal_statuses():
    """`orchestration.TERMINAL_STATUSES` vs the SPA's list.

    One side deciding a run is finished while the other does not is a stream that never
    ends or a button that never appears. Both have happened.
    """
    assert _ts_string_list(_run_status_ts(), "TERMINAL_STATUSES") == set(
        orchestration.TERMINAL_STATUSES
    )


async def test_the_frontend_agrees_on_terminal_events():
    """`storage.dynamo.TERMINAL_EVENT_TYPES` and `api/manager.TERMINAL_EVENTS`."""
    from matrix_studio.api.manager import TERMINAL_EVENTS as MANAGER_EVENTS
    from matrix_studio.storage.dynamo import TERMINAL_EVENT_TYPES

    ts = _ts_string_list(_run_status_ts(), "TERMINAL_EVENTS")
    assert ts == set(TERMINAL_EVENT_TYPES), f"SPA {ts} vs storage {set(TERMINAL_EVENT_TYPES)}"
    # And the two PYTHON copies agree with each other, which nothing checked either.
    assert set(MANAGER_EVENTS) == set(TERMINAL_EVENT_TYPES), (
        f"manager {set(MANAGER_EVENTS)} vs storage {set(TERMINAL_EVENT_TYPES)}"
    )


async def test_the_frontend_agrees_on_resumable_statuses():
    """`branching.RESUMABLE_STATUSES`. An extra entry offers a button the API 409s."""
    from matrix_studio.branching import RESUMABLE_STATUSES

    assert _ts_string_list(_run_status_ts(), "RESUMABLE_STATUSES") == set(
        RESUMABLE_STATUSES
    )


async def test_pending_is_live_on_both_sides():
    """A run is created `pending` and its first turn flips it to `running`.

    So `pending` must be absent from the terminal set on both sides — a `pending` run
    treated as terminal would show a finished run that had never spoken.
    """
    assert "pending" not in orchestration.TERMINAL_STATUSES
    assert "pending" in _ts_string_list(_run_status_ts(), "LIVE_STATUSES")
    assert "pending" not in _ts_string_list(_run_status_ts(), "TERMINAL_STATUSES")
