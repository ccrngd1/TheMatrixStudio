# SPDX-License-Identifier: Apache-2.0
"""
The JSON contract has to be the last thing the model reads.

`response_format={"type": "json_object"}` is advisory on the Bedrock path, not a constraint.
Measured 2026-09-14 against `global.anthropic.claude-sonnet-5`, using the engine's own prompt
inside the deployed image against real Bedrock:

    no retrieved passages in the prompt      12/12 replies were JSON
    with retrieved passages                   4/8  replies were JSON
    with retrieved passages + the reminder     8/8  replies were JSON

The retrieved source material is what tips it: a few thousand characters of quoted documents
followed by "Respond as <name>:" and the model answers in prose, like the transcript it just
read. On run `3abd39b3` that cost cognition 20 of 24 turns — configured on, and discarded.

These tests assert the reminder is present when cognition is on and absent when it is off.
They cannot assert the model's behaviour; the numbers above are the evidence for that, and
`cognition_parsed` on `agent.response` is how a future run reports it.
"""

from unittest.mock import MagicMock, patch

import pytest

from matrix_studio.engine import simulator
from matrix_studio.state import AgentState, CognitionConfig
from matrix_studio.settings import get_settings

pytestmark = pytest.mark.asyncio

CONVERSATION = [{"speaker": "Ada", "content": "the policy says egress is inspected"}]


def _agent() -> AgentState:
    return AgentState(name="Bo", persona="a careful SRE", goals=["find what breaks"])


class _Resp:
    def __init__(self, content):
        self.choices = [MagicMock(message=MagicMock(content=content))]
        self.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        self._hidden_params = {"response_cost": 0.0}


async def _messages_for(cognition):
    """Run one generation and return the messages the engine actually sent."""
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return _Resp('{"utterance": "fine", "rationale": "because"}')

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        await simulator._generate_response(
            "Bo", _agent(), "egress inspection", CONVERSATION, get_settings(),
            cognition=cognition,
        )
    return seen


async def test_the_reminder_is_the_last_thing_in_the_prompt_when_cognition_is_on():
    seen = await _messages_for(CognitionConfig(enabled=True))
    user = seen["messages"][-1]["content"]
    assert user.rstrip().endswith('no prose outside the object.'), user[-120:]
    # And the structured request itself is still being made.
    assert seen["response_format"] == {"type": "json_object"}


async def test_it_names_the_field_the_speech_belongs_in():
    """"Return JSON" alone leaves the model to guess where its words go, and the failure
    mode is a reply that is valid JSON with the utterance in the wrong key."""
    seen = await _messages_for(CognitionConfig(enabled=True))
    assert '"utterance"' in seen["messages"][-1]["content"]


async def test_a_cognition_off_run_gets_no_reminder_and_no_response_format():
    """Cognition-off prompts must stay byte-for-byte as they were: that is the property
    that makes the whole feature additive, and it is asserted elsewhere too."""
    seen = await _messages_for(CognitionConfig(enabled=False))
    user = seen["messages"][-1]["content"]
    assert "JSON" not in user, user[-200:]
    assert "response_format" not in seen


async def test_the_conversation_still_ends_with_the_speaker_cue():
    """The reminder is appended, not substituted. Dropping "Respond as <name>:" would
    change what the model is being asked to do, which is not what this fixes."""
    seen = await _messages_for(CognitionConfig(enabled=True))
    user = seen["messages"][-1]["content"]
    assert "Respond as Bo:" in user
    assert user.index("Respond as Bo:") < user.index("Return ONLY the JSON object")
