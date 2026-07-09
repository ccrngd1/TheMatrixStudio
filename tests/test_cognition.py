# SPDX-License-Identifier: Apache-2.0
"""
Phase 2c cognition tests — Step 1: config flags + JSON-mode structured generation.

Covers:
  (1) cognition OFF (default) is byte-for-byte the pre-2c path: the LLM is called
      with NO response_format, and events carry no rationale/goal_served/reason keys.
  (2) CognitionConfig.from_config parsing (missing/invalid -> disabled default).
  (3) cognition ON: speaker.selected carries a `reason`; agent.response carries
      `rationale` + `goal_served`; the response call uses JSON-mode.
  (4) cognition ON with malformed JSON degrades gracefully (plain text utterance,
      no rationale/goal_served) and never stalls the run.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio.engine import run_simulation
from matrix_studio.state import CognitionConfig
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


REQUEST = {
    "topic": "AI ethics",
    "cast": [
        {"name": "Ada", "persona": "ethicist", "goals": ["seek truth"]},
        {"name": "Ben", "persona": "engineer", "goals": ["ship safely"]},
    ],
}


async def _events(db, run_id, event_type):
    rows = await db.get_events(run_id)
    out = []
    for r in rows:
        if r["event_type"] != event_type:
            continue
        payload = r["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload) if payload else {}
        out.append({"event_type": r["event_type"], "payload": payload})
    return out


# --------------------------------------------------------------------------- #
# (2) config parsing
# --------------------------------------------------------------------------- #

def test_cognition_config_defaults_off():
    c = CognitionConfig.from_config(None)
    assert c.enabled is False
    assert c.reflection_every == 4  # ON by default WHEN enabled
    assert c.goals_dynamic is False
    assert c.relationships is False
    assert c.retrieval_k == 5


def test_cognition_config_from_config_variants():
    assert CognitionConfig.from_config({}).enabled is False
    assert CognitionConfig.from_config({"cognition": "nope"}).enabled is False  # not a dict
    c = CognitionConfig.from_config({"cognition": {"enabled": True, "reflection_every": 0,
                                                   "bogus": 1}})
    assert c.enabled is True
    assert c.reflection_every == 0
    # unknown keys ignored, not an error
    assert not hasattr(c, "bogus")


# --------------------------------------------------------------------------- #
# (1) cognition OFF = pre-2c behavior
# --------------------------------------------------------------------------- #

async def test_cognition_off_no_json_mode_and_clean_payloads(db):
    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        n = len(calls)
        # odd call = speaker selection (name), even = response text
        return _Resp("Ada" if n % 2 == 1 else f"plain reply {n}")

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 2, "generate_avatars": False}
        await run_simulation(req, db=db, run_id="cog-off")

    # No call ever set response_format when cognition is off.
    assert calls, "no LLM calls captured"
    assert all("response_format" not in kw for kw in calls), "off path must not use JSON-mode"

    # Event payloads carry NO cognition keys (byte-for-byte pre-2c shape).
    for ev in await _events(db, "cog-off", "speaker.selected"):
        assert "reason" not in ev["payload"]
    resp = await _events(db, "cog-off", "agent.response")
    assert resp, "expected agent.response events"
    for ev in resp:
        assert "rationale" not in ev["payload"]
        assert "goal_served" not in ev["payload"]


# --------------------------------------------------------------------------- #
# (3) cognition ON captures reason / rationale / goal_served
# --------------------------------------------------------------------------- #

async def test_cognition_on_captures_structured_fields(db):
    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        n = len(calls)
        if n % 2 == 1:  # speaker selection -> JSON {speaker, reason}
            return _Resp(json.dumps({"speaker": "Ada", "reason": "Ada was addressed"}))
        # response -> JSON {utterance, rationale, goal_served}
        return _Resp(json.dumps({
            "utterance": "I think consent matters most.",
            "rationale": "I want to steer toward ethics.",
            "goal_served": "seek truth",
        }))

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 2, "generate_avatars": False,
                         "cognition": {"enabled": True}}
        await run_simulation(req, db=db, run_id="cog-on")

    # JSON-mode was requested on every call when cognition is on.
    assert calls
    assert all(kw.get("response_format") == {"type": "json_object"} for kw in calls)

    sel = await _events(db, "cog-on", "speaker.selected")
    assert sel and all(ev["payload"].get("reason") for ev in sel)

    resp = await _events(db, "cog-on", "agent.response")
    assert resp
    for ev in resp:
        assert ev["payload"].get("rationale")
        assert ev["payload"].get("goal_served")
        # the utterance, not the raw JSON, is what got recorded as the message
        assert ev["payload"]["message"] == "I think consent matters most."


# --------------------------------------------------------------------------- #
# (4) cognition ON with malformed JSON degrades gracefully
# --------------------------------------------------------------------------- #

async def test_cognition_on_bad_json_degrades_gracefully(db):
    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        n = len(calls)
        # Return non-JSON garbage for BOTH selection and response.
        return _Resp("Ada is the one" if n % 2 == 1 else "just a plain sentence, no json")

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 2, "generate_avatars": False,
                         "cognition": {"enabled": True}}
        # Must not raise despite unparseable structured output.
        await run_simulation(req, db=db, run_id="cog-bad")

    resp = await _events(db, "cog-bad", "agent.response")
    assert resp, "run should still complete and record turns"
    for ev in resp:
        # Graceful: raw text kept as the message, no fabricated rationale.
        assert ev["payload"]["message"] == "just a plain sentence, no json"
        assert "rationale" not in ev["payload"]
        assert "goal_served" not in ev["payload"]


# --------------------------------------------------------------------------- #
# (5) API model no longer drops the cognition config
# --------------------------------------------------------------------------- #

def test_api_run_config_preserves_cognition():
    """The create-run model must carry cognition through to the engine config
    (RunConfigModel is strict; a regression here would silently disable 2c)."""
    from matrix_studio.api.app import CreateRunModel

    body = CreateRunModel(
        topic="t",
        cast=[{"name": "Ada", "persona": "p", "goals": []}],
        config={"max_messages": 2, "generate_avatars": False,
                "cognition": {"enabled": True, "reflection_every": 4}},
    )
    dumped = body.model_dump(exclude_none=True)
    assert dumped["config"]["cognition"]["enabled"] is True
    # And the engine parser accepts that shape.
    c = CognitionConfig.from_config(dumped["config"])
    assert c.enabled is True and c.reflection_every == 4
