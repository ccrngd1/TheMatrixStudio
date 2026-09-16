# SPDX-License-Identifier: Apache-2.0
"""
The closing round: one final round when a run hits its ceiling without finishing.

The simultaneous renewal run (`36231059`) stopped at round 8 of 8 with four personas all
answering the same question from Quinn — not because anything had finished, but because the
budget ran out. A closing round turns that cutoff into an ending.

**It asks for positions and terms, not agreement**, and that is the design decision worth
defending in tests. "Work toward a consensus" was the obvious wording: the Phase 6 dismissal
work measured the same sentence moving visible behaviour from 0.000 to 0.333 on framing alone,
and `distinct_positions` is the least stable metric in the harness — so an instruction to agree
would produce agreement every time, and nothing would separate a real resolution from a
manufactured one.

Three structural properties, each a test below:

1. it runs AFTER the ceiling (turn `max_messages + 1`), so it does not consume a round;
2. it runs only when the ceiling was REACHED — never after a convergence, which has already
   established that nobody had anything to add;
3. it is simultaneous in both methods, so a moderated run gets it too.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio.engine import run_simulation
from matrix_studio.engine.simulator import _CLOSING
from matrix_studio.state import SelectionConfig

pytestmark = pytest.mark.asyncio

CAST = [
    {"name": "Ada", "persona": "ethicist", "goals": ["seek truth"]},
    {"name": "Ben", "persona": "engineer", "goals": ["ship safely"]},
    {"name": "Cy", "persona": "lawyer", "goals": ["stay legal"]},
]
WHO = {"ethicist": "Ada", "engineer": "Ben", "lawyer": "Cy"}


class _Resp:
    def __init__(self, content):
        self.choices = [
            MagicMock(message=MagicMock(content=content), finish_reason="stop")
        ]
        self.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        self._hidden_params = {"response_cost": 0.001}


def _say(t, **extra):
    return json.dumps({"utterance": t, "rationale": "w", "goal_served": "none", **extra})


PASS = json.dumps({"utterance": "", "rationale": "n", "goal_served": "none", "pass": True})


def _engine(script=None, moderated=False):
    """Records every voice prompt, so a test can ask what each persona was told."""
    script = dict(script or {})
    prompts: list[tuple[str, str]] = []

    def fake(*_a, **kw):
        text = " ".join(m["content"] for m in kw["messages"])
        if "conversation moderator" in text:
            assert moderated, "no moderator in simultaneous mode"
            return _Resp(json.dumps({"speaker": "Ada", "reason": "r"}))
        who = next((n for k, n in WHO.items() if k in text), None)
        if who is None:
            return _Resp("two-words")
        prompts.append((who, text))
        replies = script.get(who) or []
        return _Resp(replies.pop(0) if replies else _say(f"{who} speaks"))

    fake.prompts = prompts
    return fake


async def _run(db, run_id, *, rounds=2, method="simultaneous", closing=True, script=None):
    fake = _engine(script, moderated=(method == "moderated"))
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        result = await run_simulation({
            "topic": "AI ethics", "cast": CAST,
            "config": {
                "max_messages": rounds, "generate_avatars": False,
                "cognition": {"enabled": True},
                "selection": {"method": method, "closing_round": closing},
            },
        }, db=db, run_id=run_id)
    return result, fake


async def _responses(db, run_id):
    out = []
    for r in await db.get_events(run_id):
        if r["event_type"] != "agent.response":
            continue
        p = r["payload"]
        out.append({"turn": r["turn"], "agent": r.get("agent_name"),
                    **(json.loads(p) if isinstance(p, str) else p)})
    return out


# --------------------------------------------------------------------------- #
# (1) it is an extra round, not one of the run's
# --------------------------------------------------------------------------- #


async def test_the_closing_round_happens_after_the_ceiling(db):
    result, _ = await _run(db, "after", rounds=2)
    turns = [r["turn"] for r in await _responses(db, "after")]
    assert turns == [1, 1, 1, 2, 2, 2, 3, 3, 3], turns
    assert result["total_turns"] == 3, "the ceiling was 2 rounds plus the closing one"


async def test_the_closing_turns_are_marked_as_such(db):
    """A final position stated under the closing instruction is a different artefact from a
    turn mid-argument, and the summary layer should be able to tell them apart."""
    await _run(db, "marked", rounds=2)
    rs = await _responses(db, "marked")
    assert all("closing" not in r for r in rs if r["turn"] < 3)
    assert all(r["closing"] is True for r in rs if r["turn"] == 3)


async def test_only_the_closing_round_gets_the_closing_instruction(db):
    _, fake = await _run(db, "instr", rounds=2)
    by_persona: dict[str, list[str]] = {}
    for who, text in fake.prompts:
        by_persona.setdefault(who, []).append(text)
    assert _CLOSING.strip() in by_persona["Ada"][-1]
    assert _CLOSING.strip() not in by_persona["Ada"][0]


def test_the_instruction_asks_for_terms_and_forbids_false_agreement():
    """The whole reason this wording was chosen over "work toward a consensus". Asserted on
    the constant so a later edit that reintroduces an agreement instruction fails here."""
    assert "cannot accept" in _CLOSING
    assert "can accept" in _CLOSING
    assert "Do not agree to something you do not agree with" in _CLOSING
    assert "consensus" not in _CLOSING.lower()


# --------------------------------------------------------------------------- #
# (2) only on the ceiling
# --------------------------------------------------------------------------- #


async def test_a_converged_run_gets_no_closing_round(db):
    """Convergence already established that nobody had anything to add. Asking again would
    contradict the finding the run just recorded."""
    result, _ = await _run(
        db, "converged", rounds=6,
        script={"Ada": [_say("one"), PASS], "Ben": [_say("two"), PASS],
                "Cy": [_say("three"), PASS]},
    )
    assert result["converged"]["reason"] == "every participant passed this round"
    assert result["total_turns"] == 1
    assert all("closing" not in r for r in await _responses(db, "converged"))


async def test_it_is_off_by_default(db):
    result, fake = await _run(db, "off", rounds=2, closing=False)
    assert result["total_turns"] == 2
    assert all(_CLOSING.strip() not in t for _, t in fake.prompts)


async def test_the_config_default_is_off(db):
    assert SelectionConfig().closing_round is False
    assert SelectionConfig.from_config(
        {"selection": {"closing_round": True}}
    ).closing_round is True


# --------------------------------------------------------------------------- #
# (3) both methods
# --------------------------------------------------------------------------- #


async def test_a_moderated_run_gets_a_simultaneous_closing_round(db):
    """The gap this also fixes: a moderated run that hits its cap today just stops. The
    closing round is concurrent in both methods — closing statements naturally are, and it
    means one implementation rather than a sequential variant nobody measured."""
    result, _ = await _run(db, "mod", rounds=3, method="moderated")
    rs = await _responses(db, "mod")
    # Three moderated turns, one per turn, then a closing round with all three at once.
    assert [r["turn"] for r in rs] == [1, 2, 3, 4, 4, 4]
    closing = [r for r in rs if r["turn"] == 4]
    assert {r["agent"] for r in closing} == {"Ada", "Ben", "Cy"}
    assert result["total_turns"] == 4


async def test_passing_is_still_allowed_in_the_closing_round(db):
    """A persona genuinely content has nothing to state, and forcing one produces exactly
    the "confirmed, nothing to add" filler §13 exists to remove."""
    await _run(db, "close-pass", rounds=1, script={"Ben": [_say("b1"), PASS]})
    rs = await _responses(db, "close-pass")
    closing = [r["agent"] for r in rs if r["turn"] == 2]
    assert closing == ["Ada", "Cy"], closing
    passes = [
        r for r in await db.get_events("close-pass") if r["event_type"] == "agent.passed"
    ]
    assert len(passes) == 1


async def test_a_closing_round_where_everybody_passes_is_not_a_convergence(db):
    """It is the last round either way. Recording it as a convergence would claim the run
    ended because the room was finished, when it ended because the budget was."""
    result, _ = await _run(
        db, "all-pass-close", rounds=1,
        script={"Ada": [_say("a"), PASS], "Ben": [_say("b"), PASS], "Cy": [_say("c"), PASS]},
    )
    assert "converged" not in result
    # The turn number KEEPS the empty closing round, because `turn > max_messages` is how
    # the next Lambda invocation knows the round already happened. Decrementing it made the
    # deployed run open a closing round for ever; `closing_round_empty` records the truth.
    assert result["total_turns"] == 2
    assert result["status"] == "complete"
    done = [e for e in await db.get_events("all-pass-close")
            if e["event_type"] == "sim.completed"][0]
    payload = done["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    assert payload["closing_round_empty"] is True


# --------------------------------------------------------------------------- #
# (4) the deployed shape — one round per Lambda invocation
# --------------------------------------------------------------------------- #


async def test_the_closing_round_fires_when_rounds_arrive_one_slice_at_a_time(db):
    """The regression test for a defect that shipped.

    Every test above drives `run_simulation`, which loops in one process. The deployed stack
    calls `execute_slice` with `turn_budget=1` — one ROUND per Lambda — and the slice that
    finishes the last round spends its budget and returns. `_run_turns` then asked "is
    `turn < max_messages`?", got no, and reported the run complete: **the closing round never
    opened.** A live run (`c055b500`) produced 8 rounds and zero closing statements while
    every in-process test passed.

    The fix is two-part and both halves are asserted here: a pending closing round makes the
    slice report `running`, and `turn > max_messages` is what tells the NEXT invocation the
    round has already happened — a flag in the function would reset on every slice, which is
    exactly how the decline streak went inert.
    """
    from matrix_studio import orchestration

    await db.create_run(
        run_id="slice-close", topic="AI ethics", cast=CAST,
        config={"max_messages": 2, "generate_avatars": False,
                "cognition": {"enabled": True},
                "selection": {"method": "simultaneous", "closing_round": True}},
    )
    turn, seen = 0, []
    for _ in range(6):  # generous; the run should finish on its own well before this
        fake = _engine()
        with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
            out = await orchestration.execute_slice(
                db, "slice-close", turn=turn, turn_budget=1
            )
        seen.append((out["turn"], out["status"]))
        turn = out["turn"]
        if out.get("done"):
            break

    rs = await _responses(db, "slice-close")
    # Two conversation rounds, then the closing round — nine turns of three personas.
    assert [r["turn"] for r in rs] == [1, 1, 1, 2, 2, 2, 3, 3, 3], seen
    assert [r["agent"] for r in rs if r["turn"] == 3] == ["Ada", "Ben", "Cy"]
    assert all(r["closing"] is True for r in rs if r["turn"] == 3)
    # The slice after the last conversation round must NOT have called the run complete.
    assert seen[1] == (2, "running"), seen
    assert seen[-1][1] == "complete", seen


async def test_a_finished_closing_round_does_not_open_another_one(db):
    """The other half. If doneness were a local flag it would reset on the next invocation
    and the run would generate closing rounds until the ceiling logic gave up — which is what
    the first version of this fix did when it decremented the turn for an empty round."""
    from matrix_studio import orchestration

    await db.create_run(
        run_id="slice-once", topic="AI ethics", cast=CAST,
        config={"max_messages": 1, "generate_avatars": False,
                "cognition": {"enabled": True},
                "selection": {"method": "simultaneous", "closing_round": True}},
    )
    turn, slices = 0, 0
    for _ in range(6):
        fake = _engine()
        with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
            out = await orchestration.execute_slice(
                db, "slice-once", turn=turn, turn_budget=1
            )
        slices += 1
        turn = out["turn"]
        if out.get("done"):
            break
    rs = await _responses(db, "slice-once")
    assert [r["turn"] for r in rs] == [1, 1, 1, 2, 2, 2]
    assert slices == 2, "one conversation round, one closing round, then done"


async def test_resuming_past_the_ceiling_does_not_open_a_second_closing_round(db):
    """Branching and resume call `resume_simulation` directly, so the orchestrator's guard
    does not apply — the engine has to know on its own that the closing round is done.

    It knows by deriving it: `turn > max_messages` can only be true because a closing round
    already ran. A mutant that made that a plain `False` passed every other test in this
    file, because `execute_slice` was catching it one layer up.
    """
    from matrix_studio.engine.simulator import resume_simulation

    fake = _engine()
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        result = await resume_simulation(
            run_id="resumed", topic="AI ethics",
            agents={c["name"]: __import__(
                "matrix_studio.state", fromlist=["AgentState"]
            ).AgentState(name=c["name"], persona=c["persona"], goals=c["goals"])
                for c in CAST},
            # A run of 1 round whose closing round already happened, so it sits at turn 2.
            conversation=[{"speaker": "Ada", "content": "a", "turn": 1},
                          {"speaker": "Ada", "content": "final", "turn": 2}],
            from_turn=2, start_seq=100, max_messages=1, db=db,
            cognition=__import__(
                "matrix_studio.state", fromlist=["CognitionConfig"]
            ).CognitionConfig(enabled=True),
            selection=SelectionConfig(method="simultaneous", closing_round=True),
        )
    assert fake.prompts == [], "nothing should have been generated"
    assert result["total_turns"] == 2
