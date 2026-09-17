# SPDX-License-Identifier: Apache-2.0
"""
Rotation and hybrid: the other two ways to decide who speaks.

Four methods now, differing in two independent dimensions — whether everyone speaks this turn,
and whether they can see each other while doing it:

    moderated      one speaker per turn, chosen by a model
    rotation       everyone once per round, SEEING the earlier speakers in that round
    simultaneous   everyone at once, BLIND to the others that round
    hybrid         N opening simultaneous rounds, then moderated

`rotation` is nearly free given the round machinery — it is `simultaneous` with the frozen
state swapped for the live one — and it is the mode the moderated interventions have been
approximating: turn share is equal by construction rather than by prompt.

`hybrid` exists because the two live runs failed in opposite directions. The simultaneous run
(`36231059`) opened superbly — every position on the table by round 1 — and degenerated into
parallel restatement by round 3, four personas answering the same question. The moderated runs
take 8–11 turns to introduce the cast but stay cumulative. So: open in rounds, then hand over.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio.engine import run_simulation
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


def _engine(script=None):
    script = dict(script or {})
    prompts: list[tuple[str, str]] = []
    moderator_calls: list[str] = []

    def fake(*_a, **kw):
        text = " ".join(m["content"] for m in kw["messages"])
        if "conversation moderator" in text:
            moderator_calls.append(text)
            return _Resp(json.dumps({"speaker": "Ada", "reason": "Ada again"}))
        who = next((n for k, n in WHO.items() if k in text), None)
        if who is None:
            return _Resp("two-words")
        prompts.append((who, text))
        replies = script.get(who) or []
        return _Resp(replies.pop(0) if replies else _say(f"{who} speaks"))

    fake.prompts = prompts
    fake.moderator_calls = moderator_calls
    return fake


async def _run(db, run_id, *, method, turns, script=None, **selection):
    fake = _engine(script)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        result = await run_simulation({
            "topic": "AI ethics", "cast": CAST,
            "config": {
                "max_messages": turns, "generate_avatars": False,
                "cognition": {"enabled": True},
                "selection": {"method": method, **selection},
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
# rotation: rounds, but sighted
# --------------------------------------------------------------------------- #


class TestRotation:
    async def test_everyone_speaks_once_per_round(self, db):
        result, fake = await _run(db, "rot", method="rotation", turns=2)
        turns = [r["turn"] for r in await _responses(db, "rot")]
        assert turns == [1, 1, 1, 2, 2, 2]
        assert fake.moderator_calls == [], "nobody selects in a rotation"
        assert len(result["conversation"]) == 6

    async def test_later_speakers_in_a_round_DO_see_the_earlier_ones(self, db):
        """The whole difference from `simultaneous`, and the reason to have both: a rotation
        is cumulative, so the second speaker can answer the first."""
        _, fake = await _run(
            db, "rot-sees", method="rotation", turns=1,
            script={"Ada": [_say("MARKER FROM ADA")]},
        )
        prompts = dict(fake.prompts)
        assert "MARKER FROM ADA" in prompts["Ben"]
        assert "MARKER FROM ADA" in prompts["Cy"]

    async def test_passing_is_offered(self, db):
        """Everyone is asked every round here too, so the quiet ones need the same out."""
        _, fake = await _run(db, "rot-pass", method="rotation", turns=1)
        assert all('"pass"' in t for _, t in fake.prompts)

    async def test_a_round_where_everybody_passes_still_converges(self, db):
        PASS = json.dumps({"utterance": "", "rationale": "n", "goal_served": "none",
                           "pass": True})
        result, _ = await _run(
            db, "rot-conv", method="rotation", turns=6,
            script={"Ada": [_say("a"), PASS], "Ben": [_say("b"), PASS],
                    "Cy": [_say("c"), PASS]},
        )
        assert result["converged"]["reason"] == "every participant passed this round"


# --------------------------------------------------------------------------- #
# hybrid: rounds, then a moderator
# --------------------------------------------------------------------------- #


class TestHybrid:
    async def test_it_opens_in_rounds_then_switches_to_one_speaker_a_turn(self, db):
        result, fake = await _run(db, "hyb", method="hybrid", turns=4,
                                  hybrid_opening_rounds=2)
        turns = [r["turn"] for r in await _responses(db, "hyb")]
        # Rounds 1 and 2 are everybody; turns 3 and 4 are one speaker each.
        assert turns == [1, 1, 1, 2, 2, 2, 3, 4], turns
        assert result["total_turns"] == 4

    async def test_the_moderator_is_called_only_after_the_opening(self, db):
        _, fake = await _run(db, "hyb-mod", method="hybrid", turns=4,
                             hybrid_opening_rounds=2)
        # Two moderated turns means exactly two selection calls, and none during the rounds.
        assert len(fake.moderator_calls) == 2, fake.moderator_calls

    async def test_the_opening_rounds_are_blind_and_the_moderated_turns_are_not(self, db):
        """Blindness belongs to the round, not the method. A moderated turn that could not
        see the previous turn would be a different mode altogether."""
        _, fake = await _run(
            db, "hyb-blind", method="hybrid", turns=3, hybrid_opening_rounds=1,
            script={"Ada": [_say("OPENING MARKER"), _say("later"), _say("later")]},
        )
        by: dict[str, list[str]] = {}
        for who, text in fake.prompts:
            by.setdefault(who, []).append(text)
        # Round 1: Ben cannot see Ada's opener.
        assert "OPENING MARKER" not in by["Ben"][0]
        # The moderated turn that follows can.
        assert "OPENING MARKER" in by["Ada"][1]

    async def test_the_moderator_inherits_the_openings_participation_counts(self, db):
        """The fairness block counts the whole conversation, so by the time the moderator
        takes over everyone already has turns on the board — which is the state the
        interventions in §10–§11 spend a prompt trying to reach."""
        _, fake = await _run(db, "hyb-counts", method="hybrid", turns=3,
                             hybrid_opening_rounds=2)
        first_selection = fake.moderator_calls[0]
        assert "Participation so far" in first_selection
        for name in ("Ada", "Ben", "Cy"):
            assert f"- {name}: 2 turn(s) so far" in first_selection, first_selection

    async def test_one_opening_round_is_allowed(self, db):
        _, fake = await _run(db, "hyb-one", method="hybrid", turns=3,
                             hybrid_opening_rounds=1)
        turns = [r["turn"] for r in await _responses(db, "hyb-one")]
        assert turns == [1, 1, 1, 2, 3]

    async def test_an_opening_longer_than_the_run_never_reaches_the_moderator(self, db):
        """Not an error: a 2-turn run with a 5-round opening is two rounds of everybody,
        which is a coherent thing to ask for and cheaper than failing."""
        _, fake = await _run(db, "hyb-long", method="hybrid", turns=2,
                             hybrid_opening_rounds=5)
        assert [r["turn"] for r in await _responses(db, "hyb-long")] == [1, 1, 1, 2, 2, 2]
        assert fake.moderator_calls == []

    async def test_passing_is_offered_in_the_opening_and_not_after(self, db):
        _, fake = await _run(db, "hyb-pass", method="hybrid", turns=3,
                             hybrid_opening_rounds=1)
        opening = [t for _, t in fake.prompts[:3]]
        after = [t for _, t in fake.prompts[3:]]
        assert all('"pass"' in t for t in opening)
        assert all('"pass"' not in t for t in after), "a moderated turn was asked for"


# --------------------------------------------------------------------------- #
# the config
# --------------------------------------------------------------------------- #


def test_every_method_is_accepted_and_a_typo_is_not():
    for m in ("moderated", "rotation", "simultaneous", "hybrid"):
        assert SelectionConfig(method=m).method == m
    with pytest.raises(ValueError):
        SelectionConfig(method="all-talk")


def test_the_opening_default_is_two_rounds():
    """From the one live run there is: round 1 puts every position on the table, round 2 was
    the strongest of eight, and the parallel restatement starts at round 3."""
    assert SelectionConfig().hybrid_opening_rounds == 2
    with pytest.raises(ValueError):
        SelectionConfig(hybrid_opening_rounds=0)
