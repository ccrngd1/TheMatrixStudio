# SPDX-License-Identifier: Apache-2.0
"""
Phase 3 secret safety tests.

Verify that no `.env`-style secret values ever appear in any API response body.
Keys stay server-side; the browser never handles raw credentials.
"""

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from matrix_studio.api.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient for secret safety tests."""
    db_path = str(tmp_path / "test.db")

    # Mock naming call
    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        return {"name": "test-run", "description": "Test", "slug": "test-run", "source": "llm"}

    monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.api.app.generate_run_name", fake_name)

    app = create_app(db_path=db_path)
    with TestClient(app) as c:
        yield c


def test_no_secrets_in_health_endpoint(client):
    """
    /api/health returns per-provider has-key booleans, NEVER key values.
    """
    response = client.get("/api/health")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ok"
    assert "readiness" in data
    assert isinstance(data["readiness"], dict)

    # Verify only booleans, not key values
    for provider, has_key in data["readiness"].items():
        assert isinstance(has_key, bool), f"readiness[{provider}] must be boolean"

    # Ensure no secret-looking strings are in the response
    text = response.text.lower()
    # Common key prefixes/patterns that should NEVER appear
    forbidden_patterns = [
        "sk-",  # OpenAI key prefix
        "aws_secret",
        "aws_access",
        "api_key=",
        "bearer_token",
    ]
    for pattern in forbidden_patterns:
        assert pattern not in text, f"Secret pattern '{pattern}' found in /api/health response"


def test_no_secrets_in_models_endpoint(client):
    """
    /api/models returns model strings ONLY, never keys.
    """
    response = client.get("/api/models")
    assert response.status_code == 200

    data = response.json()
    assert "default" in data
    assert "models" in data
    assert isinstance(data["models"], list)

    # Verify model objects contain only id + label
    for model in data["models"]:
        assert "id" in model
        assert "label" in model
        # Should NOT contain any credential fields
        assert "api_key" not in model
        assert "secret" not in model

    # Ensure no secret-looking strings are in the response
    text = response.text.lower()
    forbidden_patterns = [
        "sk-",
        "aws_secret",
        "aws_access",
        "api_key=",
        "bearer_token",
    ]
    for pattern in forbidden_patterns:
        assert pattern not in text, f"Secret pattern '{pattern}' found in /api/models response"


def test_no_secrets_in_run_response(client, monkeypatch):
    """
    Run creation and retrieval endpoints never expose keys in their responses.
    """
    # Mock the engine to avoid live calls
    from tests.test_api import make_fake_run
    monkeypatch.setattr("matrix_studio.api.manager.run_simulation", make_fake_run(turns=1))

    # Create a run
    create_response = client.post(
        "/api/runs",
        json={
            "topic": "secret safety test",
            "cast": [
                {"name": "Alice", "persona": "Test persona", "goals": ["test"]},
            ],
            "config": {"max_messages": 1, "generate_avatars": False},
        },
    )
    assert create_response.status_code == 201
    run_id = create_response.json()["run_id"]

    # Wait for run to complete (it's mocked, should be instant)
    import time
    time.sleep(0.5)

    # Retrieve run metadata
    get_response = client.get(f"/api/runs/{run_id}")
    assert get_response.status_code == 200

    # Check for secrets in response
    text = get_response.text.lower()
    forbidden_patterns = [
        "sk-",
        "aws_secret",
        "aws_access",
        "api_key=",
        "bearer_token",
    ]
    for pattern in forbidden_patterns:
        assert pattern not in text, f"Secret pattern '{pattern}' found in run response"

    # Also verify the structured response doesn't have credential fields
    data = get_response.json()
    data_str = str(data).lower()
    # Check for credential-related terms but exclude the test topic itself
    assert "api_key" not in data_str
    # "secret" will appear in the topic, but shouldn't appear as part of a credential field name
    assert "aws_secret" not in data_str
    assert "secret_key" not in data_str


def test_no_secrets_in_events(client, monkeypatch):
    """
    Event logs never contain credential values.
    """
    # Mock the engine to avoid live calls
    from tests.test_api import make_fake_run
    monkeypatch.setattr("matrix_studio.api.manager.run_simulation", make_fake_run(turns=1))

    # Create a run
    create_response = client.post(
        "/api/runs",
        json={
            "topic": "event safety test",
            "cast": [
                {"name": "Bob", "persona": "Test persona", "goals": ["test"]},
            ],
            "config": {"max_messages": 1, "generate_avatars": False},
        },
    )
    assert create_response.status_code == 201
    run_id = create_response.json()["run_id"]

    # Wait for run to complete
    import time
    time.sleep(0.5)

    # Retrieve events
    events_response = client.get(f"/api/runs/{run_id}/events")
    assert events_response.status_code == 200

    # Check for secrets in events
    text = events_response.text.lower()
    forbidden_patterns = [
        "sk-",
        "aws_secret",
        "aws_access",
        "api_key=",
        "bearer_token",
    ]
    for pattern in forbidden_patterns:
        assert pattern not in text, f"Secret pattern '{pattern}' found in events response"
