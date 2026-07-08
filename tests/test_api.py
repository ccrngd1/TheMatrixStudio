# SPDX-License-Identifier: Apache-2.0
"""
Backend API tests — REST routes, run lifecycle, and WebSocket connect/replay/
stream. The engine and avatar generation are MOCKED throughout: the real
environment carries a Bedrock key, so unmocked tests would make live billable
calls. These tests exercise the Phase 1 server plumbing only.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from matrix_studio.api.app import create_app
from matrix_studio.engine.simulator import OnEvent


# --------------------------------------------------------------------------- #
# A deterministic fake engine: instead of calling litellm, it walks a scripted
# set of events through the same on_event/db path the real engine uses.
# --------------------------------------------------------------------------- #
def make_fake_run(turns=2, fail=False, delay=0.0):
    """Build a fake run_simulation coroutine that emits scripted events."""

    async def fake_run_simulation(request, db=None, run_id=None, on_event=None):
        topic = request["topic"]
        cast = request["cast"]
        names = [c["name"] for c in cast]
        seq = 0

        async def emit(turn, event_type, payload, agent_name=None):
            nonlocal seq
            if db:
                await db.append_event(
                    run_id=run_id, turn=turn, seq=seq, event_type=event_type,
                    agent_name=agent_name, payload=payload,
                )
            if on_event:
                await on_event({
                    "run_id": run_id, "turn": turn, "seq": seq,
                    "event_type": event_type, "agent_name": agent_name,
                    "payload": payload,
                })
            seq += 1

        if db:
            await db.create_run(
                run_id=run_id, topic=topic, cast=cast,
                name=request.get("name"), description=request.get("description"),
                slug=request.get("name"), config=request.get("config"),
            )
            await db.update_run_status(run_id, "running")

        await emit(0, "sim.started", {"topic": topic, "agent_count": len(names)})

        conversation = []
        for t in range(1, turns + 1):
            if delay:
                await asyncio.sleep(delay)
            speaker = names[(t - 1) % len(names)]
            await emit(t, "speaker.selected", {"speaker": speaker, "candidates": names}, speaker)
            content = f"Message {t} from {speaker}"
            conversation.append({"speaker": speaker, "content": content, "turn": t})
            await emit(t, "agent.response", {
                "speaker": speaker, "message": content,
                "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.001,
            }, speaker)

        if fail:
            await emit(turns, "sim.failed", {"error": "boom"})
            if db:
                await db.update_run_status(run_id, "failed")
            return {"run_id": run_id, "status": "failed", "error": "boom",
                    "topic": topic, "conversation": conversation, "total_turns": turns}

        await emit(turns, "sim.completed", {
            "total_turns": turns, "message_count": len(conversation),
            "total_cost_usd": 0.001 * turns,
        })
        if db:
            from matrix_studio.state import AgentState, SimSnapshot
            agents = {n: AgentState(name=n, persona="p", goals=["g"]) for n in names}
            snap = SimSnapshot(
                run_id=run_id, turn=turns, topic=topic, agents=agents,
                conversation=conversation, status="complete",
                created_at=0, completed_at=0, total_turns=turns,
            )
            await db.save_snapshot(snap)
            await db.update_run_status(run_id, "complete", 1)

        return {"run_id": run_id, "status": "complete", "topic": topic,
                "conversation": conversation,
                "agents": {n: {"name": n} for n in names},
                "total_turns": turns, "total_cost_usd": 0.001 * turns}

    return fake_run_simulation


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient over the app with a temp DB and a mocked naming call."""
    db_path = str(tmp_path / "test.db")

    # Mock the codename LLM call so run creation is deterministic and offline.
    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        base = "trusted-robot"
        name = base
        n = 2
        while name_exists is not None and await _maybe(name_exists(name)):
            name = f"{base}-{n}"
            n += 1
        return {"name": name, "description": "A test simulation", "slug": name, "source": "llm"}

    async def _maybe(v):
        if hasattr(v, "__await__"):
            return await v
        return v

    monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.api.app.generate_run_name", fake_name)

    app = create_app(db_path=db_path)
    with TestClient(app) as c:
        yield c


REQUEST = {
    "topic": "AI ethics and responsibility",
    "cast": [
        {"name": "Ada", "persona": "An ethicist", "goals": ["Question assumptions"]},
        {"name": "Ben", "persona": "An engineer", "goals": ["Ship safely"]},
    ],
    "config": {"max_messages": 2, "generate_avatars": False},
}


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_models_endpoint(client):
    r = client.get("/api/models")
    assert r.status_code == 200
    body = r.json()
    assert "default" in body and "models" in body
    # EOL model must not appear.
    assert "claude-3-5-sonnet-20241022" not in body["default"]


def test_name_suggest(client):
    r = client.get("/api/name/suggest", params={"topic": "AI ethics"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "trusted-robot"
    assert body["description"]


def test_create_run_nonblocking_and_named(client):
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=2)):
        r = client.post("/api/runs", json=REQUEST)
    assert r.status_code == 201
    body = r.json()
    assert "run_id" in body
    assert body["name"] == "trusted-robot"
    assert body["status"] == "running"


def test_create_run_requires_cast(client):
    bad = {"topic": "x", "cast": []}
    r = client.post("/api/runs", json=bad)
    assert r.status_code == 422


def _wait_complete(client, ref, tries=50):
    for _ in range(tries):
        r = client.get(f"/api/runs/{ref}")
        if r.status_code == 200 and r.json()["status"] in ("complete", "failed"):
            return r.json()
        import time
        time.sleep(0.02)
    return client.get(f"/api/runs/{ref}").json()


def test_run_lifecycle_and_get_by_id_and_name(client):
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=2)):
        r = client.post("/api/runs", json=REQUEST)
        body = r.json()
        run_id, name = body["run_id"], body["name"]

        # Fetch by id
        detail = _wait_complete(client, run_id)
        assert detail["status"] == "complete"
        assert detail["result"] is not None
        assert len(detail["result"]["conversation"]) == 2

        # Fetch by memorable name — must resolve the same run
        by_name = client.get(f"/api/runs/{name}")
        assert by_name.status_code == 200
        assert by_name.json()["run_id"] == run_id


def test_list_and_filter_runs(client):
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=1)):
        client.post("/api/runs", json=REQUEST)
        _wait_complete(client, "trusted-robot")

    listing = client.get("/api/runs").json()["runs"]
    assert len(listing) >= 1
    assert listing[0]["name"] == "trusted-robot"

    # Filter by name substring
    filtered = client.get("/api/runs", params={"q": "trusted"}).json()["runs"]
    assert any(r["name"] == "trusted-robot" for r in filtered)
    # Non-matching filter yields nothing
    none = client.get("/api/runs", params={"q": "zzzznomatch"}).json()["runs"]
    assert none == []


def test_get_run_404(client):
    r = client.get("/api/runs/does-not-exist")
    assert r.status_code == 404


def test_events_endpoint_and_after_seq(client):
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=2)):
        run_id = client.post("/api/runs", json=REQUEST).json()["run_id"]
        _wait_complete(client, run_id)

    all_events = client.get(f"/api/runs/{run_id}/events").json()["events"]
    types = [e["event_type"] for e in all_events]
    assert "sim.started" in types
    assert "agent.response" in types
    assert "sim.completed" in types

    # after_seq paging: everything after seq 0 excludes sim.started (seq 0)
    tail = client.get(f"/api/runs/{run_id}/events", params={"after_seq": 0}).json()["events"]
    assert all(e["seq"] > 0 for e in tail)
    assert "sim.started" not in [e["event_type"] for e in tail]


def test_ws_replay_completed_run(client):
    """A late joiner connecting AFTER completion catches up via replay."""
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=2)):
        run_id = client.post("/api/runs", json=REQUEST).json()["run_id"]
        _wait_complete(client, run_id)

        received = []
        with client.websocket_connect(f"/api/runs/{run_id}/stream") as ws:
            while True:
                msg = ws.receive_json()
                received.append(msg)
                if msg["event_type"] in ("sim.completed", "sim.failed"):
                    break

    types = [e["event_type"] for e in received]
    assert types[0] == "sim.started"
    assert types[-1] == "sim.completed"
    assert types.count("agent.response") == 2
    # Sequence numbers strictly increasing (no dupes from replay+live overlap)
    seqs = [e["seq"] for e in received]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))


def test_ws_live_stream_midrun(client):
    """Connect mid-run: replay what exists, then continue live to completion."""
    # A slow fake run so the WS connects while it is still running.
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=3, delay=0.05)):
        run_id = client.post("/api/runs", json=REQUEST).json()["run_id"]

        received = []
        with client.websocket_connect(f"/api/runs/{run_id}/stream") as ws:
            while True:
                msg = ws.receive_json()
                received.append(msg)
                if msg["event_type"] in ("sim.completed", "sim.failed"):
                    break

    types = [e["event_type"] for e in received]
    assert types[-1] == "sim.completed"
    assert types.count("agent.response") == 3
    seqs = [e["seq"] for e in received]
    assert len(seqs) == len(set(seqs))  # no duplicates across replay/live boundary


def test_ws_run_not_found(client):
    with client.websocket_connect("/api/runs/nope/stream") as ws:
        msg = ws.receive_json()
        assert msg["event_type"] == "error"
