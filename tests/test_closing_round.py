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

And one rule about what it collects: **nobody passes in it.** The round exists to get one
statement from everybody, and "nothing further, I'd sign it as written" is a statement — run
`8b4c59b6` lost two of six closing statements because the prose backstop read openings like
that as passes. Section (5) is that rule.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio.engine import run_simulation, simulator
from matrix_studio.engine.simulator import _CLOSING
from matrix_studio.settings import get_settings
from matrix_studio.state import AgentState, CognitionConfig, SelectionConfig

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


async def _run(db, run_id, *, rounds=2, method="simultaneous", closing=True, script=None,
               cognition=True, **selection):
    fake = _engine(script, moderated=(method in ("moderated", "hybrid")))
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        result = await run_simulation({
            "topic": "AI ethics", "cast": CAST,
            "config": {
                "max_messages": rounds, "generate_avatars": False,
                "cognition": {"enabled": cognition},
                "selection": {"method": method, "closing_round": closing, **selection},
            },
        }, db=db, run_id=run_id)
    return result, fake


async def _events(db, run_id, event_type):
    out = []
    for r in await db.get_events(run_id):
        if r["event_type"] != event_type:
            continue
        p = r["payload"]
        out.append({"turn": r["turn"], "agent": r.get("agent_name"),
                    **(json.loads(p) if isinstance(p, str) else p)})
    return out


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


async def test_a_closing_round_where_every_reply_is_empty_is_not_a_convergence(db):
    """It is the last round either way. Recording it as a convergence would claim the run
    ended because the room was finished, when it ended because the budget was.

    Nobody can pass in the closing round, so the only way it ends empty is every reply coming
    back with no words — each recorded as `closing.missing`, none as `agent.passed`."""
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
    missing = await _events(db, "all-pass-close", "closing.missing")
    assert [e["agent"] for e in missing] == ["Ada", "Ben", "Cy"]
    assert await _events(db, "all-pass-close", "agent.passed") == []


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


# --------------------------------------------------------------------------- #
# (5) nobody passes in the closing round
# --------------------------------------------------------------------------- #

#: The shape of both statements run `8b4c59b6` lost: an opening `_PROSE_PASS` matches, then a
#: final position. Neither had set the pass field.
NOTHING_FURTHER = "Nothing further from me — I'd sign it as written. The date is my one condition."


async def test_nothing_further_is_a_closing_statement_not_a_pass(db):
    """The regression. In an ordinary round "Nothing further" is what the prose backstop is
    for; in the closing round it ate two final positions of ~600 tokens each. "I'd sign it as
    written" is where the persona ended, and the round exists to collect exactly that."""
    await _run(db, "nf", rounds=1, script={"Ben": [_say("b1"), _say(NOTHING_FURTHER)]})
    closing = [r for r in await _responses(db, "nf") if r["turn"] == 2]
    assert [r["agent"] for r in closing] == ["Ada", "Ben", "Cy"]
    ben = next(r for r in closing if r["agent"] == "Ben")
    assert ben["message"] == NOTHING_FURTHER
    assert ben["closing"] is True
    assert await _events(db, "nf", "agent.passed") == []
    assert await _events(db, "nf", "closing.missing") == []


async def test_the_closing_prompt_offers_no_pass(db):
    """The pass instruction says to pass if "your position is already on the record and
    unchanged" — the very reply the closing round wants — and it sat just before `_CLOSING`.
    Each persona's round-1 prompt is checked too, so this cannot pass merely because the
    wording it looks for was edited."""
    _, fake = await _run(db, "no-offer", rounds=1)
    by_persona: dict[str, list[str]] = {}
    for who, text in fake.prompts:
        by_persona.setdefault(who, []).append(text)
    assert set(by_persona) == {"Ada", "Ben", "Cy"}
    for who, prompts in by_persona.items():
        opening, closing = prompts[0], prompts[-1]
        assert '"pass"' in opening and "Passing is not a failure" in opening, who
        assert _CLOSING.strip() in closing, who
        assert '"pass"' not in closing, who
        assert "Passing is not" not in closing, who


async def test_the_pass_field_does_not_drop_a_closing_statement_that_has_words(db):
    """The field is not offered in the closing round, but a model that set it in every earlier
    round may set it again out of habit. The words are what count."""
    text = "My position stands: I can accept the phased plan, not the deadline."
    await _run(db, "flag", rounds=1, script={"Cy": [_say("c1"), _say(text, **{"pass": True})]})
    closing = {r["agent"]: r for r in await _responses(db, "flag") if r["turn"] == 2}
    assert set(closing) == {"Ada", "Ben", "Cy"}
    assert closing["Cy"]["message"] == text
    assert await _events(db, "flag", "agent.passed") == []


async def test_an_empty_closing_reply_is_recorded_as_missing_not_as_a_pass(db):
    """What happens when a model returns no words anyway. Nothing goes in the transcript —
    there is nothing to put there, and the JSON object is not a statement — but it is not
    silent, and it is not a pass: nobody was offered one, so `agent.passed` would record a
    choice nobody made. `closing.missing` carries the cost (the call was paid for) and what
    the model actually sent."""
    result, _ = await _run(db, "empty", rounds=1, script={"Ben": [_say("b1"), PASS]})
    assert [r["agent"] for r in await _responses(db, "empty") if r["turn"] == 2] == ["Ada", "Cy"]
    assert all('"utterance"' not in m["content"] for m in result["conversation"])
    assert await _events(db, "empty", "agent.passed") == []
    [missing] = await _events(db, "empty", "closing.missing")
    assert (missing["agent"], missing["turn"], missing["round"]) == ("Ben", 2, 2)
    assert missing["cost_usd"] > 0
    assert missing["declared_pass"] is True
    assert '"pass": true' in missing["reply"]
    # Two statements were made, so the round was not empty, and the run ended as before.
    assert result["status"] == "complete" and result["total_turns"] == 2
    [done] = await _events(db, "empty", "sim.completed")
    assert "closing_round_empty" not in done


async def test_a_missing_last_statement_still_ends_the_closing_round_one_slice_at_a_time(db):
    """When the LAST persona in the queue comes back empty, the round ends on the
    missing-statement path instead of after a message. That path has to close the round the
    way a spoken one does, in the deployed shape too, or the run either never reports
    `complete` or opens a second closing round."""
    from matrix_studio import orchestration

    await db.create_run(
        run_id="slice-missing", topic="AI ethics", cast=CAST,
        config={"max_messages": 1, "generate_avatars": False,
                "cognition": {"enabled": True},
                "selection": {"method": "simultaneous", "closing_round": True}},
    )
    turn, seen = 0, []
    for i in range(6):
        fake = _engine({"Cy": [PASS]} if i == 1 else None)
        with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
            out = await orchestration.execute_slice(
                db, "slice-missing", turn=turn, turn_budget=1
            )
        seen.append((out["turn"], out["status"]))
        turn = out["turn"]
        if out.get("done"):
            break
    assert seen == [(1, "running"), (2, "complete")], seen
    rs = await _responses(db, "slice-missing")
    assert [(r["turn"], r["agent"]) for r in rs] == [
        (1, "Ada"), (1, "Ben"), (1, "Cy"), (2, "Ada"), (2, "Ben")]
    assert [e["agent"] for e in await _events(db, "slice-missing", "closing.missing")] == ["Cy"]


@pytest.mark.parametrize("method", ["simultaneous", "rotation", "hybrid"])
async def test_every_other_round_still_honours_passes_and_prose_passes(db, method):
    """The rule is about the ROUND, not the words. In the same run, the same reply is a pass
    in an ordinary round — by the field, and by the prose backstop — and a statement in the
    closing round. Hybrid's single opening round is its only round before the closing one."""
    run_id = f"other-{method}"
    await _run(
        db, run_id, rounds=1, method=method, hybrid_opening_rounds=1,
        script={"Ben": [_say(NOTHING_FURTHER), _say(NOTHING_FURTHER)],
                "Cy": [PASS, _say("c-final")]},
    )
    passes = await _events(db, run_id, "agent.passed")
    assert [(p["agent"], p["turn"]) for p in passes] == [("Ben", 1), ("Cy", 1)]
    rs = await _responses(db, run_id)
    assert [r["agent"] for r in rs if r["turn"] == 1] == ["Ada"]
    closing = {r["agent"]: r["message"] for r in rs if r["turn"] == 2}
    assert closing == {"Ada": "Ada speaks", "Ben": NOTHING_FURTHER, "Cy": "c-final"}


async def test_with_cognition_off_the_closing_round_skips_the_prose_backstop_too(db):
    """With cognition off there is no pass field, so the prose backstop is the only pass
    signal an ordinary round has — and the closing round must ignore it all the same. An
    empty plain-text reply is a missing statement there, exactly as a blank utterance is."""
    await _run(
        db, "plain", rounds=1, cognition=False,
        script={"Ben": [NOTHING_FURTHER, NOTHING_FURTHER], "Cy": ["c1", ""]},
    )
    assert [(p["agent"], p["turn"]) for p in await _events(db, "plain", "agent.passed")] == [
        ("Ben", 1)]
    closing = {r["agent"]: r["message"] for r in await _responses(db, "plain") if r["turn"] == 2}
    assert set(closing) == {"Ada", "Ben"}
    assert closing["Ben"] == NOTHING_FURTHER
    [missing] = await _events(db, "plain", "closing.missing")
    assert missing["agent"] == "Cy" and missing["declared_pass"] is False


async def test_closing_overrides_allow_pass_in_the_generator_itself():
    """Both call sites pass `allow_pass=rounds_now, closing=in_closing`, because the closing
    round IS a round. So the override lives in `_generate_response`, where a third call site
    cannot forget it — and it removes the field from the schema, not only from the prose."""
    reply = _say("Nothing to add — the doc matches what I said last round.", **{"pass": True})

    async def generate(**flags):
        seen = {}

        def fake(*_a, **kw):
            seen.update(kw)
            return _Resp(reply)

        with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
            out = await simulator._generate_response(
                "Bo", AgentState(name="Bo", persona="a careful SRE", goals=["find what breaks"]),
                "egress inspection", [{"speaker": "Ada", "content": "where are we"}],
                get_settings(), cognition=CognitionConfig(enabled=True), **flags,
            )
        prompt = " ".join(m["content"] for m in seen["messages"])
        return out, prompt, seen["response_format"]["json_schema"]["schema"]["properties"]

    out, prompt, schema = await generate(allow_pass=True, closing=True)
    assert "passed" not in out
    assert out["content"].startswith("Nothing to add")
    assert "pass" not in schema
    assert '"pass"' not in prompt and _CLOSING.strip() in prompt

    # The same reply in an ordinary round is a pass, as it always was.
    out, prompt, schema = await generate(allow_pass=True, closing=False)
    assert out["passed"] is True
    assert "pass" in schema and '"pass"' in prompt
