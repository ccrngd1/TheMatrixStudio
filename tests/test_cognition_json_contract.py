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
    # And the structured request itself is still being made. Asserted loosely here because
    # WHICH structured request is the subject of `TestTheSchemaIsTheActualConstraint`
    # below; this test is about the reminder surviving alongside it.
    assert seen["response_format"]["type"] in ("json_object", "json_schema")


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

class TestTheSchemaIsTheActualConstraint:
    """`json_object` is advisory on Bedrock; a json_schema is converted to a forced tool.

    Measured 2026-09-14, 5 samples per arm on the persona that fails hardest:
    instruction-only 0/5, + user reminder 5/5, + json_schema 5/5, forced tool use 5/5,
    assistant prefill rejected by the API. The schema is kept AND the reminder, because the
    reminder is what remains if `drop_params` ever removes the schema.
    """

    async def test_a_json_schema_is_sent_not_a_bare_json_object(self):
        seen = await _messages_for(CognitionConfig(enabled=True))
        rf = seen["response_format"]
        assert rf["type"] == "json_schema", rf
        assert rf["json_schema"]["schema"]["type"] == "object"

    async def test_only_the_utterance_is_required(self):
        """The cognition fields are genuinely optional — a turn may form no memories —
        and requiring them would invite the model to invent one."""
        seen = await _messages_for(CognitionConfig(enabled=True))
        assert seen["response_format"]["json_schema"]["schema"]["required"] == ["utterance"]

    async def test_the_schema_carries_exactly_the_enabled_features(self):
        """The prompt's field list and the schema are built in the same branches; if they
        drift, a model is told to send a field the schema does not describe."""
        on = await _messages_for(CognitionConfig(
            enabled=True, memory=True, goals_dynamic=True, relationships=True, threads=True,
        ))
        props = on["response_format"]["json_schema"]["schema"]["properties"]
        assert set(props) == {
            "utterance", "rationale", "goal_served", "memories", "goal_update",
            "relationship_updates", "thread_updates",
        }

        off = await _messages_for(CognitionConfig(
            enabled=True, memory=False, goals_dynamic=False, relationships=False, threads=False,
        ))
        assert set(off["response_format"]["json_schema"]["schema"]["properties"]) == {
            "utterance", "rationale", "goal_served",
        }

    async def test_the_prompt_and_the_schema_agree_on_the_field_names(self):
        """Read the schema field names back out of the prompt text, so a field added to one
        and not the other fails here rather than in a run."""
        seen = await _messages_for(CognitionConfig(
            enabled=True, memory=True, goals_dynamic=True, relationships=True,
        ))
        system = seen["messages"][0]["content"]
        for name in seen["response_format"]["json_schema"]["schema"]["properties"]:
            assert f'"{name}"' in system, f"{name} is in the schema but not in the prompt"

    async def test_a_permissive_schema_so_an_extra_field_is_not_an_error(self):
        seen = await _messages_for(CognitionConfig(enabled=True))
        schema = seen["response_format"]["json_schema"]["schema"]
        assert "additionalProperties" not in schema or schema["additionalProperties"] is not False
        assert schema.get("strict") is not True
