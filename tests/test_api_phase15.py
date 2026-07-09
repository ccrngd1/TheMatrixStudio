# SPDX-License-Identifier: Apache-2.0
"""
Phase 1.5 API tests: auto-summary at completion (default on), summary config
(disabled / focus / on-demand), aside thread lifecycle (analyst / persona /
room, multi-turn, isolation), imported-summary separation, and the READ-ONLY
invariant end-to-end (events/snapshot/cost unchanged by aside+summary activity).

The engine is replaced by the deterministic fake from test_api, and the
analysis LLM seam is mocked by the autouse conftest fixture — no live calls.
"""

import time

import pytest
from fastapi.testclient import TestClient

from matrix_studio.api.app import create_app
from tests.test_api import make_fake_run, REQUEST
from unittest.mock import patch

# The client fixture records its DB path here so tests can open a second
# connection to the same file (e.g. to seed an imported summary as the importer
# would) without going through the app's event loop.
client_db_path: list = []


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    client_db_path.append(db_path)

    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        base = "trusted-robot"
        name = base
        n = 2
        while name_exists is not None and await name_exists(name):
            name = f"{base}-{n}"
            n += 1
        return {"name": name, "description": "A test simulation", "slug": name, "source": "llm"}

    monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.api.app.generate_run_name", fake_name)

    app = create_app(db_path=db_path)
    with TestClient(app) as c:
        yield c


def _wait_complete(client, ref, tries=100):
    for _ in range(tries):
        r = client.get(f"/api/runs/{ref}")
        if r.status_code == 200 and r.json()["status"] in ("complete", "failed"):
            return r.json()
        time.sleep(0.02)
    return client.get(f"/api/runs/{ref}").json()


def _wait_for_summary(client, ref, tries=100):
    """Auto-summary runs after completion in the background task."""
    for _ in range(tries):
        r = client.get(f"/api/runs/{ref}/summary")
        if r.status_code == 200 and r.json().get("generated"):
            return r.json()
        time.sleep(0.02)
    return client.get(f"/api/runs/{ref}/summary").json()


def _start(client, extra=None):
    body = dict(REQUEST)
    if extra:
        body.update(extra)
    return client.post("/api/runs", json=body).json()


def test_autosummary_by_default(client):
    """No summary config → a structured summary is auto-generated at completion."""
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=2)):
        run_id = _start(client)["run_id"]
        _wait_complete(client, run_id)
        summ = _wait_for_summary(client, run_id)

    gen = summ["generated"]
    assert gen is not None
    payload = gen["payload"]
    # Full default field set present.
    for field in ("consensus", "dissenters", "key_ideas", "open_questions", "overview"):
        assert field in payload
    # Analysis cost is tracked (from the mocked call) and is non-canonical.
    assert gen["cost_usd"] > 0


def test_autosummary_disabled(client):
    """summary.enabled=false skips auto-generation; on-demand still works."""
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=2)):
        run_id = _start(client, {"summary": {"enabled": False}})["run_id"]
        _wait_complete(client, run_id)
        # Give the background task a beat; it should NOT create a summary.
        time.sleep(0.3)
        summ = client.get(f"/api/runs/{run_id}/summary").json()
        assert summ["generated"] is None

        # On-demand generation still works for the completed run.
        gen = client.post(f"/api/runs/{run_id}/summary").json()
        assert gen["generated"] is not None


def test_summary_focus_is_passed(client):
    """A focus steer reaches the analysis call (captured via the seam)."""
    captured = {}

    async def _capture(messages, model=None, temperature=0.4, max_tokens=None):
        captured["system"] = messages[0]["content"]
        import json as _json
        return {"content": _json.dumps({"overview": "o", "consensus": [], "dissenters": [],
                                        "key_ideas": [], "open_questions": []}),
                "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0}

    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=1)):
        run_id = _start(client, {"summary": {"enabled": False}})["run_id"]
        _wait_complete(client, run_id)

    with patch("matrix_studio.analysis._acompletion", _capture):
        client.post(f"/api/runs/{run_id}/summary",
                    json={"focus": "emphasize legal and ethical risk"})
    assert "emphasize legal and ethical risk" in captured["system"]


def test_summary_on_incomplete_run_rejected(client):
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=3, delay=0.5)):
        run_id = _start(client)["run_id"]
        # Immediately (still running) — should 409.
        r = client.post(f"/api/runs/{run_id}/summary")
        assert r.status_code == 409
        _wait_complete(client, run_id)


def test_analyst_aside_thread_multiturn(client):
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=2)):
        run_id = _start(client)["run_id"]
        _wait_complete(client, run_id)

    t = client.post(f"/api/runs/{run_id}/threads", json={"target": "analyst"})
    assert t.status_code == 201
    tid = t.json()["id"]

    r1 = client.post(f"/api/threads/{tid}/messages", json={"content": "Strongest argument against?"})
    assert r1.status_code == 201
    assert r1.json()["reply"]["speaker"] == "analyst"

    r2 = client.post(f"/api/threads/{tid}/messages", json={"content": "And a follow-up?"})
    assert r2.status_code == 201

    detail = client.get(f"/api/threads/{tid}").json()
    # 2 user + 2 target = 4 messages, multi-turn history retained.
    assert len(detail["messages"]) == 4
    assert detail["total_cost_usd"] > 0


def test_persona_aside_uses_cast_persona(client):
    """A persona target must be a real cast member; reply speaks as them."""
    captured = {}

    async def _capture(messages, model=None, temperature=0.4, max_tokens=None):
        captured["system"] = messages[0]["content"]
        return {"content": "As Ada, I'd expand...", "tokens_in": 1, "tokens_out": 1,
                "cost_usd": 0.001}

    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=2)):
        run_id = _start(client)["run_id"]
        _wait_complete(client, run_id)

    # Unknown persona rejected.
    bad = client.post(f"/api/runs/{run_id}/threads",
                      json={"target": "persona", "persona_name": "Nobody"})
    assert bad.status_code == 422

    t = client.post(f"/api/runs/{run_id}/threads",
                    json={"target": "persona", "persona_name": "Ada"})
    assert t.status_code == 201
    tid = t.json()["id"]

    with patch("matrix_studio.analysis._acompletion", _capture):
        r = client.post(f"/api/threads/{tid}/messages", json={"content": "Expand your point."})
    assert r.json()["reply"]["speaker"] == "Ada"
    # The real stored persona text ("An ethicist") is used in the prompt.
    assert "An ethicist" in captured["system"]


def test_room_aside_returns_per_persona(client):
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=2)):
        run_id = _start(client)["run_id"]
        _wait_complete(client, run_id)

    t = client.post(f"/api/runs/{run_id}/threads", json={"target": "room"})
    tid = t.json()["id"]
    r = client.post(f"/api/threads/{tid}/messages", json={"content": "React, everyone."})
    reply = r.json()["reply"]
    assert reply["speaker"] == "room"
    # Both cast members (Ada, Ben) react into the thread.
    speakers = {rep["speaker"] for rep in reply["replies"]}
    assert speakers == {"Ada", "Ben"}


def test_threads_isolated_over_api(client):
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=2)):
        run_id = _start(client)["run_id"]
        _wait_complete(client, run_id)

    t1 = client.post(f"/api/runs/{run_id}/threads", json={"target": "analyst"}).json()["id"]
    t2 = client.post(f"/api/runs/{run_id}/threads", json={"target": "analyst"}).json()["id"]
    client.post(f"/api/threads/{t1}/messages", json={"content": "only in t1"})

    d1 = client.get(f"/api/threads/{t1}").json()
    d2 = client.get(f"/api/threads/{t2}").json()
    assert len(d1["messages"]) == 2  # user + target
    assert len(d2["messages"]) == 0  # untouched


def test_readonly_invariant_end_to_end(client):
    """Aside + summary activity must not change events/snapshot/run cost."""
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=2)):
        run_id = _start(client, {"summary": {"enabled": False}})["run_id"]
        _wait_complete(client, run_id)

    before_events = client.get(f"/api/runs/{run_id}/events").json()["events"]
    before_detail = client.get(f"/api/runs/{run_id}").json()
    before_cost = before_detail["total_cost_usd"]

    # Full analysis spread.
    client.post(f"/api/runs/{run_id}/summary")
    tid = client.post(f"/api/runs/{run_id}/threads", json={"target": "analyst"}).json()["id"]
    client.post(f"/api/threads/{tid}/messages", json={"content": "expensive question"})
    tid2 = client.post(f"/api/runs/{run_id}/threads", json={"target": "room"}).json()["id"]
    client.post(f"/api/threads/{tid2}/messages", json={"content": "everyone react"})

    after_events = client.get(f"/api/runs/{run_id}/events").json()["events"]
    after_detail = client.get(f"/api/runs/{run_id}").json()

    # Canonical event stream unchanged.
    assert after_events == before_events
    # Run's recorded cost unchanged (analysis cost counted separately).
    assert after_detail["total_cost_usd"] == pytest.approx(before_cost)
    # Snapshot conversation unchanged.
    assert after_detail["result"]["conversation"] == before_detail["result"]["conversation"]


def test_imported_summary_shown_separately(client):
    """An imported source summary is surfaced separately and not overwritten."""
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=2)):
        run_id = _start(client, {"summary": {"enabled": False}})["run_id"]
        _wait_complete(client, run_id)

    # Seed an imported summary directly (as the importer would) via a separate
    # connection to the same DB file, on a fresh event loop.
    import asyncio

    from matrix_studio.storage import Database

    async def _seed():
        seed_db = Database(client_db_path[-1])
        await seed_db.connect()
        await seed_db.save_summary(run_id, payload={"overview": "legacy original"},
                                   kind="imported")
        await seed_db.close()

    asyncio.new_event_loop().run_until_complete(_seed())

    # Now generate a summary alongside the imported one.
    client.post(f"/api/runs/{run_id}/summary")
    summ = client.get(f"/api/runs/{run_id}/summary").json()
    assert summ["imported"] is not None
    assert summ["imported"]["payload"]["overview"] == "legacy original"
    assert summ["generated"] is not None
