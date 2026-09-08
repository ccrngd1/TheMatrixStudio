# SPDX-License-Identifier: Apache-2.0
"""Pytest configuration and shared fixtures."""

import os
import sys
from pathlib import Path

import pytest

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@pytest.fixture(autouse=True)
def reset_settings_singleton():
    """Reset the settings singleton between tests."""
    import matrix_studio.settings
    matrix_studio.settings._settings = None
    yield
    matrix_studio.settings._settings = None


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Give every test the same environment: code defaults, no local config.

    ``_MSS_TEST_MODE`` stops ``Settings`` from reading ``.env`` directly, but that
    is not sufficient on its own. Importing litellm runs ``load_dotenv()`` at
    import time (``litellm/__init__.py``), which copies the developer's ``.env``
    into ``os.environ`` — and real environment variables outrank everything. So
    on a machine with a populated ``.env`` (i.e. anyone who followed the README),
    tests would silently pick up local configuration and assertions about
    defaults would fail.

    Clearing every var that maps to a ``Settings`` field closes that hole at the
    root. The list is derived from the model so it cannot go stale as fields are
    added. Tests that want a specific value still set it with ``monkeypatch``,
    which runs after this fixture.
    """
    monkeypatch.setenv("_MSS_TEST_MODE", "1")

    # Force litellm's import — and therefore its one-time load_dotenv() — to
    # happen BEFORE the clearing below, not after it.
    #
    # Measured: with this absent, `pytest tests/test_settings.py` alone failed
    # test_settings_defaults while the full suite passed. Clearing ran first, then
    # the mock_analysis_llm fixture below imported matrix_studio.analysis ->
    # litellm -> load_dotenv(), which put the developer's LITELLM_MODEL straight
    # back into os.environ. In a whole-suite run litellm was already in
    # sys.modules by collection time, so load_dotenv never re-ran and the
    # clearing appeared to work. That made the isolation order-dependent: whether
    # a test saw code defaults or local config depended on which other test files
    # were selected.
    try:
        import litellm  # noqa: F401
    except ImportError:
        pass

    from matrix_studio.settings import Settings

    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)

    # Not Settings fields, but read straight from the environment by
    # litellm/boto3. Cleared so a stray credential cannot turn a mocked test
    # into a live billable call.
    for var in (
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_REGION",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def mock_analysis_llm(monkeypatch):
    """
    Globally mock the Phase 1.5 analysis LLM seam so NO test ever makes a live
    billable call (the real env carries a Bedrock key). The auto-summary that
    fires when a run completes goes through this. Tests that need specific
    analysis behavior patch ``matrix_studio.analysis._acompletion`` themselves,
    which overrides this default within their scope.
    """
    import json as _json

    async def _fake_acompletion(messages, model=None, temperature=0.4, max_tokens=None):
        # Return a valid structured summary for summary prompts; a short reply
        # otherwise. Detection is heuristic on the system prompt.
        system = messages[0]["content"] if messages else ""
        if "STRUCTURED analysis" in system or "JSON object" in system:
            content = _json.dumps(
                {
                    "consensus": ["mocked consensus point"],
                    "dissenters": [{"speaker": "Mock", "position": "mocked dissent"}],
                    "key_ideas": ["mocked idea"],
                    "open_questions": ["mocked question"],
                    "overview": "Mocked analyst overview of the transcript.",
                }
            )
        else:
            content = "Mocked aside reply grounded in the transcript."
        return {
            "content": content,
            "tokens_in": 100,
            "tokens_out": 20,
            "cost_usd": 0.0012,
        }

    monkeypatch.setattr(
        "matrix_studio.analysis._acompletion", _fake_acompletion
    )
