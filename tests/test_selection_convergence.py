# SPDX-License-Identifier: Apache-2.0
"""
Intervention H: the moderator may decline, and that ends the run — opt-in, for now.

`stop_when_converged` ships **off**. Offline validation (§14) found a reproducible premature
stop at turn 17 of the 40-turn control under closed-loop replay, and the committed criterion
says one of those rejects the arm. Open-loop replay — the faithful protocol, since production
counts always match the transcript — declines only on the fair run and at the right turn, but
that protocol was chosen after seeing the result, so a live run decides. These tests opt in
explicitly.

Why it exists (`docs/SPEAKER-SELECTION-EVALUATION.md` §13–§14): with turns spread evenly, run
`d7d739dd` stated every position by turn 25 of 40 and spent the last fifteen turns on
"confirmed, nothing to add". The padding was **not** misallocation — the five
fairness-motivated picks before turn 26 were all substantive, and from turn 26 every turn was
filler whoever was chosen, including the persona with the most turns. There was no better pick
available, so the mechanism has to be able to answer "nobody".

The asymmetry is what the guards are for, and it is severe:

    fifteen turns of "nothing to add"     ~$0.30 wasted
    fifteen turns of argument cut short    the whole run

So a decline is honoured only when every persona has spoken AND it is the second in a row. A
premature stop is the failure this file is mostly about.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio.engine import run_simulation, simulator
from matrix_studio.engine.simulator import _select_next_speaker
from matrix_studio.settings import get_settings
from matrix_studio.state import AgentState, CognitionConfig, SelectionConfig

pytestmark = pytest.mark.asyncio

CAST = ["Ada", "Bo", "Cy"]


class _Resp:
    def __init__(self, content):
        self.choices = [
            MagicMock(message=MagicMock(content=content), finish_reason="stop")
        ]
        self.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        self._hidden_params = {"response_cost": 0.0}


def _agents():
    return {n: AgentState(name=n, persona=f"a {n}", goals=["g"]) for n in CAST}


DECLINE = json.dumps({"speaker": None, "reason": "every position is stated and parked"})


async def _select(reply, selection=None, cognition=True, conversation=None):
    async def fake(**_kwargs):
        return _Resp(reply)

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        return await _select_next_speaker(
            "t", _agents(),
            conversation if conversation is not None else [{"speaker": "Ada", "content": "x"}],
            "Ada", get_settings(),
            cognition=CognitionConfig(enabled=cognition),
            selection=selection or SelectionConfig(stop_when_converged=True),
            max_messages=10,
        )


# --------------------------------------------------------------------------- #
# (1) reading the verdict
# --------------------------------------------------------------------------- #


class TestTheVerdict:
    async def test_an_explicit_null_speaker_is_a_decline(self):
        choice = await _select(DECLINE)
        assert choice.name is None
        assert choice.reason == "every position is stated and parked"
        assert choice.fallback is None, "a decline is a decision, not a degradation"

    async def test_a_MISSING_speaker_key_is_a_malformed_reply_not_a_decline(self):
        """The distinction the whole feature turns on. `{"reason": "..."}` with no speaker is
        a broken reply; treating it as "the conversation is over" would end runs on a parse
        slip, which is the worst possible failure mode for an auto-stop.

        What it resolves TO is a separate question — here the raw reply mentions Bo, so the
        substring resolver finds him (intervention F, still open). The contract asserted is
        only that it is not a decline."""
        choice = await _select(json.dumps({"reason": "I think Bo"}))
        assert choice.name is not None

    async def test_an_empty_string_speaker_is_not_a_decline_either(self):
        choice = await _select(json.dumps({"speaker": "", "reason": "unsure"}))
        assert choice.name is not None
        assert choice.fallback == "unresolved"

    async def test_prose_that_mentions_nobody_is_not_a_decline(self):
        choice = await _select("I think the conversation is finished")
        assert choice.fallback == "unresolved"

    async def test_the_prompt_does_not_offer_the_option_when_the_switch_is_off(self):
        seen = {}

        async def fake(**kwargs):
            seen.update(kwargs)
            return _Resp(json.dumps({"speaker": "Bo", "reason": "r"}))

        with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
            await _select_next_speaker(
                "t", _agents(), [{"speaker": "Ada", "content": "x"}], "Ada", get_settings(),
                cognition=CognitionConfig(enabled=True),
                selection=SelectionConfig(stop_when_converged=False), max_messages=10,
            )
        assert '"speaker": null' not in seen["messages"][-1]["content"]

    async def test_a_decline_is_ignored_when_the_switch_is_off(self):
        """Belt and braces: the prompt does not ask for it, but a model that volunteers it
        anyway must not end a run that opted out."""
        choice = await _select(DECLINE, selection=SelectionConfig(stop_when_converged=False))
        assert choice.name is not None

    async def test_the_cognition_off_prompt_never_offers_it(self):
        """That prompt asks for a bare name, which cannot express "nobody"."""
        seen = {}

        async def fake(**kwargs):
            seen.update(kwargs)
            return _Resp("Bo")

        with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
            await _select_next_speaker(
                "t", _agents(), [{"speaker": "Ada", "content": "x"}], "Ada", get_settings(),
                cognition=CognitionConfig(enabled=False),
                selection=SelectionConfig(stop_when_converged=True), max_messages=10,
            )
        assert "reply with" not in seen["messages"][-1]["content"]


# --------------------------------------------------------------------------- #
# (2) the guards, which are the whole risk
# --------------------------------------------------------------------------- #

REQUEST = {
    "topic": "AI ethics",
    "cast": [
        {"name": "Ada", "persona": "ethicist", "goals": ["seek truth"]},
        {"name": "Ben", "persona": "engineer", "goals": ["ship safely"]},
        {"name": "Cy", "persona": "lawyer", "goals": ["stay legal"]},
    ],
}


def _fake_engine(selection_replies):
    """Replies the moderator gives, in order; anything else is a persona's turn."""
    replies = list(selection_replies)

    def fake(*_args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(replies.pop(0) if replies else json.dumps(
                {"speaker": "Ada", "reason": "default"}))
        return _Resp(json.dumps({"utterance": "a point", "rationale": "why",
                                 "goal_served": "none"}))

    return fake


async def _run(db, run_id, selection_replies, max_messages=8, config=None):
    """Opts IN to `stop_when_converged`, which ships off — see the module docstring."""
    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_fake_engine(selection_replies)):
        req = dict(REQUEST)
        req["config"] = {"max_messages": max_messages, "generate_avatars": False,
                         "cognition": {"enabled": True},
                         "selection": {"stop_when_converged": True}, **(config or {})}
        return await run_simulation(req, db=db, run_id=run_id)


async def _events(db, run_id, event_type):
    out = []
    for r in await db.get_events(run_id):
        if r["event_type"] != event_type:
            continue
        p = r["payload"]
        out.append({"turn": r["turn"],
                    **(json.loads(p) if isinstance(p, str) else p)})
    return out


def _pick(name):
    return json.dumps({"speaker": name, "reason": f"{name} next"})


class TestTheGuards:
    async def test_one_decline_does_not_end_a_run(self, db):
        """A single odd judgement should cost a turn, not a conversation."""
        replies = [_pick("Ada"), _pick("Ben"), _pick("Cy"), DECLINE,
                   _pick("Ada"), _pick("Ben"), _pick("Cy"), _pick("Ada")]
        result = await _run(db, "one-decline", replies)
        assert result["total_turns"] == 8
        assert "converged" not in result
        declined = await _events(db, "one-decline", "speaker.declined")
        assert len(declined) == 1 and declined[0]["honoured"] is False

    async def test_two_consecutive_declines_end_the_run(self, db):
        """Turn 4 still happens: the first decline is overridden, which is the documented
        price of requiring two — one padded turn instead of a truncated conversation. The
        SECOND decline produces no turn at all, so the run ends at 4 of 8."""
        replies = [_pick("Ada"), _pick("Ben"), _pick("Cy"), DECLINE, DECLINE]
        result = await _run(db, "converged", replies)
        assert result["total_turns"] == 4, "the honoured decline is not a turn"
        assert result["converged"]["at_turn"] == 4
        assert result["converged"]["reason"] == "every position is stated and parked"
        assert result["status"] == "complete"

    async def test_it_cannot_converge_before_everyone_has_spoken(self, db):
        """Otherwise the starvation this whole feature exists to remove comes back wearing a
        better name: a run ending at turn 2 with four personas unheard.

        A moderator declining from turn 2 is overridden until the cast is covered — turns 2
        and 3 go to the unheard personas — and only then may it stop. That is the guard doing
        exactly what it says, and it is also the weakest point of the design: nothing stops a
        persistently-declining moderator ending a run at turn 3 of 40 once coverage is met.
        The pre-registered offline validation (§14) is what checks that does not happen on
        real transcripts; a premature stop there rejects the arm."""
        replies = [_pick("Ada"), DECLINE, DECLINE, DECLINE, DECLINE,
                   _pick("Ben"), _pick("Cy"), _pick("Ada")]
        result = await _run(db, "too-early", replies)
        declined = await _events(db, "too-early", "speaker.declined")
        # Overridden while anybody was unheard…
        assert [d["honoured"] for d in declined[:2]] == [False, False]
        assert [d["everyone_spoke"] for d in declined[:2]] == [False, False]
        # …and the overrides went to the personas who had not spoken.
        picks = await _events(db, "too-early", "speaker.selected")
        assert {picks[1]["speaker"], picks[2]["speaker"]} == {"Ben", "Cy"}
        # Coverage reached, so the third decline is allowed to end it.
        assert result["converged"]["at_turn"] == 3

    async def test_an_overridden_decline_calls_on_the_least_heard_persona(self, db):
        """The guard exists because somebody has not been heard from, so the override has to
        serve that — a random pick would satisfy the loop and not the reason for it."""
        replies = [_pick("Ada"), _pick("Ada"), DECLINE, _pick("Ada"), _pick("Ada")]
        await _run(db, "override", replies, max_messages=5)
        turns = await _events(db, "override", "speaker.selected")
        assert turns[2]["speaker"] in {"Ben", "Cy"}, turns[2]
        assert turns[2]["selection_fallback"] == "declined_override"

    async def test_a_decline_streak_is_broken_by_a_real_pick(self, db):
        """Consecutive, not cumulative. Two declines twenty turns apart are two moments of
        doubt, not a converged conversation."""
        replies = [_pick("Ada"), _pick("Ben"), _pick("Cy"), DECLINE,
                   _pick("Ada"), DECLINE, _pick("Ben"), _pick("Cy")]
        result = await _run(db, "streak", replies)
        assert "converged" not in result
        assert result["total_turns"] == 8

    async def test_the_run_is_switched_off_by_config(self, db):
        replies = [_pick("Ada"), _pick("Ben"), _pick("Cy"), DECLINE, DECLINE,
                   _pick("Ada"), _pick("Ben"), _pick("Cy")]
        result = await _run(db, "no-stop", replies,
                            config={"selection": {"stop_when_converged": False}})
        assert "converged" not in result
        assert result["total_turns"] == 8


# --------------------------------------------------------------------------- #
# (3) what the transcript says about it
# --------------------------------------------------------------------------- #


class TestTheRecord:
    async def test_sim_completed_says_why_the_run_is_short(self, db):
        """A 3-turn run that asked for 8 has to explain itself, or the next reader files a
        bug about turns going missing."""
        replies = [_pick("Ada"), _pick("Ben"), _pick("Cy"), DECLINE, DECLINE]
        await _run(db, "record", replies)
        done = await _events(db, "record", "sim.completed")
        assert len(done) == 1
        assert done[0]["converged"] is True
        assert done[0]["converged_at_turn"] == 4
        assert done[0]["turns_unused"] == 4
        assert done[0]["converged_reason"]
        assert done[0]["total_turns"] == 4

    async def test_a_normal_run_says_nothing_about_convergence(self, db):
        """Additive-only, like every other payload key here: presence is the signal."""
        await _run(db, "normal", [_pick("Ada"), _pick("Ben"), _pick("Cy")], max_messages=3)
        done = await _events(db, "normal", "sim.completed")
        assert "converged" not in done[0]
        assert "converged_at_turn" not in done[0]

    async def test_every_decline_is_recorded_even_when_overridden(self, db):
        """The overridden ones are the interesting data: they say the moderator wanted to
        stop and the guard refused, which is how the guard's threshold gets tuned."""
        replies = [_pick("Ada"), DECLINE, _pick("Ben"), _pick("Cy"), _pick("Ada")]
        await _run(db, "recorded", replies, max_messages=5)
        declined = await _events(db, "recorded", "speaker.declined")
        assert len(declined) == 1
        assert declined[0]["consecutive"] == 1
        assert declined[0]["honoured"] is False
        assert declined[0]["reason"] == "every position is stated and parked"

    async def test_a_converged_run_is_not_left_looking_live(self, db):
        """The failure this would cause is a stream that never ends: `_run_turns` returns
        `running` when turns remain in the budget, and a converged run has turns remaining
        BY DEFINITION. Missing that check would leave the state machine calling the turn
        Lambda for ever."""
        replies = [_pick("Ada"), _pick("Ben"), _pick("Cy"), DECLINE, DECLINE]
        result = await _run(db, "not-live", replies, max_messages=40)
        assert result["status"] == "complete"
        run = await db.get_run("not-live")
        assert run["status"] == "complete"


# --------------------------------------------------------------------------- #
# (4) ONE TURN PER INVOCATION — the path that ships
# --------------------------------------------------------------------------- #


class TestTheStreakSurvivesAcrossInvocations:
    """The tests above drive `run_simulation`, which loops in one process. **That is not
    what ships.** Step Functions calls a turn Lambda with `turn_budget=1`, so the loop runs
    one iteration and returns, and anything held in a local variable is gone.

    The first version of intervention H kept the decline counter in a local. Every test
    above passed and the feature was inert in production: run `28235cec` declined eleven
    times, three of them on consecutive turns, and every event recorded `consecutive: 1`
    because the counter restarted each turn. These tests drive the deployed shape instead.
    """

    async def test_the_snapshot_carries_the_streak(self, db):
        """One decline, one turn: the streak has to be on the checkpoint, because the next
        invocation has nothing else to read."""
        replies = [_pick("Ada"), _pick("Ben"), _pick("Cy"), DECLINE, _pick("Ada")]
        await _run(db, "streak-snap", replies, max_messages=5)
        snap = await db.get_snapshot("streak-snap", 4)
        assert snap is not None
        assert snap.decline_streak == 1, "turn 4 was an overridden decline"
        assert (await db.get_snapshot("streak-snap", 3)).decline_streak == 0

    async def test_an_old_snapshot_without_the_field_still_loads(self, db):
        """Every snapshot written before 2026-09-16 lacks it. A required field here would
        make historical runs unopenable, which is a worse failure than a reset counter."""
        from matrix_studio.state import SimSnapshot

        raw = {
            "run_id": "old", "turn": 3, "topic": "t", "agents": {},
            "conversation": [], "status": "running", "created_at": 0, "total_turns": 3,
        }
        assert SimSnapshot(**raw).decline_streak == 0

    async def test_convergence_fires_when_turns_arrive_one_at_a_time(self, db):
        """The regression test for the shipped defect. Two declines on consecutive turns,
        delivered by two SEPARATE calls with `turn_budget=1`, must converge — which is only
        possible if the streak came back off the snapshot."""
        from matrix_studio.engine.simulator import resume_simulation
        from matrix_studio.orchestration import load_state

        # Three ordinary turns first, so the coverage guard is satisfied.
        await _run(db, "slices", [_pick("Ada"), _pick("Ben"), _pick("Cy")], max_messages=9)
        run = await db.get_run("slices")

        results = []
        for expected_turn in (4, 5):
            topic, agents, conversation, threads, ledger, streak = await load_state(
                db, run, expected_turn - 1
            )
            with patch("matrix_studio.engine.simulator.litellm.acompletion",
                       side_effect=_fake_engine([DECLINE])):
                results.append(await resume_simulation(
                    run_id="slices", topic=topic, agents=agents,
                    conversation=conversation, from_turn=expected_turn - 1,
                    start_seq=await db.max_seq("slices") + 1, max_messages=9, db=db,
                    cognition=CognitionConfig(enabled=True),
                    selection=SelectionConfig(stop_when_converged=True),
                    pending_threads=threads, firsthand_citations=ledger,
                    decline_streak=streak, turn_budget=1,
                ))

        # First slice: decline overridden (streak was 0 coming in), so a turn happened.
        assert results[0]["status"] == "running"
        assert results[0]["total_turns"] == 4
        # Second slice: the streak arrived as 1, so this decline is the second in a row.
        assert results[1]["status"] == "complete", results[1]["status"]
        assert results[1]["converged"]["at_turn"] == 4
        declined = await _events(db, "slices", "speaker.declined")
        assert [d["consecutive"] for d in declined] == [1, 2], declined

    async def test_a_slice_reads_the_streak_from_the_log_when_there_is_no_snapshot(self, db):
        """The replay fallback. `reconstruct_at_turn` rebuilds state from events for a slice
        with no checkpoint, and returning 0 there would reset the streak invisibly — the same
        bug one layer down."""
        from matrix_studio.orchestration import decline_streak_from_log

        replies = [_pick("Ada"), _pick("Ben"), _pick("Cy"), DECLINE, _pick("Ada")]
        await _run(db, "from-log", replies, max_messages=5)
        assert await decline_streak_from_log(db, "from-log", 4) == 1
        assert await decline_streak_from_log(db, "from-log", 3) == 0
        assert await decline_streak_from_log(db, "from-log", 5) == 0

    async def test_it_converges_through_execute_slice_the_way_the_machine_calls_it(self, db):
        """The seam that actually broke. Everything above can pass while the deployed path
        stays inert, because the deployed path is `execute_slice` — one call per turn, fresh
        process, state only from the checkpoint. A mutant that hardcodes `decline_streak=0`
        in `orchestration.run_turn` survives every other test in this file and is caught here.
        """
        from matrix_studio import orchestration

        await db.create_run(
            run_id="slice-conv", topic="t",
            cast=[{"name": n, "persona": "p", "goals": []} for n in ("Ada", "Ben", "Cy")],
            config={"max_messages": 9, "generate_avatars": False,
                    "cognition": {"enabled": True},
                    "selection": {"stop_when_converged": True}},
        )
        # Three real turns so the coverage guard is satisfied, one slice at a time.
        turn = 0
        for name in ("Ada", "Ben", "Cy"):
            with patch("matrix_studio.engine.simulator.litellm.acompletion",
                       side_effect=_fake_engine([_pick(name)])):
                out = await orchestration.execute_slice(
                    db, "slice-conv", turn=turn, turn_budget=1
                )
            turn = out["turn"]
        assert turn == 3 and out["status"] == "running"

        # Now two declines, in two separate slices.
        with patch("matrix_studio.engine.simulator.litellm.acompletion",
                   side_effect=_fake_engine([DECLINE])):
            first = await orchestration.execute_slice(
                db, "slice-conv", turn=turn, turn_budget=1
            )
        assert first["status"] == "running", "one decline must not end a run"

        with patch("matrix_studio.engine.simulator.litellm.acompletion",
                   side_effect=_fake_engine([DECLINE])):
            second = await orchestration.execute_slice(
                db, "slice-conv", turn=first["turn"], turn_budget=1
            )
        assert second["status"] == "complete", second
        assert second["done"] is True
        declined = await _events(db, "slice-conv", "speaker.declined")
        assert [d["consecutive"] for d in declined] == [1, 2], declined
        done = await _events(db, "slice-conv", "sim.completed")
        assert done[0]["converged"] is True
        assert done[0]["converged_at_turn"] == 4
        assert done[0]["turns_unused"] == 5
