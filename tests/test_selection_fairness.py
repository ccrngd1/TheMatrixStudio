# SPDX-License-Identifier: Apache-2.0
"""
The moderator can now see who is overdue (interventions A+B), and that is ON by default.

Adopted 2026-09-15 on 146 replays over four transcripts and two models
(`docs/SPEAKER-SELECTION-EVALUATION.md` §10–§11):

    prompt                     Gini (Haiku / Sonnet)   replays that starved somebody to 0
    shipped, no counts            0.332 / 0.335                 6 of 24
    counts + fair share           0.223 / 0.185                 0 of 24

It is the only arm that is near the top on BOTH models — the deterministic floor wins on
Haiku and the fair-share sentence wins on Sonnet — which is why it is the one that shipped:
the ranking of every other intervention inverts when the selector's model changes.

Two properties matter more than the wording being present, and both are asserted below:

1. **The text is byte-identical to the measured arm** in `scripts/eval_speaker_selection.py`.
   An arm is a claim about a prompt. If the engine's copy drifts by a word, every number
   above silently describes a prompt that is not running.
2. **It reaches every path.** The deployed stack runs turns through
   `orchestration` → `resume_simulation`, not through `run_simulation`, so a config parsed
   only in the fresh-run path is a feature that works locally and is dead in production —
   which is exactly how cognition shipped broken in v0.2.
"""

import inspect
import json
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from matrix_studio.engine import simulator
from matrix_studio.engine.simulator import _fairness_block, _select_next_speaker
from matrix_studio.personas import public_persona
from matrix_studio.settings import get_settings
from matrix_studio.state import AgentState, CognitionConfig, SelectionConfig

pytestmark = pytest.mark.asyncio

CAST = ["Ada", "Bo", "Cy"]
CONVERSATION = [
    {"speaker": "Ada", "content": "one"},
    {"speaker": "Bo", "content": "two"},
    {"speaker": "Ada", "content": "three"},
]


def _agents():
    return {n: AgentState(name=n, persona=f"a {n}", goals=["g"]) for n in CAST}


async def _noop_emit(**_kwargs):
    """`_run_turns` persists nothing when `db` is None, but it always emits."""
    return None


class _Resp:
    def __init__(self, content='{"speaker": "Bo", "reason": "r"}'):
        self.choices = [
            MagicMock(message=MagicMock(content=content), finish_reason="stop")
        ]
        self.usage = MagicMock(prompt_tokens=1, completion_tokens=1)
        self._hidden_params = {"response_cost": 0.0}


async def _prompt(selection=None, max_messages=40, conversation=None, cognition=True):
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return _Resp()

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        await _select_next_speaker(
            "the topic", _agents(),
            CONVERSATION if conversation is None else conversation,
            "Ada", get_settings(),
            cognition=CognitionConfig(enabled=cognition),
            selection=selection, max_messages=max_messages,
        )
    return seen["messages"][-1]["content"]


# --------------------------------------------------------------------------- #
# (1) the prompt the measurement was taken on
# --------------------------------------------------------------------------- #


class TestItMatchesTheMeasuredArm:
    async def test_byte_identical_to_the_counts_budget_arm(self):
        """The whole evaluation rests on this. `--check-baseline` asserts the same thing
        against a real recorded run; this asserts it offline, on every test run, so the
        drift is caught before somebody spends $21 measuring the wrong prompt."""
        import eval_speaker_selection as harness

        engine = await _prompt()
        seen = [m["speaker"] for m in CONVERSATION]
        arm = harness.ARMS["counts+budget"](
            topic="the topic",
            personas_desc="\n".join(
                f"- {n}: " + public_persona(f"a {n}", None, enabled=False) for n in CAST
            ),
            conv="\n".join(f"{m['speaker']}: {m['content']}" for m in CONVERSATION),
            last_speaker="Ada",
            cast_names=CAST,
            taken=Counter(seen),
            since={
                n: next(
                    (len(seen) - j - 1 for j in range(len(seen) - 1, -1, -1)
                     if seen[j] == n),
                    None,
                )
                for n in CAST
            },
            max_messages=40,
        )
        assert engine == arm

    def test_the_odd_phrasing_is_preserved_on_purpose(self):
        """"last spoke 0 turn(s) ago" for the persona who just spoke is what the measured
        arm says. Tidying it would be a different prompt with borrowed numbers."""
        block = _fairness_block(CAST, CONVERSATION, 40)
        assert "- Ada: 2 turn(s) so far, last spoke 0 turn(s) ago" in block
        assert "- Cy: 0 turn(s) so far, has not spoken yet" in block

    def test_the_fair_share_is_the_runs_budget_divided_by_the_cast(self):
        block = _fairness_block(CAST, CONVERSATION, 40)
        assert "runs for 40 turns with 3 participants" in block
        assert "roughly 13 turns each" in block

    def test_a_persona_who_has_not_spoken_is_named_as_such_not_given_a_number(self):
        """`since=None` must not render as "last spoke 0 turns ago", which would make the
        never-spoken persona look like the most recent one — the exact inversion of the
        signal this block exists to send."""
        block = _fairness_block(CAST, [], None)
        assert block.count("has not spoken yet") == 3
        assert "0 turn(s) ago" not in block


# --------------------------------------------------------------------------- #
# (2) the switch
# --------------------------------------------------------------------------- #


class TestTheDefaultAndTheOffSwitch:
    def test_fairness_is_on_when_no_config_is_given(self):
        """Unlike every other config block in state.py. The measured alternative is worse,
        and an opt-in default would mean the improvement reached almost no runs."""
        assert SelectionConfig().fairness is True
        assert SelectionConfig.from_config(None).fairness is True
        assert SelectionConfig.from_config({}).fairness is True
        assert SelectionConfig.from_config({"selection": "nonsense"}).fairness is True

    def test_it_can_be_turned_off(self):
        cfg = SelectionConfig.from_config({"selection": {"fairness": False}})
        assert cfg.fairness is False

    async def test_off_restores_the_pre_2026_09_15_prompt(self):
        """The off switch is what makes the comparison re-measurable after shipping."""
        off = await _prompt(selection=SelectionConfig(fairness=False))
        assert off.rstrip().endswith("Choose naturally based on conversation flow.")
        assert "Participation so far" not in off
        assert "fair share" not in off

    async def test_on_replaces_rather_than_appends_the_closing_sentence(self):
        """One closing instruction, not two. Two would leave "choose naturally." followed
        by "…but do not let a participant fall behind", which reads as a contradiction."""
        on = await _prompt()
        assert on.count("Choose naturally based on conversation flow") == 1

    async def test_the_block_is_added_to_the_cognition_off_prompt_too(self):
        """The measurement used the cognition-on prompt because that is what every recorded
        transcript ran, but there is no reason a cognition-off run should be the unfair one,
        and the closing sentence is the same string in both."""
        off = await _prompt(cognition=False)
        assert "Participation so far" in off
        assert "Respond with ONLY the name of the persona" in off


# --------------------------------------------------------------------------- #
# (3) the budget half degrades instead of guessing
# --------------------------------------------------------------------------- #


class TestTheBudgetSentence:
    async def test_no_run_length_means_counts_without_a_fair_share(self):
        """A fair share computed from the wrong denominator is worse than none: it would
        tell the moderator everyone is over their share, on every turn."""
        p = await _prompt(max_messages=None)
        assert "Participation so far" in p
        assert "fair share" not in p
        assert p.rstrip().endswith("Choose naturally based on conversation flow.")

    async def test_the_run_budget_is_used_not_the_per_call_turn_budget(self):
        """The deployed path generates ONE turn per Lambda invocation, so this is asserted
        by running that shape: `turn_budget=1` inside a 40-turn run. If the per-call budget
        reached the prompt, the fair share would read "1 turn each" on every turn of every
        run under Step Functions — a fairness signal that says the opposite of the truth."""
        prompts = []

        def fake(*args, **kwargs):
            text = " ".join(m["content"] for m in kwargs["messages"])
            if "conversation moderator" in text:
                prompts.append(text)
                return _Resp("Ada")
            return _Resp("a plain reply")

        seq = iter(range(1000))
        with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
            await simulator._run_turns(
                run_id="budget", topic="t", agents=_agents(),
                conversation=list(CONVERSATION), last_speaker="Ada",
                start_turn=3, max_messages=40, settings=get_settings(),
                db=None, emit=_noop_emit, next_seq=lambda: next(seq),
                turn_budget=1,
            )
        assert prompts, "no selection call captured"
        assert "runs for 40 turns" in prompts[0]
        assert "roughly 13 turns each" in prompts[0]


# --------------------------------------------------------------------------- #
# (4) it reaches every path, including the deployed one
# --------------------------------------------------------------------------- #


class TestEveryPathCarriesIt:
    """A config parsed only in `run_simulation` is dead in production: Step Functions runs
    each turn through `orchestration` → `resume_simulation`. Cognition shipped that way in
    v0.2 and was inert for six weeks."""

    def test_resume_simulation_accepts_a_selection_config(self):
        assert "selection" in inspect.signature(simulator.resume_simulation).parameters

    def test_run_turns_accepts_a_selection_config(self):
        assert "selection" in inspect.signature(simulator._run_turns).parameters

    def test_the_orchestrated_turn_path_passes_one(self):
        import matrix_studio.orchestration as orch

        src = inspect.getsource(orch)
        assert "selection=SelectionConfig.from_config(cfg)" in src

    def test_branching_passes_one_on_both_of_its_resume_calls(self):
        import matrix_studio.branching as branching

        src = inspect.getsource(branching)
        assert src.count("selection=SelectionConfig.from_config(") == 2

    def test_the_api_accepts_and_preserves_the_block(self):
        """`RunConfigModel` is strict, so an unmodelled key would be rejected and the
        switch would be unreachable through the HTTP API."""
        from matrix_studio.api.app import CreateRunModel

        body = CreateRunModel(
            topic="t",
            cast=[{"name": "Ada", "persona": "p", "goals": []}],
            config={"max_messages": 2, "selection": {"fairness": False}},
        )
        dumped = body.model_dump(exclude_none=True)
        assert dumped["config"]["selection"]["fairness"] is False
        assert SelectionConfig.from_config(dumped["config"]).fairness is False


# --------------------------------------------------------------------------- #
# (5) end to end through a run
# --------------------------------------------------------------------------- #

REQUEST = {
    "topic": "AI ethics",
    "cast": [
        {"name": "Ada", "persona": "ethicist", "goals": ["seek truth"]},
        {"name": "Ben", "persona": "engineer", "goals": ["ship safely"]},
    ],
}


async def _run(db, run_id, config):
    prompts = []

    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            prompts.append(text)
            return _Resp("Ada")
        return _Resp("a plain reply")

    from matrix_studio.engine import run_simulation

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 3, "generate_avatars": False, **config}
        await run_simulation(req, db=db, run_id=run_id)
    return prompts


async def test_a_default_run_shows_the_moderator_the_counts(db):
    prompts = await _run(db, "fair-on", {})
    assert prompts, "no selection calls captured"
    assert all("Participation so far" in p for p in prompts)
    # And the counts actually advance as the run proceeds, rather than being computed once.
    assert "- Ada: 0 turn(s) so far, has not spoken yet" in prompts[0]
    assert "- Ada: 1 turn(s) so far, last spoke 0 turn(s) ago" in prompts[1]


async def test_a_run_can_opt_out(db):
    prompts = await _run(db, "fair-off", {"selection": {"fairness": False}})
    assert prompts
    assert all("Participation so far" not in p for p in prompts)
