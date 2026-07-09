# SPDX-License-Identifier: Apache-2.0
"""
Phase 2c step-4 tests — introspection read APIs over the REAL engine + API
(litellm mocked): /agents/{name}/dossier and /turns/{turn}/trace. Also asserts
the honesty gate: a cognition-off run's trace reports {available: false} rather
than synthesizing a rationale.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from matrix_studio.api.app import create_app


class _Resp:
    def __init__(self, content):
        self.choices = [MagicMock(message=MagicMock(content=content))]
        self.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
        self._hidden_params = {"response_cost": 0.001}


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        return {"name": "trusted-robot", "description": "t", "slug": "trusted-robot",
                "source": "llm"}

    monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.api.app.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.branching.generate_run_name", fake_name)
    app = create_app(db_path=str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def _wait(client, ref, tries=300):
    for _ in range(tries):
        r = client.get(f"/api/runs/{ref}")
        if r.status_code == 200 and r.json()["status"] in ("complete", "failed"):
            return r.json()
        time.sleep(0.02)
    return client.get(f"/api/runs/{ref}").json()


def _cognition_fake():
    """Always Ada; JSON-mode selection + response with rationale/memory."""
    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Ada", "reason": "Ada should lead"}))
        return _Resp(json.dumps({
            "utterance": "Consent is the crux.",
            "rationale": "I want to steer toward ethics.",
            "goal_served": "seek truth",
            "memories": [{"content": "the group values consent", "importance": 0.8,
                          "tags": ["fact"]}],
        }))
    return fake


def _plain_fake():
    """Cognition OFF: plain name + plain text (no JSON, no rationale)."""
    n = {"i": 0}
    def fake(*args, **kwargs):
        n["i"] += 1
        return _Resp("Ada" if n["i"] % 2 == 1 else "just talking, no structure")
    return fake


COG_REQUEST = {
    "topic": "AI ethics",
    "cast": [
        {"name": "Ada", "persona": "ethicist", "goals": ["seek truth"]},
        {"name": "Ben", "persona": "engineer", "goals": ["ship safely"]},
    ],
    "config": {"max_messages": 3, "generate_avatars": False,
               "cognition": {"enabled": True}},
}

PLAIN_REQUEST = {
    "topic": "AI ethics",
    "cast": [
        {"name": "Ada", "persona": "ethicist", "goals": ["seek truth"]},
        {"name": "Ben", "persona": "engineer", "goals": ["ship safely"]},
    ],
    "config": {"max_messages": 3, "generate_avatars": False},
}


def test_dossier_returns_captured_cognition(client):
    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_cognition_fake()):
        run_id = client.post("/api/runs", json=COG_REQUEST).json()["run_id"]
        _wait(client, run_id)

    r = client.get(f"/api/runs/{run_id}/agents/Ada/dossier")
    assert r.status_code == 200
    d = r.json()
    assert d["agent"] == "Ada"
    assert d["goals"] == ["seek truth"]
    assert len(d["memory_stream"]) >= 1
    assert all("id" in m and "content" in m for m in d["memory_stream"])
    assert d["tokens_out"] > 0

    # unknown agent -> 404
    assert client.get(f"/api/runs/{run_id}/agents/Nobody/dossier").status_code == 404


def test_trace_available_for_cognition_run(client):
    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_cognition_fake()):
        run_id = client.post("/api/runs", json=COG_REQUEST).json()["run_id"]
        _wait(client, run_id)

    r = client.get(f"/api/runs/{run_id}/turns/2/trace")
    assert r.status_code == 200
    t = r.json()
    assert t["available"] is True
    assert t["speaker"] == "Ada"
    assert t["selection_reason"] == "Ada should lead"
    assert t["rationale"]
    assert t["goal_served"] == "seek truth"
    # memory_refs resolve to real memory items (causal, from a prior turn)
    assert t["memory_refs"], "turn 2 should have retrieved a prior memory"
    assert {m["id"] for m in t["memories"]} == set(t["memory_refs"])


def test_trace_not_available_for_cognition_off_run(client):
    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_plain_fake()):
        run_id = client.post("/api/runs", json=PLAIN_REQUEST).json()["run_id"]
        _wait(client, run_id)

    r = client.get(f"/api/runs/{run_id}/turns/2/trace")
    assert r.status_code == 200
    t = r.json()
    assert t["available"] is False
    # honesty gate: no synthesized rationale/goal for a run that never captured it
    assert "rationale" not in t

    # nonexistent turn -> 404
    assert client.get(f"/api/runs/{run_id}/turns/99/trace").status_code == 404
