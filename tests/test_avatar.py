# SPDX-License-Identifier: Apache-2.0
"""Tests for avatar generation - Stability image model, graceful fallback."""

import json

import pytest
from unittest.mock import MagicMock, patch

from matrix_studio.avatar import generate_avatar, _deterministic_seed
from matrix_studio.settings import Settings


def _settings(**overrides):
    """Build settings ignoring any local .env for deterministic tests."""
    base = dict(
        enable_avatars=True,
        avatar_model_id="stability.sd3-5-large-v1:0",
        avatar_region="us-west-2",
        avatar_aspect_ratio="1:1",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _mock_response(payload: dict):
    return {"body": MagicMock(read=MagicMock(return_value=json.dumps(payload).encode()))}


@pytest.mark.asyncio
async def test_avatar_disabled_in_settings():
    """enable_avatars=False short-circuits before any AWS call."""
    with patch("matrix_studio.avatar.get_settings", return_value=_settings(enable_avatars=False)):
        with patch("boto3.client") as mock_boto:
            result = await generate_avatar("Alice", "Friendly person")
    assert result is None
    mock_boto.assert_not_called()


@pytest.mark.asyncio
async def test_avatar_no_credentials():
    """Missing credentials (NoCredentialsError at invoke) → graceful None."""
    from botocore.exceptions import NoCredentialsError

    mock_client = MagicMock()
    mock_client.invoke_model.side_effect = NoCredentialsError()
    with patch("matrix_studio.avatar.get_settings", return_value=_settings()):
        with patch("boto3.client", return_value=mock_client):
            result = await generate_avatar("Bob", "Curious researcher")
    assert result is None


@pytest.mark.asyncio
async def test_avatar_successful_generation():
    """Valid Stability response returns the base64 image."""
    payload = {"images": ["base64_png_data"], "seeds": [42], "finish_reasons": [None]}
    mock_client = MagicMock()
    mock_client.invoke_model.return_value = _mock_response(payload)
    with patch("matrix_studio.avatar.get_settings", return_value=_settings()):
        with patch("boto3.client", return_value=mock_client):
            result = await generate_avatar("Diana", "Artist with creative vision")
    assert result == "base64_png_data"
    mock_client.invoke_model.assert_called_once()
    # Sanity: uses the configured model/region and Stability body shape.
    _, kwargs = mock_client.invoke_model.call_args
    body = json.loads(kwargs["body"])
    assert body["mode"] == "text-to-image"
    assert body["output_format"] == "png"


@pytest.mark.asyncio
async def test_avatar_content_filtered():
    """A non-null finish_reason (content filter) yields None, not a bad image."""
    payload = {"images": [], "seeds": [1], "finish_reasons": ["CONTENT_FILTERED"]}
    mock_client = MagicMock()
    mock_client.invoke_model.return_value = _mock_response(payload)
    with patch("matrix_studio.avatar.get_settings", return_value=_settings()):
        with patch("boto3.client", return_value=mock_client):
            result = await generate_avatar("Eve", "Test persona")
    assert result is None


@pytest.mark.asyncio
async def test_avatar_unexpected_response_format():
    """Unexpected response shape → None."""
    mock_client = MagicMock()
    mock_client.invoke_model.return_value = _mock_response({"unexpected": "format"})
    with patch("matrix_studio.avatar.get_settings", return_value=_settings()):
        with patch("boto3.client", return_value=mock_client):
            result = await generate_avatar("Frank", "Test persona")
    assert result is None


@pytest.mark.asyncio
async def test_avatar_client_error():
    """AWS ClientError → graceful None."""
    from botocore.exceptions import ClientError

    mock_client = MagicMock()
    err = {"Error": {"Code": "AccessDenied", "Message": "Access denied"}}
    mock_client.invoke_model.side_effect = ClientError(err, "InvokeModel")
    with patch("matrix_studio.avatar.get_settings", return_value=_settings()):
        with patch("boto3.client", return_value=mock_client):
            result = await generate_avatar("Grace", "Test persona")
    assert result is None


def test_deterministic_seed_stable_and_distinct():
    """Seed is stable per name (across processes) and differs between names."""
    assert _deterministic_seed("Alice") == _deterministic_seed("Alice")
    assert _deterministic_seed("Alice") != _deterministic_seed("Bob")
    assert 0 <= _deterministic_seed("Alice") < 2**32 - 1
