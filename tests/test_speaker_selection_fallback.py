# SPDX-License-Identifier: Apache-2.0
"""
A fallback speaker is a degradation, and it has to look like one.

`_select_next_speaker` used to wrap its whole body in `except Exception` and return
`candidates[0]` on any failure. Two consequences, both found by reading the code while
building `scripts/eval_speaker_selection.py`:

1. **A programming error became a choice.** Calling it with `agents` and `topic` swapped
   raised inside the function, was caught, and returned a speaker — no model call, no
   error logged as such, a plausible pick on every turn for ever.
2. **The fallback was silent and cast-position biased.** `candidates[0]` is the same
   person every time for a given last speaker, so the turn-share skew measured in
   `docs/SPEAKER-SELECTION-EVALUATION.md` could not be attributed between the model and
   the fallback — which is why these fixes land BEFORE any further arm is measured.

These tests pin the boundary: our own defects raise, external failures degrade loudly.
"""

import json
import logging
import random
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio.engine import run_simulation
from matrix_studio.engine.simulator import _select_next_speaker
from matrix_studio.settings import get_settings
from matrix_studio.state import AgentState, CognitionConfig

pytestmark = pytest.mark.asyncio


class _Resp:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [
            MagicMock(message=MagicMock(content=content), finish_reason=finish_reason)
        ]
        self.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        self._hidden_params = {"response_cost": 0.0}


def _cast(*names):
    return {n: AgentState(name=n, persona="p", goals=["g"]) for n in names}


CONVERSATION = [{"speaker": "Ada", "content": "the schedule slipped"}]


async def _select(reply, cast=None, last_speaker="Ada", cognition=None,
                  finish_reason="stop", seen=None, **kw):
    cast = cast if cast is not None else _cast("Ada", "Bo", "Cy")

    async def fake(**kwargs):
        if seen is not None:
            seen.update(kwargs)
        if isinstance(reply, Exception):
            raise reply
        return _Resp(reply, finish_reason=finish_reason)

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        return await _select_next_speaker(
            "the schedule", cast, CONVERSATION, last_speaker, get_settings(),
            cognition=cognition, **kw,
        )


# --------------------------------------------------------------------------- #
# (1) our own errors surface
# --------------------------------------------------------------------------- #


async def test_swapped_arguments_raise_instead_of_returning_a_speaker():
    """The exact mistake that motivated this: (agents, topic) instead of (topic, agents).

    A TypeError here is recoverable by the programmer. A fallback speaker is not — it is
    indistinguishable from the moderator making an odd but defensible pick.
    """
    agents = _cast("Ada", "Bo")
    with pytest.raises(TypeError) as err:
        await _select_next_speaker(
            agents, "the schedule", CONVERSATION, "Ada", get_settings(),
        )
    assert "agents must be a dict" in str(err.value)
    assert "str" in str(err.value), "the message should name what was passed"


async def test_an_empty_cast_raises_rather_than_inventing_a_speaker():
    with pytest.raises(ValueError):
        await _select_next_speaker(
            "the schedule", {}, CONVERSATION, None, get_settings(),
        )


async def test_a_defect_in_our_parsing_is_not_laundered_into_a_fallback():
    """Only the provider call is guarded. If the code AROUND it breaks, the run stops.

    Mutation target: widening the `try` back over the parse and match would make this
    return a speaker, which is the whole defect being fixed.
    """
    boom = RuntimeError("extract_json_object is broken")
    with patch("matrix_studio.engine.simulator.extract_json_object", side_effect=boom):
        with pytest.raises(RuntimeError):
            await _select(
                json.dumps({"speaker": "Bo", "reason": "Bo was asked"}),
                cognition=CognitionConfig(enabled=True),
            )


# --------------------------------------------------------------------------- #
# (2) external failures degrade, loudly and attributably
# --------------------------------------------------------------------------- #


async def test_a_provider_failure_still_yields_a_turn_but_names_the_fallback():
    """A throttle or an expired credential is external; the run should continue. What it
    must not do is present the drawn name as a decision."""
    choice = await _select(RuntimeError("throttled"))
    assert choice.name in {"Bo", "Cy"}, "never the last speaker"
    assert choice.fallback == "call_failed"
    assert choice.reason is None


async def test_a_reply_naming_nobody_in_the_cast_is_recorded_as_unresolved():
    choice = await _select("whoever feels moved to speak")
    assert choice.name in {"Bo", "Cy"}
    assert choice.fallback == "unresolved"


async def test_the_moderators_reason_survives_an_unresolved_name():
    """The reason says what the moderator was trying to do and is worth keeping; the
    NAME is ours, and `fallback` is what says so."""
    choice = await _select(
        json.dumps({"speaker": "Quill", "reason": "the CFO should answer"}),
        cognition=CognitionConfig(enabled=True),
    )
    assert choice.reason == "the CFO should answer"
    assert choice.fallback == "unresolved"


@pytest.mark.xfail(
    strict=True,
    reason="intervention F in docs/SPEAKER-SELECTION-EVALUATION.md: `_match` scans the "
           "cast in order for a SUBSTRING, so a name that merely contains a cast name "
           "resolves to that cast member. Found by writing the test above, where the "
           "moderator naming 'Nobody At All' resolved to 'Bo'. Out of scope for this "
           "change (the fallback, not the resolver); delete the marker when F lands.",
)
async def test_a_name_that_merely_contains_a_cast_name_should_not_resolve():
    choice = await _select(
        json.dumps({"speaker": "Nobody At All", "reason": "whoever"}),
        cognition=CognitionConfig(enabled=True),
    )
    assert choice.fallback == "unresolved", f"resolved to {choice.name!r}"


async def test_a_real_decision_carries_no_fallback_marker():
    choice = await _select(
        json.dumps({"speaker": "Cy", "reason": "Cy owns the schedule"}),
        cognition=CognitionConfig(enabled=True),
    )
    assert (choice.name, choice.reason, choice.fallback) == (
        "Cy", "Cy owns the schedule", None,
    )


async def test_an_empty_model_reply_does_not_crash_the_turn():
    """`message.content` is None on some provider errors. That used to raise inside the
    broad try and reach the same fallback by accident; now it is explicit."""
    choice = await _select(None)
    assert choice.fallback == "unresolved"


async def test_every_fallback_logs_a_warning_naming_the_speaker_and_the_cause(caplog):
    with caplog.at_level(logging.WARNING, logger="matrix_studio.engine.simulator"):
        choice = await _select(RuntimeError("throttled"))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a silent fallback is the defect"
    text = warnings[-1].getMessage()
    assert "call_failed" in text and choice.name in text
    assert "No model made this choice." in text


# --------------------------------------------------------------------------- #
# (2b) the output cap that made a bigger model look like it was refusing
# --------------------------------------------------------------------------- #


class TestThereIsNoOutputCap:
    """The selection call sent `max_tokens=120` (50 with cognition off) until 2026-09-15.

    Measured in `docs/SELECTION-MODEL-DEFAULT.md` §6: a reply that hits the cap returns
    `finish_reason="length"`, 120 completion tokens and **empty content** — not a partial
    object — so nothing can be matched and the fallback fires. Haiku averaged 75 tokens
    against that cap and Sonnet 5 averaged 80–87, so the cap cost Sonnet 43% of its picks
    in replay while looking survivable on Haiku's ~45 tokens of headroom.
    """

    async def test_the_call_sends_no_max_tokens_with_cognition_on(self):
        seen = {}
        await _select('{"speaker": "Bo", "reason": "r"}',
                      cognition=CognitionConfig(enabled=True), seen=seen)
        assert "max_tokens" not in seen, seen.get("max_tokens")

    async def test_the_call_sends_no_max_tokens_with_cognition_off(self):
        """The cognition-off path had the tighter cap of the two (50 tokens)."""
        seen = {}
        await _select("Bo", seen=seen)
        assert "max_tokens" not in seen, seen.get("max_tokens")

    async def test_a_truncated_reply_is_recorded_as_truncated_not_unresolved(self):
        """Opposite fixes: `truncated` means the model ran out of room mid-answer,
        `unresolved` means it named somebody who is not in the cast. Recording both as
        `unresolved` is what hid an output-budget problem inside a prompt problem."""
        choice = await _select("", finish_reason="length",
                               cognition=CognitionConfig(enabled=True))
        assert choice.fallback == "truncated"
        assert choice.name in {"Bo", "Cy"}

    async def test_a_name_outside_the_cast_is_still_unresolved(self):
        choice = await _select('{"speaker": "Quill", "reason": "r"}', finish_reason="stop",
                               cognition=CognitionConfig(enabled=True))
        assert choice.fallback == "unresolved"

    async def test_a_truncated_reply_that_still_named_somebody_is_a_real_pick(self):
        """Truncation only matters if it cost us the name. A cut-off reply whose speaker
        field survived is a decision, and marking it as a fallback would overcount."""
        choice = await _select('{"speaker": "Cy", "reason": "half a sen',
                               finish_reason="length",
                               cognition=CognitionConfig(enabled=True))
        assert (choice.name, choice.fallback) == ("Cy", None)


# --------------------------------------------------------------------------- #
# (3) no cast-position bias
# --------------------------------------------------------------------------- #


async def test_the_fallback_does_not_always_pick_the_same_person():
    """`candidates[0]` handed every failed turn to whoever the cast listed first, which
    reads in the turn shares exactly like a persona the moderator favours.

    Seeded so the assertion is deterministic; the property under test is that more than
    one candidate can win.
    """
    random.seed(20260915)
    picks = {
        (await _select(RuntimeError("throttled"), cast=_cast("Ada", "Bo", "Cy", "Di"))).name
        for _ in range(40)
    }
    assert picks == {"Bo", "Cy", "Di"}, picks


async def test_the_fallback_never_repeats_the_last_speaker():
    """Self-repetition is 0 in every measured run and is a committed guardrail in the
    evaluation doc: the fallback must not be what breaks it."""
    random.seed(7)
    for _ in range(20):
        choice = await _select(RuntimeError("x"), last_speaker="Bo")
        assert choice.name != "Bo"


async def test_a_single_agent_cast_still_gets_a_speaker():
    """The degenerate case: with one persona there is no alternative to the last speaker,
    and returning nobody would stall the run."""
    choice = await _select(RuntimeError("x"), cast=_cast("Solo"), last_speaker="Solo")
    assert choice.name == "Solo"
    assert choice.fallback == "call_failed"


# --------------------------------------------------------------------------- #
# (4) the transcript records it
# --------------------------------------------------------------------------- #

REQUEST = {
    "topic": "AI ethics",
    "cast": [
        {"name": "Ada", "persona": "ethicist", "goals": ["seek truth"]},
        {"name": "Ben", "persona": "engineer", "goals": ["ship safely"]},
    ],
}


async def _speaker_events(db, run_id):
    rows = await db.get_events(run_id)
    out = []
    for r in rows:
        if r["event_type"] != "speaker.selected":
            continue
        payload = r["payload"]
        out.append(json.loads(payload) if isinstance(payload, str) else payload)
    return out


async def test_speaker_selected_carries_selection_fallback_when_nobody_chose(db):
    """End to end: a moderator reply naming nobody must be visible in the transcript,
    not merely in a log line that nothing is reading a week later."""
    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp("someone suitable")  # names no cast member
        return _Resp("a plain reply")

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 2, "generate_avatars": False}
        await run_simulation(req, db=db, run_id="sel-fb")

    events = await _speaker_events(db, "sel-fb")
    assert events and all(e["selection_fallback"] == "unresolved" for e in events)


async def test_a_healthy_run_has_no_selection_fallback_key(db):
    """Additive-only, like `reason`: the key's PRESENCE is the signal, so it must be
    absent when selection worked."""
    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp("Ada")
        return _Resp("a plain reply")

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 2, "generate_avatars": False}
        await run_simulation(req, db=db, run_id="sel-ok")

    events = await _speaker_events(db, "sel-ok")
    assert events and all("selection_fallback" not in e for e in events)
