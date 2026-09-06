# SPDX-License-Identifier: Apache-2.0
"""Phase 6 API tests — the structured-persona request contract and dossier.

The first test here is a regression guard, not a nicety. In Phase 5 exactly this
was missed: ``PersonaModel`` did not declare ``documents`` and
``RunConfigModel`` did not declare ``retrieval``, Pydantic silently dropped both,
and the feature worked from the CLI while vanishing through the API. Structured
personas have the same shape of risk, so the round trip is asserted end to end.

The second thing under test is the API BOUNDARY behaviour of a bad persona. A
malformed ``firmness`` must be a 422 from the request model, not a 500 from the
engine — which is why ``PersonaModel.structured`` is typed as the real model
rather than a loose dict.
"""

import copy
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


def _fake(*args, **kwargs):
    text = " ".join(m["content"] for m in kwargs["messages"])
    if "conversation moderator" in text:
        return _Resp("Dana")
    return _Resp("A considered reply.")


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        return {
            "name": "trusted-robot", "description": "t",
            "slug": "trusted-robot", "source": "llm",
        }

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


CONCERN = "I own the failure when a customer never reaches a working run"
VALIDITY = "overgeneralised"

STRUCTURED = {
    "role": "Head of Distribution & Packaging",
    "background": {
        "tenure_years": 9,
        "formative_events": [
            {"year": 2023, "event": "A quickstart needing a vector database",
             "lesson": "Every extra service costs you users"}
        ],
    },
    "preferences": {
        "optimises_for": ["time-to-first-run"],
        "dismisses": ["retrieval answer quality"],
        "persuaded_by": ["a clean-machine install"],
    },
    "viewpoints": [
        {
            "position": "No feature may add a stateful external service",
            "underlying_concern": CONCERN,
            "formed_by": "The 2023 product that stalled at the install step",
            "firmness": "firm",
            "evidence_that_shifts": ["an embedded index that is a file"],
            "validity": VALIDITY,
        }
    ],
}


def _request(**config_extra):
    config = {"max_messages": 1, "generate_avatars": False, "personas": {"enabled": True}}
    config.update(config_extra)
    return {
        "topic": "Should we add a document retrieval layer",
        "cast": [
            {
                "name": "Dana",
                "persona": "distribution lead",
                "goals": ["protect the install"],
                # Deep-copied: tests below mutate the request (a bad `firmness`),
                # and sharing the module-level dict would leak that into every
                # later test in file order.
                "structured": copy.deepcopy(STRUCTURED),
            },
            {"name": "Marcus", "persona": "cost analyst", "goals": ["measure spend"]},
        ],
        "config": config,
    }


def test_api_request_contract_carries_structured_personas(client):
    """Regression: PersonaModel must declare `structured` and RunConfigModel must
    declare `personas`, or the feature silently vanishes through the API."""
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake):
        ref = client.post("/api/runs", json=_request()).json()["run_id"]
        _wait(client, ref)

    stored = client.get(f"/api/runs/{ref}").json()
    assert stored["config"]["personas"]["enabled"] is True

    # It reached the engine: the seeding event only fires when the feature is on.
    events = client.get(f"/api/runs/{ref}/events").json()["events"]
    seeded = [e for e in events if e["event_type"] == "persona.structured"]
    assert [e["agent_name"] for e in seeded] == ["Dana"]


def test_dossier_exposes_convictions_without_the_private_fields(client):
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake):
        ref = client.post("/api/runs", json=_request()).json()["run_id"]
        _wait(client, ref)

    body = client.get(f"/api/runs/{ref}/agents/Dana/dossier").json()
    structured = body["structured"]
    assert structured is not None
    assert structured["role"] == "Head of Distribution & Packaging"
    assert structured["viewpoints"][0]["firmness"] == "firm"
    # The dossier is a UI surface. Showing the withheld concern there would let an
    # operator read off the answer the panel is meant to draw out in conversation.
    flat = json.dumps(structured)
    assert CONCERN not in flat
    assert VALIDITY not in flat


def test_dossier_structured_is_null_for_a_plain_persona(client):
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake):
        ref = client.post("/api/runs", json=_request()).json()["run_id"]
        _wait(client, ref)

    body = client.get(f"/api/runs/{ref}/agents/Marcus/dossier").json()
    assert body["structured"] is None


def test_bad_firmness_is_a_422_at_the_boundary_not_a_500_from_the_engine(client):
    request = _request()
    request["cast"][0]["structured"]["viewpoints"][0]["firmness"] = "rock solid"
    resp = client.post("/api/runs", json=request)
    assert resp.status_code == 422
    assert "firmness" in resp.text


def test_omitting_the_personas_config_leaves_structure_inert(client):
    """A cast carrying convictions with no `personas` block must run exactly as
    it did before Phase 6 — accepted, stored, never rendered."""
    request = _request()
    del request["config"]["personas"]
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake):
        ref = client.post("/api/runs", json=request).json()["run_id"]
        result = _wait(client, ref)

    assert result["status"] == "complete"
    events = client.get(f"/api/runs/{ref}/events").json()["events"]
    assert not [e for e in events if e["event_type"] == "persona.structured"]
