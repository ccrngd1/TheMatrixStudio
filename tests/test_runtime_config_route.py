# SPDX-License-Identifier: Apache-2.0
"""
``GET /config.json`` — what the SPA reads to decide whether to show a login.

This route exists because of a defect the login gate introduced and no test caught:
the SPA fetches ``/config.json``, and under uvicorn the static catch-all
``/{full_path:path}`` matched it and SPA-fell-back to ``index.html``. So the SPA got
**200 text/html** where it expected JSON, `res.json()` threw, and — correctly failing
closed — the local single-user tool showed "Configuration error" instead of the app.

Every frontend test mocked `fetch`, so all 33 of them passed.

The route also removes an inference that was unsafe in the other direction. The SPA
used to read a 404 as "the local tool, which ships no config file" and therefore *no
login required*. A 404 is reachable on a real deployment — the documented SPA sync was
``aws s3 sync … --delete``, which deletes the ``config.json`` that ``cdk deploy``
writes separately — so a routine frontend deploy served the whole application to
anybody. With this route, "no login" is something a server SAYS, never something the
client infers from absence.
"""

import pytest
from fastapi.testclient import TestClient

from matrix_studio.api.app import create_app


@pytest.fixture(autouse=True)
def _storage_backend(aws_backend):
    """The app's lifespan connects to DynamoDB; without this it reaches real AWS."""


def _config(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with TestClient(create_app()) as client:
        response = client.get("/config.json")
    return response


# --------------------------------------------------------------------------- #
# It answers with JSON, not the SPA shell
# --------------------------------------------------------------------------- #


def test_it_returns_json_not_the_spa_fallback(monkeypatch):
    """The bug this route fixes. Registered BEFORE the catch-all, or it is HTML."""
    response = _config(monkeypatch)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    # The failure mode being guarded: an HTML shell with a 200.
    assert not response.text.lstrip().startswith("<")
    assert set(response.json()) == {
        "hostedUiUrl",
        "clientId",
        "userPoolId",
        "authRequired",
    }


def test_it_is_not_cached(monkeypatch):
    """A cached config outlives a change of auth mode, and the browser would keep
    showing a login for a pool that is gone (or none for one that is not)."""
    response = _config(monkeypatch)
    assert "no-store" in response.headers.get("cache-control", "")


# --------------------------------------------------------------------------- #
# authRequired follows the server's actual auth mode
# --------------------------------------------------------------------------- #


def test_single_user_mode_says_no_login_is_required(monkeypatch):
    """The local tool, stated explicitly rather than inferred from a missing file."""
    body = _config(monkeypatch, AUTH_MODE="single-user").json()
    assert body["authRequired"] is False


def test_jwt_mode_says_a_login_is_required(monkeypatch):
    body = _config(monkeypatch, AUTH_MODE="jwt").json()
    assert body["authRequired"] is True


def test_the_default_mode_is_reported_faithfully(monkeypatch):
    """Whatever `Settings` defaults to, this route must report THAT and not a
    hardcoded guess — the two drifting apart is how a deployment ends up open."""
    from matrix_studio.settings import get_settings

    body = _config(monkeypatch).json()
    assert body["authRequired"] is (get_settings().auth_mode != "single-user")


# --------------------------------------------------------------------------- #
# The pool details
# --------------------------------------------------------------------------- #


def test_pool_details_come_from_the_environment(monkeypatch):
    body = _config(
        monkeypatch,
        AUTH_MODE="jwt",
        COGNITO_HOSTED_UI_URL="https://pool.auth.us-east-1.amazoncognito.com",
        COGNITO_CLIENT_ID="client123",
        COGNITO_USER_POOL_ID="us-east-1_abc",
    ).json()
    assert body["hostedUiUrl"] == "https://pool.auth.us-east-1.amazoncognito.com"
    assert body["clientId"] == "client123"
    assert body["userPoolId"] == "us-east-1_abc"


def test_missing_pool_details_are_empty_rather_than_absent(monkeypatch):
    """Empty strings, not missing keys.

    On the deployed stack this route is never reached — CloudFront serves
    ``/config.json`` from the SPA bucket. If it ever IS what answers on a deployment
    requiring a login, the SPA reports "requires a login but names no user pool":
    a visible failure rather than an open door. That depends on the keys existing.
    """
    body = _config(monkeypatch, AUTH_MODE="jwt").json()
    assert body["hostedUiUrl"] == ""
    assert body["clientId"] == ""
    assert body["userPoolId"] == ""
    # And it still demands a login it cannot itself service.
    assert body["authRequired"] is True


# --------------------------------------------------------------------------- #
# It does not shadow the SPA
# --------------------------------------------------------------------------- #


def test_it_does_not_break_the_spa_catch_all(monkeypatch):
    """Adding a root route ahead of the catch-all must not change anything else.

    `/config.json` is the only path taken over, and only exactly that path.
    """
    with TestClient(create_app()) as client:
        # A path that merely resembles it still reaches the SPA fallback (or the
        # placeholder when no build is present) rather than returning config.
        for path in ("/config.jsonx", "/nested/config.json"):
            response = client.get(path)
            assert response.status_code in (200, 404)
            if response.headers.get("content-type", "").startswith("application/json"):
                assert "authRequired" not in response.json()
