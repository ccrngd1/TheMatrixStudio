# SPDX-License-Identifier: Apache-2.0
"""
Model selection tests.

Covers: (1) the engine honours a per-run config model instead of always using
the settings default; (2) /api/models exposes the configured allowlist; (3) a
branch resolves its generation model (explicit override > inherited parent model
> settings default for imported parents).
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio import branching
from matrix_studio.engine import run_simulation
from matrix_studio.storage import Database


@pytest.fixture
async def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()
    Path(db_path).unlink(missing_ok=True)


class _Resp:
    def __init__(self, content):
        self.choices = [MagicMock(message=MagicMock(content=content))]
        self.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        self._hidden_params = {"response_cost": 0.001}


def _always(names):
    def factory(*args, **kwargs):
        factory.models.append(kwargs.get("model"))
        factory.n += 1
        if factory.n % 2 == 1:
            return _Resp(names[(factory.n // 2) % len(names)])
        return _Resp(f"reply {factory.n}")
    factory.n = 0
    factory.models = []
    return factory


REQUEST = {
    "topic": "AI ethics",
    "cast": [
        {"name": "Ada", "persona": "ethicist", "goals": []},
        {"name": "Ben", "persona": "engineer", "goals": []},
    ],
}


async def test_engine_uses_per_run_config_model(db):
    fake = _always(["Ada", "Ben"])
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 2, "generate_avatars": False,
                         "model": "bedrock/custom-run-model"}
        await run_simulation(req, db=db)
    # Every LLM call (speaker selection + response) used the per-run model.
    assert fake.models, "no LLM calls captured"
    assert all(m == "bedrock/custom-run-model" for m in fake.models), fake.models


async def test_engine_falls_back_to_settings_model_when_unset(db):
    fake = _always(["Ada", "Ben"])
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 2, "generate_avatars": False}
        await run_simulation(req, db=db)
    from matrix_studio.settings import get_settings
    default = get_settings().litellm_model
    assert all(m == default for m in fake.models), fake.models


async def _mk_parent(db, run_id, cfg):
    await db.create_run(
        run_id=run_id, topic="AI ethics",
        cast=[{"name": "Ada", "persona": "e", "goals": []},
              {"name": "Ben", "persona": "eng", "goals": []}],
        config=cfg,
    )
    await db.update_run_status(run_id, "complete")
    return await db.get_run(run_id)


async def test_branch_explicit_model_override_wins(db):
    parent = await _mk_parent(db, "p1", {"max_messages": 4, "model": "bedrock/parent-model"})
    meta = await branching.create_branch_run(
        db, parent, from_turn=2, name="child-a", gen_model="bedrock/override-model"
    )
    assert meta["model"] == "bedrock/override-model"
    child = await db.get_run(meta["run_id"])
    import json
    assert json.loads(child["config_json"])["model"] == "bedrock/override-model"


async def test_branch_inherits_parent_model_when_not_imported(db):
    parent = await _mk_parent(db, "p2", {"max_messages": 4, "model": "bedrock/parent-model"})
    meta = await branching.create_branch_run(db, parent, from_turn=2, name="child-b")
    assert meta["model"] == "bedrock/parent-model"


async def test_branch_drops_stale_model_for_imported_parent(db):
    parent = await _mk_parent(
        db, "p3",
        {"max_messages": 4, "imported": True, "model": "bedrock/eol-legacy-model"},
    )
    meta = await branching.create_branch_run(db, parent, from_turn=2, name="child-c")
    assert meta["model"] is None  # -> settings default at generation/analysis
    child = await db.get_run(meta["run_id"])
    import json
    assert "model" not in json.loads(child["config_json"])


def test_api_models_exposes_allowlist(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from matrix_studio.api.app import create_app
    import matrix_studio.settings as settings_mod

    monkeypatch.setenv("LITELLM_MODEL", "bedrock/default-x")
    monkeypatch.setenv("AVAILABLE_MODELS", "bedrock/default-x, bedrock/opus-y , bedrock/sonnet-z")
    settings_mod._settings = None

    app = create_app(db_path=str(tmp_path / "m.db"))
    with TestClient(app) as c:
        body = c.get("/api/models").json()
    assert body["default"] == "bedrock/default-x"
    # Each model is {id, label}; default first, de-duplicated, order preserved.
    ids = [m["id"] for m in body["models"]]
    assert ids == ["bedrock/default-x", "bedrock/opus-y", "bedrock/sonnet-z"]
    # Unknown ids fall back to the id tail as the label.
    assert all(m["label"] for m in body["models"])
    settings_mod._settings = None
