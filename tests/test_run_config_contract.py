# SPDX-License-Identifier: Apache-2.0
"""
The run config's contract: what it accepts, what it refuses, and what it does NOT silently drop.

Found 2026-09-26. `config.model` was undeclared on `RunConfigModel`, so Pydantic dropped it without
a word — and every run built from `data/renewalBrief/run.json`, which set it, asked for Opus 5 and
ran on the deployment's Sonnet 5: the renewal runs, research-verify-1, and all three §9 comparisons.
Nothing anywhere said so; the dry run printed "voice: sonnet-5" beside a definition saying Opus.

Two fixes, both pinned here: the key is declared and honoured end to end, and an UNKNOWN config key
is refused with a 422 naming it — because the general failure is not this one field, it is any
misplaced setting vanishing silently.
"""

import pytest
from unittest.mock import patch

# `_storage_backend` is autouse in test_api and provides the MOCKED account. Importing `client` without it
# built the app against real DynamoDB: these tests passed only while real credentials happened to be valid.
from tests.test_api import REQUEST, _storage_backend, _wait_complete, client, make_fake_run  # noqa: F401

OPUS = "bedrock/global.anthropic.claude-opus-5"


def _post(client, **config):
    body = {**REQUEST, "config": {**REQUEST["config"], **config}}
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=1)):
        r = client.post("/api/runs", json=body)
        if r.status_code == 201:
            _wait_complete(client, r.json()["run_id"])
    return r


def test_config_model_reaches_the_stored_config_and_every_role(client):
    r = _post(client, model=OPUS)
    assert r.status_code == 201, r.text
    detail = client.get(f"/api/runs/{r.json()['run_id']}").json()
    assert detail["config"]["model"] == OPUS, "config.model was dropped again"
    # The resolved plan, through the engine's own ModelSet: the voice is what was asked for.
    assert detail["models"]["voice"] == OPUS


def test_a_per_role_model_still_wins_over_config_model(client):
    haiku = "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0"
    r = _post(client, model=OPUS, models={"validation": haiku})
    models = client.get(f"/api/runs/{r.json()['run_id']}").json()["models"]
    assert models["voice"] == OPUS
    assert models["validation"] == haiku


def test_an_unknown_config_key_is_refused_naming_it(client):
    # A typo is the everyday version of the bug: without this it would be dropped silently, and the
    # author would find out only by wondering why the setting did nothing.
    r = _post(client, modle=OPUS)
    assert r.status_code == 422
    assert "modle" in r.text


def test_the_model_set_twice_differently_is_refused(client):
    body = {**REQUEST, "model": "bedrock/some-other-model",
            "config": {**REQUEST["config"], "model": OPUS}}
    r = client.post("/api/runs", json=body)
    assert r.status_code == 422
    assert "set twice" in r.text


def test_the_model_set_twice_the_SAME_is_accepted(client):
    body = {**REQUEST, "model": OPUS, "config": {**REQUEST["config"], "model": OPUS}}
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=1)):
        assert client.post("/api/runs", json=body).status_code == 201


def test_start_over_carries_selection_and_per_role_models(client):
    """The setup export had the same kind of silent drop, the other way round: "start over from
    this conversation" lost the speaker method and the per-role models."""
    r = _post(client, selection={"method": "hybrid", "hybrid_opening_rounds": 1},
              models={"validation": "bedrock/x"})
    setup = client.get(f"/api/runs/{r.json()['run_id']}/setup").json()["setup"]
    assert setup["config"]["selection"]["method"] == "hybrid"
    assert setup["config"]["models"] == {"validation": "bedrock/x"}


def test_the_export_names_the_voice_model_that_actually_ran():
    from matrix_studio.export import settings_lines

    assert "Personas' voice model: claude-opus-5" in settings_lines({"model": OPUS})
