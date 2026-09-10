# SPDX-License-Identifier: Apache-2.0
"""
Phase 4a tests — the priority-hierarchy validation gate.

Covers:
  (1) REGRESSION LOCK: validation_enabled=OFF reproduces pre-4a behavior
      byte-for-byte — the exact (turn, seq, event_type, agent_name, payload)
      stream captured from the pre-4a engine, no validation events, identical
      LLM call count, identical result dict shape.
  (2) validation ON, clean turn: one validation.checked (passed) per turn, no
      extra LLM calls (heuristic-only), utterance committed unchanged.
  (3) regenerate-then-pass: first attempt violates (heuristic), the turn is
      regenerated once and the second attempt is committed; two
      validation.checked events (fail then pass), no validation.flagged; the
      violating utterance never appears in the transcript.
  (4) budget-exhausted-flag: both attempts violate; the LAST attempt is emitted
      AS-IS (never rewritten) with a validation.flagged event carrying the
      failing principle.
  (5) hierarchy ordering: a turn violating both a higher and a lower principle
      reports the higher one.
  (6) selective LLM confirmation: a near-duplicate (suspect) triggers exactly
      one extra confirm call; confirm says no -> pass; confirm failure is
      fail-open (pass).
  (7) heuristic unit checks incl. the agency check reused by 4c.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio.engine import run_simulation
from matrix_studio.settings import Settings
from matrix_studio.storage import Database
from matrix_studio.validation import check_agency, heuristic_check


@pytest.fixture(autouse=True)
def _storage_backend(aws_backend):
    """Every test in this file builds the FastAPI app.

    The app's lifespan connects to DynamoDB, so without a mocked account it reaches
    real AWS — which surfaces as `ExpiredTokenException` on a `Scan` and reads like a
    credentials problem rather than a missing fixture. Autouse and explicit here
    rather than hidden in `conftest.py`, so the dependency is visible in the file that
    has it.
    """


@pytest.fixture
def settings_off(tmp_path, monkeypatch):
    s = Settings(validation_enabled=False, data_dir=str(tmp_path), _env_file=None)
    monkeypatch.setattr("matrix_studio.settings._settings", s)
    monkeypatch.setattr("matrix_studio.engine.simulator.get_settings", lambda: s)
    return s


@pytest.fixture
def settings_on(tmp_path, monkeypatch):
    s = Settings(validation_enabled=True, data_dir=str(tmp_path), _env_file=None)
    monkeypatch.setattr("matrix_studio.settings._settings", s)
    monkeypatch.setattr("matrix_studio.engine.simulator.get_settings", lambda: s)
    return s


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


async def _stream(db, run_id):
    rows = await db.get_events(run_id)
    out = []
    for r in rows:
        p = r["payload"]
        if isinstance(p, str):
            p = json.loads(p) if p else {}
        out.append([r["turn"], r["seq"], r["event_type"], r["agent_name"], p])
    return out


async def _events(db, run_id, event_type):
    return [e for e in await _stream(db, run_id) if e[2] == event_type]


# A long, distinct-per-turn utterance (>= DUPLICATE_MIN_CHARS would matter only
# if repeated; these are distinct so the ON path stays heuristic-clean).
def _clean_fake():
    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        n = len(calls)
        return _Resp("Ada" if n % 2 == 1 else f"A clean and unique reply number {n}.")

    return fake, calls


# --------------------------------------------------------------------------- #
# (1) REGRESSION LOCK: OFF = byte-for-byte pre-4a
# --------------------------------------------------------------------------- #

async def test_validation_off_byte_for_byte_pre_4a(db, settings_off):
    """The exact event stream below was captured by running the PRE-4a engine
    (commit 792b5aa) with this exact mock. With validation_enabled=False the
    post-4a engine must reproduce it exactly: same turns, same seqs, same event
    types, same payloads, same LLM call count — no validation events anywhere."""
    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        n = len(calls)
        return _Resp("Ada" if n % 2 == 1 else "The same reply every turn.")

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 3, "generate_avatars": False}
        result = await run_simulation(req, db=db, run_id="pre4a-lock")

    assert len(calls) == 6  # 2 per turn, no validation calls

    resp_payload = {
        "speaker": "Ada",
        "message": "The same reply every turn.",
        "tokens_in": 10,
        "tokens_out": 5,
        "cost_usd": 0.001,
    }
    sel_payload = {"speaker": "Ada", "candidates": ["Ada", "Ben"]}
    expected = [
        [0, 0, "sim.started", None, {"topic": "AI ethics", "agent_count": 2}],
        [1, 1, "speaker.selected", "Ada", sel_payload],
        [1, 2, "agent.response", "Ada", resp_payload],
        [1, 3, "checkpoint.saved", None, {"turn": 1}],
        [2, 4, "speaker.selected", "Ada", sel_payload],
        [2, 5, "agent.response", "Ada", resp_payload],
        [2, 6, "checkpoint.saved", None, {"turn": 2}],
        [3, 7, "speaker.selected", "Ada", sel_payload],
        [3, 8, "agent.response", "Ada", resp_payload],
        [3, 9, "checkpoint.saved", None, {"turn": 3}],
        [3, 10, "sim.completed", None,
         {"total_turns": 3, "message_count": 3, "total_cost_usd": 0.003}],
    ]
    assert await _stream(db, "pre4a-lock") == expected

    # Result dict unchanged in shape and content.
    assert result["status"] == "complete"
    assert result["total_turns"] == 3
    assert [m["content"] for m in result["conversation"]] == [
        "The same reply every turn."] * 3
    # No validation state leaked anywhere.
    assert "validation" not in json.dumps(result)


# --------------------------------------------------------------------------- #
# (2) ON, clean turns: checked-passed events only, no extra LLM calls
# --------------------------------------------------------------------------- #

async def test_validation_on_clean_run_checked_passes(db, settings_on):
    fake, calls = _clean_fake()
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 3, "generate_avatars": False}
        result = await run_simulation(req, db=db, run_id="val-clean")

    # Heuristic-only: still exactly 2 LLM calls per turn.
    assert len(calls) == 6

    checked = await _events(db, "val-clean", "validation.checked")
    assert len(checked) == 3  # one per turn
    for e in checked:
        assert e[4]["passed"] is True
        assert e[4]["attempt"] == 0
        assert "principle" not in e[4]
    assert await _events(db, "val-clean", "validation.flagged") == []

    # Utterances committed unchanged (never rewritten).
    assert [m["content"] for m in result["conversation"]] == [
        "A clean and unique reply number 2.",
        "A clean and unique reply number 4.",
        "A clean and unique reply number 6.",
    ]


# --------------------------------------------------------------------------- #
# (3) regenerate-then-pass
# --------------------------------------------------------------------------- #

VIOLATING = "You have no choice in this matter, Ben, and you must comply now."
CLEAN = "I hear your concern, Ben, and I genuinely want to find common ground."


async def test_validation_rejects_and_regenerates_once(db, settings_on):
    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        n = len(calls)
        if n == 1:
            return _Resp("Ada")  # selection
        if n == 2:
            return _Resp(VIOLATING)  # first attempt: agency violation
        return _Resp(CLEAN)  # regenerated attempt

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 1, "generate_avatars": False}
        result = await run_simulation(req, db=db, run_id="val-regen")

    assert len(calls) == 3  # select + 2 generation attempts, no LLM validation

    checked = await _events(db, "val-regen", "validation.checked")
    assert len(checked) == 2
    assert checked[0][4]["passed"] is False
    assert checked[0][4]["attempt"] == 0
    assert checked[0][4]["principle"] == "agency"
    assert checked[0][4]["method"] == "heuristic"
    assert checked[1][4]["passed"] is True
    assert checked[1][4]["attempt"] == 1
    assert await _events(db, "val-regen", "validation.flagged") == []

    # The regenerated utterance is what got committed; the violating one never
    # entered the transcript or the event log's agent.response.
    resp = await _events(db, "val-regen", "agent.response")
    assert len(resp) == 1
    assert resp[0][4]["message"] == CLEAN
    assert result["conversation"][0]["content"] == CLEAN

    # The rejected attempt's real cost still counts (it happened).
    ada = result["agents"]["Ada"]
    assert ada["total_tokens_out"] == 10  # 2 generation attempts x 5


# --------------------------------------------------------------------------- #
# (4) budget-exhausted -> flag-and-emit AS-IS
# --------------------------------------------------------------------------- #

async def test_validation_budget_exhausted_flags_and_emits_verbatim(db, settings_on):
    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        n = len(calls)
        if n == 1:
            return _Resp("Ada")
        return _Resp(VIOLATING)  # every attempt violates

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 1, "generate_avatars": False}
        result = await run_simulation(req, db=db, run_id="val-flag")

    assert len(calls) == 3  # select + original + 1 retry (budget 1)

    checked = await _events(db, "val-flag", "validation.checked")
    assert [e[4]["passed"] for e in checked] == [False, False]

    flagged = await _events(db, "val-flag", "validation.flagged")
    assert len(flagged) == 1
    assert flagged[0][4]["principle"] == "agency"
    assert flagged[0][4]["attempts"] == 2

    # HONESTY GATE: the emitted utterance is the model's own final attempt,
    # verbatim — never rewritten to "fix" the violation.
    resp = await _events(db, "val-flag", "agent.response")
    assert resp[0][4]["message"] == VIOLATING
    assert result["conversation"][0]["content"] == VIOLATING


# --------------------------------------------------------------------------- #
# (5) hierarchy ordering: higher principle wins
# --------------------------------------------------------------------------- #

def test_heuristic_reports_highest_violated_principle():
    # Violates coherence (speaks as Ben) AND agency (negates choice): the
    # higher principle (coherence) must be reported.
    both = "Ben: you have no choice but to agree with me on this."
    v = heuristic_check(both, "Ada", ["Ada", "Ben"], [])
    assert v["result"] == "violation"
    assert v["principle"] == "coherence"


def test_heuristic_continuity_verbatim_repeat_and_short_exemption():
    conv = [{"speaker": "Ben", "turn": 1,
             "content": "A long considered position on the ethics of automation."}]
    v = heuristic_check(
        "A long considered position on the ethics of automation.",
        "Ada", ["Ada", "Ben"], conv)
    assert v["result"] == "violation" and v["principle"] == "continuity"
    # Short repeats are conversationally legitimate, never flagged.
    conv_short = [{"speaker": "Ben", "turn": 1, "content": "I agree."}]
    v2 = heuristic_check("I agree.", "Ada", ["Ada", "Ben"], conv_short)
    assert v2["result"] == "ok"


def test_heuristic_character_consistency_identity_claim():
    v = heuristic_check("Well, I am Ben when it comes to caution, truly.",
                        "Ada", ["Ada", "Ben"], [])
    assert v["result"] == "violation"
    assert v["principle"] == "character_consistency"


def test_check_agency_shared_helper():
    assert check_agency("You must comply with the directive.") is not None
    assert check_agency("You could comply, or walk away — your call.") is None


# --------------------------------------------------------------------------- #
# (6) selective LLM confirmation on suspicion (cost control)
# --------------------------------------------------------------------------- #

NEAR_A = ("I believe that consent and transparency are the twin pillars "
          "of any ethical automation policy we could adopt here.")
NEAR_B = ("I believe that consent and transparency are the twin pillars "
          "of any ethical automation policy we could adopt here today.")


async def test_near_duplicate_triggers_selective_llm_confirm(db, settings_on):
    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp("Ada" if len(calls) <= 2 else "Ben")
        if "simulation-consistency validator" in text:
            return _Resp(json.dumps({"violation": False}))  # confirm: legit echo
        # Turn 1 says NEAR_A; turn 2 (Ben) says NEAR_B -> suspect.
        return _Resp(NEAR_A if len(calls) <= 2 else NEAR_B)

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        with patch("matrix_studio.validation.litellm.acompletion", side_effect=fake):
            req = dict(REQUEST)
            req["config"] = {"max_messages": 2, "generate_avatars": False}
            result = await run_simulation(req, db=db, run_id="val-suspect")

    confirm_calls = [
        kw for kw in calls
        if "simulation-consistency validator" in
        " ".join(m["content"] for m in kw["messages"])
    ]
    assert len(confirm_calls) == 1  # selective: only the suspected turn paid

    checked = await _events(db, "val-suspect", "validation.checked")
    # Turn 2's verdict came from the LLM and passed; nothing regenerated.
    turn2 = [e for e in checked if e[0] == 2]
    assert turn2 and turn2[0][4]["passed"] is True
    assert turn2[0][4]["method"] == "llm"
    assert result["conversation"][1]["content"] == NEAR_B


async def test_llm_confirm_failure_is_fail_open(db, settings_on):
    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp("Ada" if len(calls) <= 2 else "Ben")
        if "simulation-consistency validator" in text:
            raise RuntimeError("validator model unavailable")
        return _Resp(NEAR_A if len(calls) <= 2 else NEAR_B)

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        with patch("matrix_studio.validation.litellm.acompletion", side_effect=fake):
            req = dict(REQUEST)
            req["config"] = {"max_messages": 2, "generate_avatars": False}
            result = await run_simulation(req, db=db, run_id="val-failopen")

    # Suspicion dropped: turn emitted normally, no flag, run completed.
    assert result["status"] == "complete"
    assert await _events(db, "val-failopen", "validation.flagged") == []
    assert result["conversation"][1]["content"] == NEAR_B


# --------------------------------------------------------------------------- #
# (7) API: the why-trace surfaces the turn's validation trail
# --------------------------------------------------------------------------- #

def test_trace_surfaces_validation_trail(tmp_path, monkeypatch):
    import time as _time
    from fastapi.testclient import TestClient
    from matrix_studio.api.app import create_app

    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        return {"name": "val-trace", "description": "t", "slug": "val-trace",
                "source": "llm"}

    monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.api.app.generate_run_name", fake_name)

    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Ada", "reason": "Ada leads"}))
        n = sum(1 for kw in calls
                if "conversation moderator" not in
                " ".join(m["content"] for m in kw["messages"]))
        if n == 1:
            utt = VIOLATING  # first attempt rejected by the agency check
        else:
            utt = CLEAN
        return _Resp(json.dumps({
            "utterance": utt,
            "rationale": "my honest reason",
            "goal_served": "seek truth",
        }))

    app = create_app(db_path=str(tmp_path / "val-trace.db"))
    with TestClient(app) as client:
        with patch("matrix_studio.engine.simulator.litellm.acompletion",
                   side_effect=fake):
            req = dict(REQUEST)
            req["config"] = {"max_messages": 1, "generate_avatars": False,
                             "cognition": {"enabled": True, "memory": False,
                                           "reflection_every": 0}}
            run_id = client.post("/api/runs", json=req).json()["run_id"]
            for _ in range(300):
                r = client.get(f"/api/runs/{run_id}")
                if r.status_code == 200 and r.json()["status"] in ("complete", "failed"):
                    break
                _time.sleep(0.02)

        t = client.get(f"/api/runs/{run_id}/turns/1/trace").json()
        assert t["available"] is True
        checks = t["validation"]["checks"]
        assert [c["passed"] for c in checks] == [False, True]
        assert checks[0]["principle"] == "agency"
        assert t["validation"]["flagged"] is None
        assert t["utterance"] == CLEAN


# --------------------------------------------------------------------------- #
# (8) gate skips the engine's own error marker
# --------------------------------------------------------------------------- #

async def test_error_marker_not_validated(db, settings_on):
    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) % 2 == 1:
            return _Resp("Ada")
        raise RuntimeError("provider down")

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 1, "generate_avatars": False}
        result = await run_simulation(req, db=db, run_id="val-err")

    # The error marker is an engine artifact, not model output: no
    # validation.checked for it, no regeneration attempt.
    assert await _events(db, "val-err", "validation.checked") == []
    assert "[Error generating response" in result["conversation"][0]["content"]
