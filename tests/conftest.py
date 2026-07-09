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
    """Clean environment variables that might affect tests."""
    # Disable .env file loading in Settings to avoid permission errors
    # (the .env file is owned by root in this environment)
    monkeypatch.setenv("_MSS_TEST_MODE", "1")

    # Remove any API keys from environment
    env_vars_to_remove = [
        "LITELLM_MODEL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ]
    for var in env_vars_to_remove:
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
