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
