# SPDX-License-Identifier: Apache-2.0
"""
Phase 4d tests — the optional structured output view.

Covers:
  (1) OFF by default: the endpoint 403s without settings.structured_output or
      ?opt_in=true, and enabling the view changes NOTHING about the canonical
      event stream (derived view only).
  (2) Sourcing honesty: every Consequences/Updated-State line carries a
      source_seq that resolves to a REAL canonical event of this turn, and the
      Narrative utterance is the agent.response message verbatim.
  (3) Possibilities = open threads from the turn snapshot only; sections with
      no data say so (note) instead of inventing content.
  (4) Pure formatter unit behavior over synthetic events.
"""

import json
import time as _time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from matrix_studio.api.app import create_app
from matrix_studio.state import PendingThread, SimSnapshot, AgentState
from matrix_studio.structured_view import build_structured_view


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


REQUEST = {
    "topic": "AI ethics",
    "cast": [
        {"name": "Ada", "persona": "ethicist", "goals": ["seek truth"]},
        {"name": "Ben", "persona": "engineer", "goals": ["ship safely"]},
    ],
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        return {"name": "sv-run", "description": "t", "slug": "sv-run",
                "source": "llm"}

    monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.api.app.generate_run_name", fake_name)
    app = create_app(db_path=str(tmp_path / "sv.db"))
    with TestClient(app) as c:
        yield c


def _wait(client, ref, tries=300):
    for _ in range(tries):
        r = client.get(f"/api/runs/{ref}")
        if r.status_code == 200 and r.json()["status"] in ("complete", "failed"):
            return r.json()
        _time.sleep(0.02)
    return client.get(f"/api/runs/{ref}").json()


def _threads_fake():
    """Turn 1 plants a thread; later turns just talk (thread stays open)."""
    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Ada", "reason": "Ada leads"}))
        if "Unresolved threads" not in text:
            return _Resp(json.dumps({
                "utterance": "I suspect the audit hides something.",
                "rationale": "planting doubt",
                "goal_served": "seek truth",
                "thread_updates": {"open": [
                    {"description": "what the audit hides",
                     "thread_type": "setup"}], "resolved": [], "abandoned": []},
            }))
        return _Resp(json.dumps({
            "utterance": "We should keep digging into this.",
            "rationale": "keeping pressure",
            "goal_served": "seek truth",
            "thread_updates": {"open": [], "resolved": [], "abandoned": []},
        }))
    return fake


def _run_threads_sim(client, max_messages=2):
    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_threads_fake()):
        req = dict(REQUEST)
        req["config"] = {"max_messages": max_messages, "generate_avatars": False,
                         "cognition": {"enabled": True, "threads": True,
                                       "memory": False, "reflection_every": 0}}
        run_id = client.post("/api/runs", json=req).json()["run_id"]
        _wait(client, run_id)
    return run_id


# --------------------------------------------------------------------------- #
# (1) default OFF + no canonical change
# --------------------------------------------------------------------------- #

def test_structured_view_off_by_default_403(client):
    run_id = _run_threads_sim(client)
    r = client.get(f"/api/runs/{run_id}/turns/1/structured")
    assert r.status_code == 403


def test_structured_view_opt_in_does_not_touch_canonical_events(client):
    run_id = _run_threads_sim(client)
    before = client.get(f"/api/runs/{run_id}/events").json()["events"]
    r = client.get(f"/api/runs/{run_id}/turns/1/structured?opt_in=true")
    assert r.status_code == 200
    after = client.get(f"/api/runs/{run_id}/events").json()["events"]
    assert after == before  # derived view: zero writes


def test_structured_view_global_setting_enables(client, monkeypatch):
    from matrix_studio.settings import get_settings
    run_id = _run_threads_sim(client)
    monkeypatch.setattr(get_settings(), "structured_output", True)
    assert client.get(f"/api/runs/{run_id}/turns/1/structured").status_code == 200


# --------------------------------------------------------------------------- #
# (2) sourcing honesty: every line backed by a real event, verbatim narrative
# --------------------------------------------------------------------------- #

def test_every_line_backed_by_real_event_and_verbatim_narrative(client):
    run_id = _run_threads_sim(client)
    events = client.get(f"/api/runs/{run_id}/events").json()["events"]
    by_seq = {e["seq"]: e for e in events}

    for turn in (1, 2):
        view = client.get(
            f"/api/runs/{run_id}/turns/{turn}/structured?opt_in=true").json()

        # Narrative: verbatim agent.response message of THIS turn.
        for entry in view["narrative"]["entries"]:
            src = by_seq[entry["source_seq"]]
            assert src["event_type"] == "agent.response"
            assert src["turn"] == turn
            assert entry["utterance"] == src["payload"]["message"]

        # Consequences + state deltas: every single line resolves to a real
        # canonical event of this turn (no line may lack a backing event).
        lines = (view["consequences"]["immediate"]
                 + view["consequences"]["deferred"]
                 + view["updated_state"]["deltas"])
        for line in lines:
            assert "source_seq" in line, f"unsourced line: {line}"
            src = by_seq.get(line["source_seq"])
            assert src is not None, f"line cites nonexistent event: {line}"
            assert src["turn"] == turn

    # Turn 1 opened a thread -> it must appear as a DEFERRED consequence backed
    # by the thread.opened event.
    view1 = client.get(f"/api/runs/{run_id}/turns/1/structured?opt_in=true").json()
    deferred = view1["consequences"]["deferred"]
    assert len(deferred) == 1
    src = by_seq[deferred[0]["source_seq"]]
    assert src["event_type"] == "thread.opened"
    assert "what the audit hides" in deferred[0]["text"]


# --------------------------------------------------------------------------- #
# (3) possibilities from real ledger; absent data -> honest notes
# --------------------------------------------------------------------------- #

def test_possibilities_are_open_threads_and_absent_data_is_said(client):
    run_id = _run_threads_sim(client)
    view2 = client.get(f"/api/runs/{run_id}/turns/2/structured?opt_in=true").json()

    # The turn-1 thread is still open at turn 2 -> the only possibility.
    poss = view2["possibilities"]["open_threads"]
    assert len(poss) == 1
    assert poss[0]["description"] == "what the audit hides"
    assert view2["possibilities"]["non_limiting"] is True

    # Turn 2 changed no state -> honest notes, not invented content.
    assert view2["updated_state"]["deltas"] == []
    assert view2["updated_state"]["note"]
    assert view2["consequences"]["immediate"] == []
    assert view2["consequences"]["deferred"] == []
    assert view2["consequences"]["note"]


def test_unknown_turn_404(client):
    run_id = _run_threads_sim(client)
    r = client.get(f"/api/runs/{run_id}/turns/99/structured?opt_in=true")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# (4) pure formatter unit behavior
# --------------------------------------------------------------------------- #

def _ev(seq, etype, payload, turn=1, agent=None):
    return {"run_id": "r", "turn": turn, "seq": seq, "event_type": etype,
            "agent_name": agent, "payload": payload}


def test_formatter_folds_cognition_and_thread_events():
    events = [
        _ev(1, "agent.response", {"speaker": "Ada", "message": "Hello there."}),
        _ev(2, "goal.updated", {"agent": "Ada", "before": ["a"], "after": ["b"]}),
        _ev(3, "relationship.updated",
            {"agent": "Ada", "other": "Ben", "stance": "wary"}),
        _ev(4, "memory.formed", {"agent": "Ada", "content": "Ben hesitated."}),
        _ev(5, "thread.resolved",
            {"id": "abc123abc123", "description": "old setup", "origin_turn": 0}),
        _ev(6, "validation.flagged",
            {"principle": "agency", "reason": "negates choice"}),
    ]
    snap = SimSnapshot(
        run_id="r", turn=1, topic="t",
        agents={"Ada": AgentState(name="Ada", persona="p")},
        status="running", created_at=0, total_turns=1,
        pending_threads=[
            PendingThread(description="live setup", origin_turn=1),
            PendingThread(description="done", origin_turn=0, status="resolved"),
        ],
    )
    view = build_structured_view(1, events, snap)

    assert view["narrative"]["entries"][0]["utterance"] == "Hello there."
    imm_texts = [l["text"] for l in view["consequences"]["immediate"]]
    assert any("changed goals" in t for t in imm_texts)
    assert any("resolved" in t for t in imm_texts)
    assert any("flagged" in t for t in imm_texts)
    delta_texts = [l["text"] for l in view["updated_state"]["deltas"]]
    assert any("formed a memory" in t for t in delta_texts)
    # Only the OPEN thread appears as a possibility.
    assert [p["description"] for p in view["possibilities"]["open_threads"]] == ["live setup"]
    # Every line is sourced.
    for line in (view["consequences"]["immediate"]
                 + view["consequences"]["deferred"]
                 + view["updated_state"]["deltas"]):
        assert isinstance(line["source_seq"], int)


def test_formatter_empty_turn_is_honest():
    view = build_structured_view(3, [], None)
    assert view["narrative"]["entries"] == []
    assert view["narrative"]["note"]
    assert view["possibilities"]["open_threads"] == []
    assert view["possibilities"]["note"]
