# SPDX-License-Identifier: Apache-2.0
"""
Phase 2a API tests — snapshot routes + branch primitive over the real
service/engine with LLM calls MOCKED.

Unlike test_api.py (which patches run_simulation with a fake), these exercise
the REAL engine + branching service through the API, mocking only the litellm
seam, so per-turn checkpointing, event copy, and forward resume are verified
end-to-end. The core assertion is the IMMUTABILITY INVARIANT: a branch never
modifies or re-runs the parent (events/snapshots/cost byte-for-byte unchanged).
"""

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from matrix_studio.api.app import create_app


class MockLiteLLMResponse:
    def __init__(self, content: str, tokens_in: int = 100, tokens_out: int = 50):
        self.choices = [MagicMock(message=MagicMock(content=content))]
        self.usage = MagicMock(prompt_tokens=tokens_in, completion_tokens=tokens_out)
        self._hidden_params = {"response_cost": 0.001}


def _always(content_cycle):
    """A litellm side-effect that never runs dry: selects a name / emits text on
    a repeating cycle so both fresh runs and resumed branches have enough."""
    names = content_cycle

    def _factory(*args, **kwargs):
        # Alternate: odd calls select a speaker, even calls generate a message.
        _factory.n += 1
        if _factory.n % 2 == 1:
            return MockLiteLLMResponse(names[(_factory.n // 2) % len(names)])
        return MockLiteLLMResponse(f"reply {_factory.n}")

    _factory.n = 0
    return _factory


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")

    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        base = "trusted-robot"
        name = base
        n = 2
        while name_exists is not None and await _maybe(name_exists(name)):
            name = f"{base}-{n}"
            n += 1
        return {"name": name, "description": "A test simulation", "slug": name, "source": "llm"}

    async def _maybe(v):
        if hasattr(v, "__await__"):
            return await v
        return v

    monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.api.app.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.branching.generate_run_name", fake_name)

    app = create_app(db_path=db_path)
    with TestClient(app) as c:
        yield c


REQUEST = {
    "topic": "AI ethics and responsibility",
    "cast": [
        {"name": "Ada", "persona": "An ethicist", "goals": ["Question assumptions"]},
        {"name": "Ben", "persona": "An engineer", "goals": ["Ship safely"]},
    ],
    "config": {"max_messages": 4, "generate_avatars": False},
}


def _wait_status(client, ref, statuses=("complete", "failed"), tries=200):
    for _ in range(tries):
        r = client.get(f"/api/runs/{ref}")
        if r.status_code == 200 and r.json()["status"] in statuses:
            return r.json()
        time.sleep(0.02)
    return client.get(f"/api/runs/{ref}").json()


def _make_run(client, request=REQUEST):
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_always(["Ada", "Ben"]),
    ):
        run_id = client.post("/api/runs", json=request).json()["run_id"]
        _wait_status(client, run_id)
    return run_id


def test_snapshots_list_and_get(client):
    run_id = _make_run(client)
    snaps = client.get(f"/api/runs/{run_id}/snapshots").json()["snapshots"]
    assert [s["turn"] for s in snaps] == [1, 2, 3, 4]
    assert snaps[-1]["status"] == "complete"

    # Full state at a turn.
    r = client.get(f"/api/runs/{run_id}/snapshots/2")
    assert r.status_code == 200
    body = r.json()
    assert body["turn"] == 2
    assert len(body["conversation"]) == 2
    assert set(body["agents"].keys()) == {"Ada", "Ben"}

    # Unknown turn → 404.
    assert client.get(f"/api/runs/{run_id}/snapshots/999").status_code == 404


def test_snapshots_404_for_unknown_run(client):
    assert client.get("/api/runs/nope/snapshots").status_code == 404
    assert client.get("/api/runs/nope/snapshots/1").status_code == 404


def _parent_fingerprint(client, run_id):
    """Capture the parent's events + snapshots + cost for the invariant check."""
    events = client.get(f"/api/runs/{run_id}/events").json()["events"]
    detail = client.get(f"/api/runs/{run_id}").json()
    snaps = client.get(f"/api/runs/{run_id}/snapshots").json()["snapshots"]
    return {
        "events": events,
        "snapshots": snaps,
        "cost": detail["total_cost_usd"],
        "turn_count": detail["turn_count"],
    }


def test_branch_is_nonblocking_and_returns_immediately(client):
    run_id = _make_run(client)
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_always(["Ada", "Ben"]),
    ):
        r = client.post(f"/api/runs/{run_id}/branch", json={"from_turn": 2})
        assert r.status_code == 201
        meta = r.json()
        # Returns immediately with the new run id + codename, status running.
        assert meta["run_id"] != run_id
        assert meta["name"]
        assert meta["parent_run_id"] == run_id
        assert meta["branch_turn"] == 2
        assert meta["status"] == "running"
        _wait_status(client, meta["run_id"])


def test_branch_resumes_forward_and_parent_unchanged(client):
    run_id = _make_run(client)
    before = _parent_fingerprint(client, run_id)

    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_always(["Ada", "Ben"]),
    ):
        meta = client.post(
            f"/api/runs/{run_id}/branch", json={"from_turn": 2}
        ).json()
        branch_id = meta["run_id"]
        branch_detail = _wait_status(client, branch_id)

    # Branch generated forward to its own completion as a new timeline.
    assert branch_detail["status"] == "complete"
    assert branch_detail["turn_count"] >= 3  # at least one new turn past turn 2

    # Branch events replay identically to the parent up to the fork (turns 1..2),
    # then diverge.
    branch_events = client.get(f"/api/runs/{branch_id}/events").json()["events"]
    parent_upto2 = [e for e in before["events"] if e["turn"] <= 2]
    branch_upto2 = [e for e in branch_events if e["turn"] <= 2]
    # Same (turn, seq, event_type, payload) up to the fork.
    def _key(e):
        return (e["turn"], e["seq"], e["event_type"], e["agent_name"])
    assert [_key(e) for e in branch_upto2] == [_key(e) for e in parent_upto2]

    # IMMUTABILITY INVARIANT: parent unchanged byte-for-byte after branching.
    after = _parent_fingerprint(client, run_id)
    assert after["events"] == before["events"]
    assert after["snapshots"] == before["snapshots"]
    assert after["cost"] == before["cost"]
    assert after["turn_count"] == before["turn_count"]


def test_branch_lineage_shown_in_history_and_detail(client):
    run_id = _make_run(client)
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_always(["Ada", "Ben"]),
    ):
        meta = client.post(
            f"/api/runs/{run_id}/branch", json={"from_turn": 2}
        ).json()
        branch_id = meta["run_id"]
        _wait_status(client, branch_id)

    # Parent detail lists the child branch.
    parent_detail = client.get(f"/api/runs/{run_id}").json()
    child_ids = [b["run_id"] for b in parent_detail["lineage"]["branches"]]
    assert branch_id in child_ids

    # Branch detail names its parent + fork turn.
    branch_detail = client.get(f"/api/runs/{branch_id}").json()
    assert branch_detail["lineage"]["parent"]["run_id"] == run_id
    assert branch_detail["lineage"]["parent"]["branch_turn"] == 2

    # History list flags the branch's lineage fields.
    listing = client.get("/api/runs").json()["runs"]
    branch_row = next(r for r in listing if r["run_id"] == branch_id)
    assert branch_row["parent_run_id"] == run_id
    assert branch_row["branch_turn"] == 2


def test_branch_rejects_out_of_range_turn(client):
    run_id = _make_run(client)
    # Parent has 4 turns; turn 99 does not exist.
    r = client.post(f"/api/runs/{run_id}/branch", json={"from_turn": 99})
    assert r.status_code == 422


def test_branch_404_for_unknown_run(client):
    r = client.post("/api/runs/nope/branch", json={"from_turn": 1})
    assert r.status_code == 404


def test_branch_watchable_over_ws(client):
    run_id = _make_run(client)
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_always(["Ada", "Ben"]),
    ):
        meta = client.post(
            f"/api/runs/{run_id}/branch", json={"from_turn": 2}
        ).json()
        branch_id = meta["run_id"]
        _wait_status(client, branch_id)

        received = []
        with client.websocket_connect(f"/api/runs/{branch_id}/stream") as ws:
            while True:
                msg = ws.receive_json()
                received.append(msg)
                if msg["event_type"] in ("sim.completed", "sim.failed"):
                    break
    types = [e["event_type"] for e in received]
    # The branch replays the copied fork events then its own forward turns.
    assert types[0] == "sim.started"
    assert types[-1] == "sim.completed"
    assert "agent.response" in types
