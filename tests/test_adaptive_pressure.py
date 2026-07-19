# SPDX-License-Identifier: Apache-2.0
"""
Phase 4c tests — adaptive-pressure intervention (EXPERIMENTAL, opt-in).

Covers:
  (1) OFF by default: the API refuses the mutation kind with 422 (no branch
      run row is created), and the engine dispatch refuses it too.
  (2) signals: observe_signals computes repetition / stale threads / budget
      from real state only (pure unit).
  (3) ON: one pressure branch injects a Narrator world event as a real branch
      turn (source "pressure"), preceded by a pressure.applied audit event
      carrying the observed signals; the parent run is untouched.
  (4) HARD agency guard: a generated pressure that negates participant choice
      is regenerated once; if the retry still violates, the intervention is
      REJECTED (BranchMutationError; nothing emitted, nothing rewritten) —
      the same check_agency the 4a gate uses.
  (5) generation failure rejects rather than fabricating a fallback event.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio import branching
from matrix_studio.engine import run_simulation
from matrix_studio.engine.simulator import BranchMutationError
from matrix_studio.pressure import PressureRejectedError, generate_pressure, observe_signals
from matrix_studio.settings import Settings
from matrix_studio.state import PendingThread
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


def _mk_settings(monkeypatch, tmp_path, **kw):
    s = Settings(data_dir=str(tmp_path), _env_file=None, **kw)
    monkeypatch.setattr("matrix_studio.settings._settings", s)
    monkeypatch.setattr("matrix_studio.engine.simulator.get_settings", lambda: s)
    monkeypatch.setattr("matrix_studio.branching.get_settings", lambda: s)
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

PRESSURE_TEXT = ("An urgent bulletin lands: the regulator has moved the audit "
                 "deadline to tomorrow morning. The room's screens all light up "
                 "with the same red banner.")
VIOLATING_PRESSURE = ("The regulator rules that Ada and Ben must comply; "
                      "you have no choice in the matter anymore.")


def _plain_fake():
    n = {"i": 0}
    def fake(*args, **kwargs):
        n["i"] += 1
        return _Resp("Ada" if n["i"] % 2 == 1 else f"reply number {n['i']} on ethics")
    return fake


async def _seed_parent(db, run_id="press-parent", max_messages=2):
    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_plain_fake()):
        req = dict(REQUEST)
        req["config"] = {"max_messages": max_messages, "generate_avatars": False}
        await run_simulation(req, db=db, run_id=run_id)
    return await db.get_run(run_id)


async def _events(db, run_id, event_type=None):
    rows = await db.get_events(run_id)
    out = []
    for r in rows:
        if event_type and r["event_type"] != event_type:
            continue
        p = r["payload"]
        if isinstance(p, str):
            p = json.loads(p) if p else {}
        out.append({"turn": r["turn"], "seq": r["seq"],
                    "event_type": r["event_type"], "agent": r["agent_name"],
                    "payload": p})
    return out


# --------------------------------------------------------------------------- #
# (1) OFF by default
# --------------------------------------------------------------------------- #

def test_api_refuses_adaptive_pressure_when_disabled(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from matrix_studio.api.app import create_app

    _mk_settings(monkeypatch, tmp_path)  # adaptive_pressure_enabled default False

    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        return {"name": "p-run", "description": "t", "slug": "p-run", "source": "llm"}

    monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.api.app.generate_run_name", fake_name)

    app = create_app(db_path=str(tmp_path / "p.db"))
    with TestClient(app) as client:
        with patch("matrix_studio.engine.simulator.litellm.acompletion",
                   side_effect=_plain_fake()):
            req = dict(REQUEST)
            req["config"] = {"max_messages": 1, "generate_avatars": False}
            run_id = client.post("/api/runs", json=req).json()["run_id"]
            import time as _t
            for _ in range(300):
                r = client.get(f"/api/runs/{run_id}")
                if r.json().get("status") in ("complete", "failed"):
                    break
                _t.sleep(0.02)

        runs_before = len(client.get("/api/runs").json()["runs"])
        r = client.post(f"/api/runs/{run_id}/branch",
                        json={"from_turn": 1,
                              "mutation": {"kind": "adaptive_pressure"}})
        assert r.status_code == 422
        assert "experimental" in r.json()["detail"]
        # No branch run row was created for the refused mutation.
        assert len(client.get("/api/runs").json()["runs"]) == runs_before


async def test_engine_refuses_adaptive_pressure_when_disabled(db, tmp_path, monkeypatch):
    settings = _mk_settings(monkeypatch, tmp_path)
    parent = await _seed_parent(db)
    meta = await branching.create_branch_run(db, parent, from_turn=1, name="p-off")
    with pytest.raises(Exception) as exc_info:
        await branching.execute_branch(
            db, parent, branch_run_id=meta["run_id"], from_turn=1,
            max_messages=meta["max_messages"],
            mutation={"kind": "adaptive_pressure"},
        )
    assert "disabled" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# (2) signals are computed from real state only
# --------------------------------------------------------------------------- #

def test_observe_signals_pure():
    conv = [
        {"speaker": "Ada", "content": "the audit matters for consent and safety", "turn": 1},
        {"speaker": "Ben", "content": "the audit matters for consent and safety today", "turn": 2},
    ]
    threads = [
        PendingThread(description="old dangling setup", origin_turn=1),
        PendingThread(description="fresh", origin_turn=7),
        PendingThread(description="closed", origin_turn=1, status="resolved"),
    ]
    s = observe_signals(conv, threads, from_turn=8, max_messages=10, stale_after=5)
    assert s["repetition"] > 0.6           # near-identical messages
    assert s["open_threads"] == 2          # resolved not counted
    assert [t["description"] for t in s["stale_threads"]] == ["old dangling setup"]
    assert s["stale_threads"][0]["age"] == 7
    assert s["budget_remaining"] == 2
    # empty inputs
    s0 = observe_signals([], [], from_turn=0, max_messages=5)
    assert s0["repetition"] == 0.0 and s0["stale_threads"] == []


# --------------------------------------------------------------------------- #
# (3) ON: pressure branch = audit event + injected narrator world event
# --------------------------------------------------------------------------- #

async def test_pressure_branch_injects_world_event(db, tmp_path, monkeypatch):
    _mk_settings(monkeypatch, tmp_path, adaptive_pressure_enabled=True)
    parent = await _seed_parent(db)
    parent_events_before = await _events(db, "press-parent")

    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "narrator of a simulated conversation" in text:
            return _Resp(PRESSURE_TEXT)
        n = fake.__dict__.setdefault("n", [0])
        n[0] += 1
        return _Resp("Ada" if n[0] % 2 == 1 else "responding to the deadline news")

    meta = await branching.create_branch_run(
        db, parent, from_turn=1, name="p-on",
        mutation={"kind": "adaptive_pressure", "add_budget": 1})
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        with patch("matrix_studio.pressure.litellm.acompletion", side_effect=fake):
            result = await branching.execute_branch(
                db, parent, branch_run_id=meta["run_id"], from_turn=1,
                max_messages=meta["max_messages"],
                mutation={"kind": "adaptive_pressure", "add_budget": 1},
            )

    assert result["status"] == "complete"

    # Audit event with the real observed signals.
    applied = await _events(db, meta["run_id"], "pressure.applied")
    assert len(applied) == 1
    assert applied[0]["turn"] == 2  # injected at fork+1
    sig = applied[0]["payload"]["signals"]
    assert sig["as_of_turn"] == 1
    assert "repetition" in sig and "budget_remaining" in sig
    assert applied[0]["payload"]["attempts"] == 1

    # The world event is a real injected narrator branch turn.
    responses = await _events(db, meta["run_id"], "agent.response")
    injected = [e for e in responses if e["payload"].get("injected")]
    assert len(injected) == 1
    assert injected[0]["agent"] == "Narrator"
    assert injected[0]["payload"]["message"] == PRESSURE_TEXT
    assert injected[0]["payload"]["source"] == "pressure"

    # The parent run is untouched.
    assert await _events(db, "press-parent") == parent_events_before


# --------------------------------------------------------------------------- #
# (4) HARD agency guard: regenerate once, then REJECT (never emit/rewrite)
# --------------------------------------------------------------------------- #

async def test_agency_guard_regenerates_then_accepts(tmp_path, monkeypatch):
    s = Settings(data_dir=str(tmp_path), _env_file=None,
                 adaptive_pressure_enabled=True)
    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        # 1st attempt violates agency; the retry is clean.
        return _Resp(VIOLATING_PRESSURE if len(calls) == 1 else PRESSURE_TEXT)

    with patch("matrix_studio.pressure.litellm.acompletion", side_effect=fake):
        out = await generate_pressure(
            topic="t", conversation=[], participants=["Ada", "Ben"],
            signals=observe_signals([], [], 1, 5), settings=s)
    assert len(calls) == 2
    assert out["content"] == PRESSURE_TEXT
    assert out["attempts"] == 2


async def test_agency_guard_rejects_after_budget(tmp_path, monkeypatch):
    s = Settings(data_dir=str(tmp_path), _env_file=None,
                 adaptive_pressure_enabled=True)
    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        return _Resp(VIOLATING_PRESSURE)  # every attempt violates

    with patch("matrix_studio.pressure.litellm.acompletion", side_effect=fake):
        with pytest.raises(PressureRejectedError) as exc_info:
            await generate_pressure(
                topic="t", conversation=[], participants=["Ada", "Ben"],
                signals=observe_signals([], [], 1, 5), settings=s)
    assert len(calls) == 2  # original + 1 retry
    assert "agency" in str(exc_info.value)


async def test_rejected_pressure_emits_nothing_on_branch(db, tmp_path, monkeypatch):
    """End-to-end: an always-violating generator rejects the WHOLE intervention
    — no pressure.applied, no injected turn on the branch."""
    _mk_settings(monkeypatch, tmp_path, adaptive_pressure_enabled=True)
    parent = await _seed_parent(db, run_id="press-rej")

    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "narrator of a simulated conversation" in text:
            return _Resp(VIOLATING_PRESSURE)
        return _Resp("Ada")

    meta = await branching.create_branch_run(
        db, parent, from_turn=1, name="p-rej",
        mutation={"kind": "adaptive_pressure"})
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        with patch("matrix_studio.pressure.litellm.acompletion", side_effect=fake):
            with pytest.raises(BranchMutationError):
                await branching.execute_branch(
                    db, parent, branch_run_id=meta["run_id"], from_turn=1,
                    max_messages=meta["max_messages"],
                    mutation={"kind": "adaptive_pressure"},
                )

    assert await _events(db, meta["run_id"], "pressure.applied") == []
    responses = await _events(db, meta["run_id"], "agent.response")
    # Only the copied parent events exist; nothing injected/generated.
    assert all(not e["payload"].get("injected") for e in responses)


# --------------------------------------------------------------------------- #
# (5) generation failure rejects (no fabricated fallback)
# --------------------------------------------------------------------------- #

async def test_generation_failure_rejects(tmp_path):
    s = Settings(data_dir=str(tmp_path), _env_file=None,
                 adaptive_pressure_enabled=True)

    def fake(*args, **kwargs):
        raise RuntimeError("provider down")

    with patch("matrix_studio.pressure.litellm.acompletion", side_effect=fake):
        with pytest.raises(PressureRejectedError):
            await generate_pressure(
                topic="t", conversation=[], participants=["Ada"],
                signals=observe_signals([], [], 1, 5), settings=s)
