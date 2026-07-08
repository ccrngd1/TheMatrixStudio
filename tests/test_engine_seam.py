# SPDX-License-Identifier: Apache-2.0
"""
Tests for the Phase 1 additive engine seam: the optional ``on_event`` callback
and the parallel ``avatar.ready`` emission. litellm and avatar generation are
MOCKED — no live calls.
"""

from unittest.mock import MagicMock, patch

import pytest

from matrix_studio.engine import run_simulation


class MockResp:
    def __init__(self, content, tin=10, tout=5):
        self.choices = [MagicMock(message=MagicMock(content=content))]
        self.usage = MagicMock(prompt_tokens=tin, completion_tokens=tout)
        self._hidden_params = {"response_cost": 0.001}


BASE_REQUEST = {
    "topic": "AI ethics",
    "cast": [
        {"name": "Ada", "persona": "ethicist", "goals": ["ask"]},
        {"name": "Ben", "persona": "engineer", "goals": ["build"]},
    ],
    "config": {"max_messages": 2, "generate_avatars": False},
}


@pytest.mark.asyncio
async def test_on_event_receives_all_events_in_order():
    events = []

    async def on_event(e):
        events.append(e)

    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock:
        mock.side_effect = [
            MockResp("Ada"), MockResp("Ada opens"),
            MockResp("Ben"), MockResp("Ben replies"),
        ]
        result = await run_simulation(BASE_REQUEST, db=None, on_event=on_event)

    assert result["status"] == "complete"
    types = [e["event_type"] for e in events]
    assert types[0] == "sim.started"
    assert types[-1] == "sim.completed"
    assert types.count("speaker.selected") == 2
    assert types.count("agent.response") == 2
    # Seq is monotonically increasing across the whole run.
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))


@pytest.mark.asyncio
async def test_avatar_ready_emitted_per_agent():
    events = []

    async def on_event(e):
        events.append(e)

    req = {**BASE_REQUEST, "config": {"max_messages": 1, "generate_avatars": True}}

    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock:
        mock.side_effect = [MockResp("Ada"), MockResp("Ada opens")]
        with patch("matrix_studio.engine.simulator.generate_avatar", return_value="B64IMG"):
            await run_simulation(req, db=None, on_event=on_event)

    avatar_events = [e for e in events if e["event_type"] == "avatar.ready"]
    assert len(avatar_events) == 2
    names = {e["payload"]["agent_name"] for e in avatar_events}
    assert names == {"Ada", "Ben"}
    assert all(e["payload"]["portrait_b64"] == "B64IMG" for e in avatar_events)


@pytest.mark.asyncio
async def test_avatar_failure_emits_null_portrait_and_run_completes():
    """If avatar generation returns None, run still completes and portrait is null."""
    events = []

    async def on_event(e):
        events.append(e)

    req = {**BASE_REQUEST, "config": {"max_messages": 1, "generate_avatars": True}}

    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock:
        mock.side_effect = [MockResp("Ada"), MockResp("Ada opens")]
        with patch("matrix_studio.engine.simulator.generate_avatar", return_value=None):
            result = await run_simulation(req, db=None, on_event=on_event)

    assert result["status"] == "complete"
    avatar_events = [e for e in events if e["event_type"] == "avatar.ready"]
    assert len(avatar_events) == 2
    assert all(e["payload"]["portrait_b64"] is None for e in avatar_events)


@pytest.mark.asyncio
async def test_failing_callback_does_not_break_run():
    """An exception in on_event must never abort the simulation."""

    async def bad_callback(e):
        raise RuntimeError("subscriber died")

    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock:
        mock.side_effect = [
            MockResp("Ada"), MockResp("Ada opens"),
            MockResp("Ben"), MockResp("Ben replies"),
        ]
        result = await run_simulation(BASE_REQUEST, db=None, on_event=bad_callback)

    assert result["status"] == "complete"
    assert result["total_turns"] == 2


@pytest.mark.asyncio
async def test_no_on_event_is_backward_compatible():
    """Without on_event, behavior is exactly Phase 0 (no crash, same result)."""
    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mock:
        mock.side_effect = [
            MockResp("Ada"), MockResp("Ada opens"),
            MockResp("Ben"), MockResp("Ben replies"),
        ]
        result = await run_simulation(BASE_REQUEST, db=None)
    assert result["status"] == "complete"
    assert result["total_turns"] == 2
