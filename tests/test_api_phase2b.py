# SPDX-License-Identifier: Apache-2.0
"""
Phase 2b step-1 tests — branch-with-mutation over the REAL engine + branching
service (litellm mocked). Covers the two step-1 mutation kinds (inject_message,
continue) end-to-end plus validation, and re-asserts the immutability invariant:
a mutated branch NEVER changes the parent run.
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


def _always(names):
    def _factory(*args, **kwargs):
        _factory.n += 1
        if _factory.n % 2 == 1:
            return MockLiteLLMResponse(names[(_factory.n // 2) % len(names)])
        return MockLiteLLMResponse(f"reply {_factory.n}")
    _factory.n = 0
    return _factory


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        base = "trusted-robot"
        name = base
        n = 2
        while name_exists is not None and await _maybe(name_exists(name)):
            name = f"{base}-{n}"
            n += 1
        return {"name": name, "description": "t", "slug": name, "source": "llm"}

    async def _maybe(v):
        if hasattr(v, "__await__"):
            return await v
        return v

    monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.api.app.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.branching.generate_run_name", fake_name)

    app = create_app(db_path=str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


REQUEST = {
    "topic": "AI ethics",
    "cast": [
        {"name": "Ada", "persona": "An ethicist", "goals": ["Question assumptions"]},
        {"name": "Ben", "persona": "An engineer", "goals": ["Ship safely"]},
    ],
    "config": {"max_messages": 4, "generate_avatars": False},
}


def _wait_status(client, ref, statuses=("complete", "failed"), tries=300):
    for _ in range(tries):
        r = client.get(f"/api/runs/{ref}")
        if r.status_code == 200 and r.json()["status"] in statuses:
            return r.json()
        time.sleep(0.02)
    return client.get(f"/api/runs/{ref}").json()


def _make_run(client):
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_always(["Ada", "Ben"]),
    ):
        run_id = client.post("/api/runs", json=REQUEST).json()["run_id"]
        _wait_status(client, run_id)
    return run_id


def _fingerprint(client, run_id):
    events = client.get(f"/api/runs/{run_id}/events").json()["events"]
    detail = client.get(f"/api/runs/{run_id}").json()
    snaps = client.get(f"/api/runs/{run_id}/snapshots").json()["snapshots"]
    return {"events": events, "snapshots": snaps,
            "cost": detail["total_cost_usd"], "turn_count": detail["turn_count"]}


def test_inject_message_becomes_a_forward_turn_and_parent_unchanged(client):
    run_id = _make_run(client)
    before = _fingerprint(client, run_id)

    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_always(["Ada", "Ben"]),
    ):
        meta = client.post(
            f"/api/runs/{run_id}/branch",
            json={
                "from_turn": 2,
                "mutation": {
                    "kind": "inject_message",
                    "speaker": "Moderator",
                    "content": "Let's focus on real-world harm.",
                },
            },
        ).json()
        branch_id = meta["run_id"]
        assert meta["mutation"]["kind"] == "inject_message"
        _wait_status(client, branch_id)

    # The injected message is a real turn at from_turn+1 (turn 3), flagged.
    branch_events = client.get(f"/api/runs/{branch_id}/events").json()["events"]
    injected = [
        e for e in branch_events
        if e["event_type"] == "agent.response" and e["payload"].get("injected")
    ]
    assert len(injected) == 1
    ev = injected[0]
    assert ev["turn"] == 3
    assert ev["payload"]["speaker"] == "Moderator"
    assert ev["payload"]["message"] == "Let's focus on real-world harm."
    assert ev["payload"]["cost_usd"] == 0.0

    # Branch config records the mutation (self-describing for the tree UI).
    detail = client.get(f"/api/runs/{branch_id}").json()
    assert detail["config"]["branch_mutation"]["kind"] == "inject_message"

    # It generated forward past the injection.
    assert detail["turn_count"] >= 4

    # IMMUTABILITY: parent unchanged.
    after = _fingerprint(client, run_id)
    assert after["events"] == before["events"]
    assert after["snapshots"] == before["snapshots"]
    assert after["cost"] == before["cost"]
    assert after["turn_count"] == before["turn_count"]


def test_continue_extends_budget(client):
    run_id = _make_run(client)  # completes at turn 4
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_always(["Ada", "Ben"]),
    ):
        meta = client.post(
            f"/api/runs/{run_id}/branch",
            json={"from_turn": 4, "mutation": {"kind": "continue", "add_budget": 3}},
        ).json()
        branch_id = meta["run_id"]
        detail = _wait_status(client, branch_id)

    # Forked at turn 4, +3 budget => generates up to turn 7.
    assert detail["status"] == "complete"
    assert detail["turn_count"] == 7
    assert detail["config"]["branch_mutation"] == {"kind": "continue", "add_budget": 3}


def test_plain_branch_still_works_without_mutation(client):
    run_id = _make_run(client)
    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_always(["Ada", "Ben"]),
    ):
        meta = client.post(f"/api/runs/{run_id}/branch", json={"from_turn": 2}).json()
        assert meta["mutation"] is None
        detail = _wait_status(client, meta["run_id"])
    assert detail["status"] == "complete"
    assert "branch_mutation" not in detail["config"]


@pytest.mark.parametrize(
    "mutation,expected_detail",
    [
        ({"kind": "inject_message", "content": "x"}, "speaker"),
        ({"kind": "inject_message", "speaker": "M"}, "content"),
        ({"kind": "continue"}, "add_budget"),
        ({"kind": "continue", "add_budget": 0}, "add_budget"),
        ({"kind": "bogus"}, "unsupported"),
    ],
)
def test_invalid_mutations_rejected_422(client, mutation, expected_detail):
    run_id = _make_run(client)
    r = client.post(
        f"/api/runs/{run_id}/branch", json={"from_turn": 2, "mutation": mutation}
    )
    assert r.status_code == 422
    body = r.json()
    detail = body.get("detail")
    assert expected_detail in str(detail).lower()


# ---------------------------------------------------------------------------
# Step 2: state-mutation kinds  (edit_goal / add_persona / remove_persona)
# ---------------------------------------------------------------------------

def test_edit_goal_changes_forward_agent_state(client):
    run_id = _make_run(client)
    before = _fingerprint(client, run_id)

    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_always(["Ada", "Ben"]),
    ):
        meta = client.post(
            f"/api/runs/{run_id}/branch",
            json={
                "from_turn": 2,
                "mutation": {
                    "kind": "edit_goal",
                    "persona_name": "Ada",
                    "goals": ["Maximise safety", "Challenge assumptions"],
                },
            },
        ).json()
        branch_id = meta["run_id"]
        detail = _wait_status(client, branch_id)

    assert detail["status"] == "complete"
    # Branch config records the mutation.
    assert detail["config"]["branch_mutation"]["kind"] == "edit_goal"
    assert "Maximise safety" in detail["config"]["branch_mutation"]["goals"]
    # The branch's snapshot at turn 2 reflects the new goals.
    snap = client.get(f"/api/runs/{branch_id}/snapshots/2").json()
    assert snap["agents"]["Ada"]["goals"] == ["Maximise safety", "Challenge assumptions"]
    # Immutability: parent Ada goals unchanged.
    parent_snap = client.get(f"/api/runs/{run_id}/snapshots/2").json()
    assert "Maximise safety" not in parent_snap["agents"]["Ada"]["goals"]
    # Parent event/snapshot/cost fingerprint unmodified.
    after = _fingerprint(client, run_id)
    assert after["events"] == before["events"]


def test_add_persona_appears_in_forward_speakers(client):
    run_id = _make_run(client)
    before = _fingerprint(client, run_id)

    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_always(["Carol", "Ada", "Ben"]),
    ):
        meta = client.post(
            f"/api/runs/{run_id}/branch",
            json={
                "from_turn": 2,
                "mutation": {
                    "kind": "add_persona",
                    "name": "Carol",
                    "persona": "A sociologist studying AI impact",
                    "goals": ["Understand systemic effects"],
                },
            },
        ).json()
        branch_id = meta["run_id"]
        detail = _wait_status(client, branch_id)

    assert detail["status"] == "complete"
    assert detail["config"]["branch_mutation"]["kind"] == "add_persona"
    # Carol exists in the branch's cast snapshot.
    snap = client.get(f"/api/runs/{branch_id}/snapshots/2").json()
    assert "Carol" in snap["agents"]
    # Parent unchanged.
    parent_snap = client.get(f"/api/runs/{run_id}/snapshots/2").json()
    assert "Carol" not in parent_snap["agents"]
    after = _fingerprint(client, run_id)
    assert after["events"] == before["events"]


def test_remove_persona_absent_from_forward_state(client):
    run_id = _make_run(client)
    before = _fingerprint(client, run_id)

    with patch(
        "matrix_studio.engine.simulator.litellm.acompletion",
        side_effect=_always(["Ada"]),
    ):
        meta = client.post(
            f"/api/runs/{run_id}/branch",
            json={
                "from_turn": 2,
                "mutation": {"kind": "remove_persona", "persona_name": "Ben"},
            },
        ).json()
        branch_id = meta["run_id"]
        detail = _wait_status(client, branch_id)

    assert detail["status"] == "complete"
    assert detail["config"]["branch_mutation"]["kind"] == "remove_persona"
    snap = client.get(f"/api/runs/{branch_id}/snapshots/2").json()
    assert "Ben" not in snap["agents"]
    assert "Ada" in snap["agents"]
    # Ben still in parent's snapshot.
    parent_snap = client.get(f"/api/runs/{run_id}/snapshots/2").json()
    assert "Ben" in parent_snap["agents"]
    after = _fingerprint(client, run_id)
    assert after["events"] == before["events"]


@pytest.mark.parametrize(
    "mutation,expected_status",
    [
        # Schema/field errors -> 422
        ({"kind": "edit_goal", "goals": ["x"]}, 422),
        ({"kind": "edit_goal", "persona_name": "Ada"}, 422),
        ({"kind": "add_persona", "persona": "x"}, 422),
        ({"kind": "add_persona", "name": "Carol"}, 422),
        ({"kind": "remove_persona"}, 422),
        # Run-time errors (unknown persona at fork) -> 201 but branch fails
        ({"kind": "edit_goal", "persona_name": "NoOne", "goals": []}, 201),
        ({"kind": "add_persona", "name": "Ada", "persona": "x"}, 201),  # already exists
        ({"kind": "remove_persona", "persona_name": "NoOne"}, 201),
    ],
)
def test_step2_invalid_mutations_rejected_or_fail(client, mutation, expected_status):
    run_id = _make_run(client)
    r = client.post(
        f"/api/runs/{run_id}/branch", json={"from_turn": 2, "mutation": mutation}
    )
    assert r.status_code == expected_status
