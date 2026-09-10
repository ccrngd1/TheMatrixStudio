# SPDX-License-Identifier: Apache-2.0
"""
Stopping a live run: POST /api/runs/{ref}/stop.

Two decisions define the behaviour and are what these tests defend:

- **The in-flight turn finishes and is persisted.** Cancelling mid-call would throw
  away tokens already paid for and leave a partial turn for a later resume to trim.
  So the stop is a request polled between turns, not a cancellation.
- **`stopped` is its own terminal status**, resumable exactly like `interrupted` but
  distinguishable from it, so a run list still shows whether someone chose to end a
  run or the process died under it.
"""

import asyncio
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from matrix_studio.api.app import create_app
from matrix_studio.branching import RESUMABLE_STATUSES


@pytest.fixture(autouse=True)
def _storage_backend(aws_backend):
    """Every test in this file builds the FastAPI app.

    The app's lifespan connects to DynamoDB, so without a mocked account it reaches
    real AWS — which surfaces as `ExpiredTokenException` on a `Scan` and reads like a
    credentials problem rather than a missing fixture. Autouse and explicit here
    rather than hidden in `conftest.py`, so the dependency is visible in the file that
    has it.
    """


class _Resp:
    def __init__(self, content):
        self.choices = [MagicMock(message=MagicMock(content=content))]
        self.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        self._hidden_params = {"response_cost": 0.001}


def _slow_llm(delay=0.12):
    """Paced so a stop request can land while a turn is genuinely in flight."""
    def fake(*args, **kwargs):
        time.sleep(delay)
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Ada", "reason": "her turn"}))
        return _Resp("A turn of conversation.")
    return fake


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        return {"name": "stoppable", "description": "d", "slug": "stoppable",
                "source": "llm"}

    for target in ("matrix_studio.api.manager.generate_run_name",
                   "matrix_studio.api.app.generate_run_name",
                   "matrix_studio.branching.generate_run_name"):
        monkeypatch.setattr(target, fake_name)
    app = create_app(db_path=str(tmp_path / "stop.db"))
    with TestClient(app) as c:
        yield c


REQUEST = {
    "topic": "Should we ship it?",
    "cast": [
        {"name": "Ada", "persona": "an ethicist", "goals": ["seek truth"]},
        {"name": "Ben", "persona": "an engineer", "goals": ["ship safely"]},
    ],
    # Long enough that a stop must be what ends it, not the budget.
    "config": {"max_messages": 40, "generate_avatars": False},
}


def _wait_for(client, ref, statuses, tries=600):
    for _ in range(tries):
        body = client.get(f"/api/runs/{ref}").json()
        if body["status"] in statuses:
            return body
        time.sleep(0.02)
    return client.get(f"/api/runs/{ref}").json()


def _start(client):
    run_id = client.post("/api/runs", json=REQUEST).json()["run_id"]
    # Wait until at least one turn exists, so the stop lands mid-run.
    for _ in range(600):
        body = client.get(f"/api/runs/{run_id}").json()
        if body["turn_count"] >= 1 or body["status"] != "running":
            break
        time.sleep(0.02)
    return run_id


def test_stop_ends_the_run_in_the_stopped_status(client):
    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_slow_llm()):
        run_id = _start(client)
        r = client.post(f"/api/runs/{run_id}/stop")
        assert r.status_code == 202, r.text
        assert r.json() == {"run_id": run_id, "status": "stopping",
                            "stop_requested": True}
        body = _wait_for(client, run_id, {"stopped", "complete", "failed"})

    assert body["status"] == "stopped", body
    # Well short of the 40-turn budget: the stop is what ended it.
    assert body["turn_count"] < 40


def test_the_turn_in_flight_is_finished_and_kept(client):
    """
    Nothing generated is thrown away, and the log has no partial tail.

    This is the whole reason the stop is polled between turns rather than
    cancelling the task: the tokens for the current turn are already spent.
    """
    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_slow_llm()):
        run_id = _start(client)
        before = client.get(f"/api/runs/{run_id}").json()["turn_count"]
        client.post(f"/api/runs/{run_id}/stop")
        body = _wait_for(client, run_id, {"stopped", "complete", "failed"})

    assert body["status"] == "stopped"
    # No turn was lost, and at most a turn or two more landed.
    assert body["turn_count"] >= before

    events = client.get(f"/api/runs/{run_id}/events").json()["events"]
    responses = [e for e in events if e["event_type"] == "agent.response"]
    assert len(responses) == body["turn_count"], (
        "every counted turn must have its response event persisted"
    )
    # The terminal marker is last, and it is the stop marker.
    assert events[-1]["event_type"] == "sim.stopped"
    stopped = events[-1]["payload"]
    assert stopped["total_turns"] == body["turn_count"]
    assert stopped["total_cost_usd"] > 0, "cost accrued before the stop is reported"


def test_a_stopped_run_keeps_its_transcript_and_a_checkpoint(client):
    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_slow_llm()):
        run_id = _start(client)
        client.post(f"/api/runs/{run_id}/stop")
        _wait_for(client, run_id, {"stopped", "complete", "failed"})

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "stopped"
    # The final snapshot is readable, so the run can be inspected and branched.
    assert detail["result"] is not None
    assert detail["result"]["conversation"], "a stopped run must keep its transcript"
    assert detail["result"]["total_turns"] == detail["turn_count"]


def test_a_stopped_run_can_be_resumed(client):
    """The status exists so a stop is recoverable, not just recorded."""
    assert "stopped" in RESUMABLE_STATUSES

    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_slow_llm()):
        run_id = _start(client)
        client.post(f"/api/runs/{run_id}/stop")
        stopped = _wait_for(client, run_id, {"stopped", "complete", "failed"})
        assert stopped["status"] == "stopped"
        turns_at_stop = stopped["turn_count"]

        assert client.post(f"/api/runs/{run_id}/resume").status_code == 200
        resumed = _wait_for(client, run_id, {"complete", "stopped", "failed"}, tries=900)

    # Same run id, and it moved forward rather than restarting.
    assert resumed["run_id"] == run_id
    assert resumed["turn_count"] > turns_at_stop, (
        "resume must continue past the stop, not re-run it"
    )


def test_resuming_does_not_inherit_the_earlier_stop(client):
    """
    The stop request is cleared when the run ends.

    Left behind, it would stop the run again one turn into every later resume,
    which reads as resume silently not working.
    """
    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_slow_llm()):
        run_id = _start(client)
        client.post(f"/api/runs/{run_id}/stop")
        first = _wait_for(client, run_id, {"stopped", "complete", "failed"})
        assert first["status"] == "stopped"

        client.post(f"/api/runs/{run_id}/resume")
        after = _wait_for(client, run_id, {"complete", "stopped", "failed"}, tries=900)

    assert after["status"] != "stopped", (
        "the resumed run stopped again, so the stale stop request was not cleared"
    )
    assert after["turn_count"] > first["turn_count"]


def test_stopping_twice_is_not_an_error(client):
    """A second click on a button whose effect takes a turn is expected."""
    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_slow_llm()):
        run_id = _start(client)
        assert client.post(f"/api/runs/{run_id}/stop").status_code == 202
        assert client.post(f"/api/runs/{run_id}/stop").status_code == 202
        body = _wait_for(client, run_id, {"stopped", "complete", "failed"})
    assert body["status"] == "stopped"


def test_stopping_an_already_finished_run_is_409_not_a_silent_no_op(client):
    """
    Accepting a request that can never take effect would be the worse answer.

    A 202 here would tell the operator their stop was registered when there is
    nothing left to stop.
    """
    def quick(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Ada", "reason": "x"}))
        return _Resp("Short.")

    body = dict(REQUEST, config={"max_messages": 1, "generate_avatars": False})
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=quick):
        run_id = client.post("/api/runs", json=body).json()["run_id"]
        _wait_for(client, run_id, {"complete", "failed", "stopped"})

    r = client.post(f"/api/runs/{run_id}/stop")
    assert r.status_code == 409
    assert "nothing to stop" in r.json()["detail"]


def test_stopping_an_unknown_run_is_404(client):
    assert client.post("/api/runs/no-such-run/stop").status_code == 404


def test_a_stopped_run_gets_no_auto_summary(client):
    """
    Stopping is a request to stop spending, so it must not trigger a summary.

    The auto-summary is gated on `complete`; a stopped run reaching it would spend
    tokens immediately after the operator asked to stop spending them.
    """
    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_slow_llm()):
        run_id = _start(client)
        client.post(f"/api/runs/{run_id}/stop")
        _wait_for(client, run_id, {"stopped", "complete", "failed"})

    summary = client.get(f"/api/runs/{run_id}").json()["summary"]
    assert summary["generated"] is None, "a stopped run must not auto-summarise"


def test_sim_stopped_closes_the_live_stream(client):
    """
    The WebSocket must close on a stop, like any other terminal event.

    Otherwise the UI keeps showing a live run that has ended.
    """
    from matrix_studio.api.manager import TERMINAL_EVENTS

    assert "sim.stopped" in TERMINAL_EVENTS

    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_slow_llm()):
        run_id = _start(client)
        client.post(f"/api/runs/{run_id}/stop")
        _wait_for(client, run_id, {"stopped", "complete", "failed"})

    with client.websocket_connect(f"/api/runs/{run_id}/stream") as ws:
        seen = []
        try:
            for _ in range(400):
                seen.append(ws.receive_json()["event_type"])
        except Exception:
            pass
    assert "sim.stopped" in seen


def test_a_resumed_run_can_be_stopped_again(client):
    """
    Stop works on a resumed run too, not just a fresh one.

    Resume goes through a different entry point (`resume_run_in_place` ->
    `resume_simulation`) than a fresh start, so the predicate has to be forwarded
    down both. Without this test that forwarding could be dropped and everything
    else here still passed — which is exactly what a mutation check showed.
    """
    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_slow_llm()):
        run_id = _start(client)
        client.post(f"/api/runs/{run_id}/stop")
        first = _wait_for(client, run_id, {"stopped", "complete", "failed"})
        assert first["status"] == "stopped"

        # Resume, let it get going, then stop the RESUMED generation.
        assert client.post(f"/api/runs/{run_id}/resume").status_code == 200
        for _ in range(600):
            body = client.get(f"/api/runs/{run_id}").json()
            if body["turn_count"] > first["turn_count"] or body["status"] != "running":
                break
            time.sleep(0.02)

        r = client.post(f"/api/runs/{run_id}/stop")
        assert r.status_code == 202, r.text
        second = _wait_for(client, run_id, {"stopped", "complete", "failed"}, tries=900)

    assert second["status"] == "stopped", (
        "the resumed run ignored the stop, so should_stop was not forwarded "
        "through the resume path"
    )
    assert second["turn_count"] > first["turn_count"]
    # Well short of the budget again: the second stop is what ended it.
    assert second["turn_count"] < 40
