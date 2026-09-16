# SPDX-License-Identifier: Apache-2.0
"""
The simultaneous conversation method: everyone is asked every round.

`selection.method = "simultaneous"` replaces the moderator with a round. Every persona is
asked, **against the state as it stood when the round opened**, so none of them can see what
the others are saying that round. Whoever passes is dropped and never reaches the transcript;
the survivors share one turn number, because they genuinely happened at once.

Three properties carry the mode, and each is a test below:

1. **Blindness.** The second persona in a round must NOT see the first. If it does, this is a
   rotation with extra steps, and the round grouping in the transcript becomes a lie.
2. **A pass is declared, not detected.** The persona sets `pass: true`; a prose backstop
   exists for a model that ignores the field, and a disagreement between them is logged
   rather than silently resolved.
3. **A round is a turn.** `turn_budget=1` under Step Functions must generate a WHOLE round —
   a sixth of a round would leave the next invocation opening a fresh one with the earlier
   speakers lost.

And the payoff: a round in which everybody passes is convergence that was *measured* — six
personas each asked, each declining — rather than inferred by a moderator (§14–§16).
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio.engine import run_simulation, simulator
from matrix_studio.state import SelectionConfig

pytestmark = pytest.mark.asyncio

CAST = [
    {"name": "Ada", "persona": "ethicist", "goals": ["seek truth"]},
    {"name": "Ben", "persona": "engineer", "goals": ["ship safely"]},
    {"name": "Cy", "persona": "lawyer", "goals": ["stay legal"]},
]
REQUEST = {"topic": "AI ethics", "cast": CAST}


class _Resp:
    def __init__(self, content):
        self.choices = [
            MagicMock(message=MagicMock(content=content), finish_reason="stop")
        ]
        self.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        self._hidden_params = {"response_cost": 0.001}


def _say(text, **extra):
    return json.dumps({"utterance": text, "rationale": "why", "goal_served": "none", **extra})


PASS = json.dumps({"utterance": "", "rationale": "nothing new", "goal_served": "none",
                   "pass": True})


#: Each persona's description, which is what identifies the caller. NOT the "Respond as
#: <name>:" cue: that only appears once somebody has spoken, and in round 1 of a
#: simultaneous run the frozen state is empty for everyone, so all N personas get the
#: cold-start prompt instead. Worth knowing about the mode, and it made the first version
#: of this fake unable to tell the three of them apart.
WHO = {"ethicist": "Ada", "engineer": "Ben", "lawyer": "Cy"}


def _engine(script):
    """`script` maps a persona name to the replies it gives, in call order per persona."""
    seen = []

    def fake(*_a, **kw):
        text = " ".join(m["content"] for m in kw["messages"])
        assert "conversation moderator" not in text, "no moderator in simultaneous mode"
        who = next((n for k, n in WHO.items() if k in text), None)
        if who is None:
            # Naming, reflection, validation — not a voice call, so not this fake's
            # business and not counted as one.
            return _Resp("two-words")
        seen.append((who, text))
        replies = script.get(who) or []
        return _Resp(replies.pop(0) if replies else _say(f"{who} says something"))

    fake.seen = seen
    return fake


async def _run(db, run_id, script, max_messages=3, config=None):
    fake = _engine(script)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {
            "max_messages": max_messages, "generate_avatars": False,
            "cognition": {"enabled": True},
            "selection": {"method": "simultaneous"}, **(config or {}),
        }
        result = await run_simulation(req, db=db, run_id=run_id)
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


# --------------------------------------------------------------------------- #
# (1) blindness — the property that makes it "simultaneous"
# --------------------------------------------------------------------------- #


async def test_nobody_in_a_round_can_see_anybody_else_in_it(db):
    """The whole mode. Ada speaks first in round 1; Ben and Cy must not see her words."""
    _, fake = await _run(db, "blind", {"Ada": [_say("ARBITRARY MARKER FROM ADA")]},
                         max_messages=1)
    prompts = {who: text for who, text in fake.seen}
    assert len(prompts) == 3, "all three were asked"
    for who in ("Ben", "Cy"):
        assert "ARBITRARY MARKER FROM ADA" not in prompts[who], who


async def test_the_next_round_does_see_the_previous_one(db):
    """Blind within a round, not across rounds — otherwise it is three monologues."""
    _, fake = await _run(db, "across", {"Ada": [_say("MARKER ROUND ONE"), _say("later")]},
                         max_messages=2)
    round_two = [t for who, t in fake.seen if who == "Ben"][1]
    assert "MARKER ROUND ONE" in round_two


async def test_the_moderator_is_never_called(db):
    """Asserted in the fake itself, so it holds for every test in this file: a selection
    call in this mode would be a wasted call and a decision nobody asked for."""
    _, fake = await _run(db, "no-mod", {}, max_messages=2)
    assert len(fake.seen) == 6, "two rounds of three, no moderator calls"


# --------------------------------------------------------------------------- #
# (2) the pass
# --------------------------------------------------------------------------- #


async def test_a_pass_is_dropped_from_the_transcript_but_recorded_as_an_event(db):
    result, _ = await _run(db, "passed", {"Ben": [PASS]}, max_messages=1)
    said = [e["agent"] for e in await _events(db, "passed", "agent.response")]
    assert said == ["Ada", "Cy"], said
    passes = await _events(db, "passed", "agent.passed")
    assert [p["agent"] for p in passes] == ["Ben"]
    # The call was still paid for, and the event says so — a converging run in this mode
    # gets MORE expensive per surviving turn, and hiding that would misreport it.
    assert passes[0]["cost_usd"] > 0
    assert len(result["conversation"]) == 2


async def test_a_prose_pass_is_caught_when_the_model_ignores_the_field(db):
    """The field is the contract; this is the backstop. A model that writes "I'll pass" in
    the utterance and leaves the flag unset must not have that appended as speech."""
    _, _ = await _run(db, "prose", {"Cy": [_say("Pass — nothing to add from me.")]},
                      max_messages=1)
    assert [e["agent"] for e in await _events(db, "prose", "agent.passed")] == ["Cy"]


async def test_a_reply_that_merely_mentions_passing_is_not_a_pass(db):
    """The backstop is anchored to the START of the utterance for exactly this reason: a
    turn about passing legislation, or one that says "I won't pass on this", is speech."""
    _, _ = await _run(
        db, "mention",
        {"Cy": [_say("We should not pass this without Casey's sign-off; nothing here is "
                     "settled.")]},
        max_messages=1,
    )
    assert await _events(db, "mention", "agent.passed") == []
    said = [e["agent"] for e in await _events(db, "mention", "agent.response")]
    assert "Cy" in said


async def test_passing_is_not_offered_in_moderated_mode(db):
    """The option costs prompt tokens and changes behaviour; a sequential run must not get
    it, or every measurement in the design doc describes a different prompt."""
    seen = {}

    def fake(*_a, **kw):
        text = " ".join(m["content"] for m in kw["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Ada", "reason": "r"}))
        seen["prompt"] = text
        return _Resp(_say("hello"))

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 1, "generate_avatars": False,
                         "cognition": {"enabled": True}}
        await run_simulation(req, db=db, run_id="moderated")
    assert '"pass"' not in seen["prompt"]
    assert "Passing is not" not in seen["prompt"]


# --------------------------------------------------------------------------- #
# (3) a round is a turn
# --------------------------------------------------------------------------- #


async def test_the_survivors_of_a_round_share_one_turn_number(db):
    """They happened at once, so they are one turn. A reader — and the participation
    timeline — needs that, or three simultaneous replies look like three exchanges."""
    await _run(db, "shared", {}, max_messages=2)
    responses = await _events(db, "shared", "agent.response")
    assert [r["turn"] for r in responses] == [1, 1, 1, 2, 2, 2]


async def test_a_slice_generates_a_whole_round_not_a_fraction_of_one(db):
    """The deployed shape. `turn_budget=1` means one TURN, and a turn is a round — a slice
    that stopped after one persona would lose the rest of the round on the next invocation,
    which is the same class of bug as the decline streak resetting per Lambda."""
    from matrix_studio.engine.simulator import resume_simulation

    fake = _engine({})
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        result = await simulator._run_turns(
            run_id="slice", topic="t",
            agents={c["name"]: simulator.AgentState(
                name=c["name"], persona=c["persona"], goals=c["goals"]) for c in CAST},
            conversation=[], last_speaker=None, start_turn=0, max_messages=9,
            settings=simulator.get_settings(), db=None,
            emit=_noop, next_seq=_seq(), turn_budget=1,
            cognition=simulator.CognitionConfig(enabled=True),
            selection=SelectionConfig(method="simultaneous"),
        )
    assert result["status"] == "running", "a round finished, the run has not"
    assert result["total_turns"] == 1
    assert len(result["conversation"]) == 3, "all three of the round's turns"


async def test_a_round_where_everybody_passes_ends_the_run(db):
    """Convergence, measured. Every persona was asked and every one declined — no moderator
    verdict, no two-in-a-row guard, no streak on the snapshot."""
    result, _ = await _run(
        db, "all-pass", {"Ada": [_say("one"), PASS], "Ben": [_say("two"), PASS],
                         "Cy": [_say("three"), PASS]}, max_messages=6,
    )
    assert result["total_turns"] == 1, "round 2 produced no turns, so it is not a turn"
    assert result["converged"]["reason"] == "every participant passed this round"
    assert result["status"] == "complete"
    done = await _events(db, "all-pass", "sim.completed")
    assert done[0]["converged"] is True
    assert done[0]["converged_at_turn"] == 1


async def test_a_partial_pass_does_not_end_the_run(db):
    """Two of three passing is a quiet round, not a finished conversation."""
    result, _ = await _run(db, "partial", {"Ada": [PASS], "Ben": [PASS]}, max_messages=1)
    assert "converged" not in result
    assert len(result["conversation"]) == 1


async def test_the_selected_event_says_nobody_selected(db):
    await _run(db, "marked", {}, max_messages=1)
    sel = await _events(db, "marked", "speaker.selected")
    assert len(sel) == 3
    assert all(s["method"] == "simultaneous" and s["round"] == 1 for s in sel)
    assert all("reason" not in s for s in sel), "nothing reasoned about this"


# --------------------------------------------------------------------------- #

async def _noop(**_kw):
    return None


def _seq():
    n = iter(range(10000))
    return lambda: next(n)


async def test_a_resumed_run_refreshes_the_frozen_state_each_round(db):
    """Seeded with history, which is the only way to tell "freeze once" from "freeze per
    round" apart.

    With an empty opening conversation the two are indistinguishable — round 1 freezes `[]`
    and round 2 refreshes because `[]` is falsy either way — so a mutant that froze the
    state permanently survived every other test in this file. A resumed or branched run
    starts with a transcript, and there the difference is the whole mode: round 2 must see
    round 1, and round 1 must see only the seed.
    """
    seed = [{"speaker": "Ada", "content": "SEEDED HISTORY", "turn": 1}]
    fake = _engine({"Ada": [_say("ROUND TWO OPENER"), _say("x")],
                    "Ben": [_say("b1"), _say("b2")], "Cy": [_say("c1"), _say("c2")]})
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        await simulator._run_turns(
            run_id="seeded", topic="t",
            agents={c["name"]: simulator.AgentState(
                name=c["name"], persona=c["persona"], goals=c["goals"]) for c in CAST},
            conversation=list(seed), last_speaker="Ada", start_turn=1, max_messages=3,
            settings=simulator.get_settings(), db=None, emit=_noop, next_seq=_seq(),
            cognition=simulator.CognitionConfig(enabled=True),
            selection=SelectionConfig(method="simultaneous"),
        )
    by_persona: dict[str, list[str]] = {}
    for who, text in fake.seen:
        by_persona.setdefault(who, []).append(text)
    # Round 2 (the second call to each) sees round 2's opener…
    assert "ROUND TWO OPENER" in by_persona["Ben"][1]
    # …and round 1 saw the seed but nothing from round 1 itself.
    assert "SEEDED HISTORY" in by_persona["Ben"][0]
    assert "ROUND TWO OPENER" not in by_persona["Ben"][0]
