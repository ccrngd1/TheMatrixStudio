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
    assert result["total_turns"] == 1, "the closing round produced no turns"
    assert result["status"] == "complete"
