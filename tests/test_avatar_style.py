# SPDX-License-Identifier: Apache-2.0
"""Avatar art-style tests: avatars must be NON-photorealistic (stylized) so they
never read as photos of real people. Bedrock/boto3 is mocked to capture the
image request body without any network/model call."""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio.avatar import generate_avatar
from matrix_studio.settings import get_settings


def _mock_boto(captured):
    def _client(service, **kwargs):
        c = MagicMock()
        def _invoke(*, modelId, body, **_kw):
            captured["body"] = json.loads(body)
            png = base64.b64encode(b"fakepng").decode()
            return {"body": MagicMock(read=lambda: json.dumps(
                {"images": [png], "finish_reasons": ["SUCCESS"]}).encode())}
        c.invoke_model.side_effect = _invoke
        return c
    return _client


async def test_avatar_prompt_is_non_photorealistic():
    get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
    captured = {}
    with patch("boto3.client", side_effect=_mock_boto(captured)):
        out = await generate_avatar("Dr. Emily Chen", "a calm bioethicist")
    assert out  # base64 returned
    prompt = captured["body"]["prompt"].lower()
    neg = captured["body"]["negative_prompt"].lower()
    # positive prompt must NOT ask for a photo, and must flag it fictional/stylized
    assert "photograph of" not in prompt
    assert "non-photorealistic" in prompt  # explicitly stylized
    assert "fictional character" in prompt
    # negative prompt must actively push away from realism
    assert "photorealistic" in neg and "real person" in neg


async def test_avatar_style_setting_switches_clause(monkeypatch):
    from matrix_studio import settings as settings_mod
    captured = {}
    s = settings_mod.get_settings()
    monkeypatch.setattr(s, "avatar_style", "anime")
    with patch("boto3.client", side_effect=_mock_boto(captured)):
        await generate_avatar("Ada", "an ethicist")
    assert "anime" in captured["body"]["prompt"].lower()
