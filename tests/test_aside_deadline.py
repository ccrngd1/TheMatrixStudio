# SPDX-License-Identifier: Apache-2.0
"""
Asides must finish inside the deployed API's 30 s request limit, or fail cleanly.

Reported 2026-09-28: a persona aside returned HTTP 504 in the deployed UI. The Lambda log showed the
reply being generated inside the request and running into the 30 s limit; no persona aside had ever
returned on the deployed stack, and the unanswered question stayed in the thread. Three causes, each
pinned here: replies had no output budget of their own (they borrowed the 8,000-token summary one), a
room reply asked each persona in turn, and the question was stored before the reply existed.
"""

import asyncio
import time
from unittest.mock import patch

import pytest

from matrix_studio import analysis, service
from tests.test_api import make_fake_run
from tests.test_api_phase15 import _start, _storage_backend, _wait_complete, client  # noqa: F401


def _capture(calls, delay=0.0):
    async def fake(messages, model=None, temperature=0.4, max_tokens=None):
        calls.append(max_tokens)
        await asyncio.sleep(delay)
        return {"content": "A short reply.", "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.001}
    return fake


@pytest.mark.asyncio
async def test_persona_and_analyst_asides_use_their_own_small_budget(monkeypatch):
    calls = []
    monkeypatch.setattr(analysis, "_acompletion", _capture(calls))
    conv = [{"speaker": "A", "content": "x", "turn": 1}]
    await analysis.persona_reply("q", "A", "p", conv, "t")
    await analysis.analyst_reply("q", conv, "t")
    assert calls == [analysis.ASIDE_MAX_TOKENS, analysis.ASIDE_MAX_TOKENS]
    assert analysis.ASIDE_MAX_TOKENS <= 500  # ~15 s of output at the measured deployed rate


@pytest.mark.asyncio
async def test_the_room_asks_every_persona_at_once(monkeypatch):
    calls = []
    monkeypatch.setattr(analysis, "_acompletion", _capture(calls, delay=0.2))
    cast = [{"name": f"P{i}", "persona": "p"} for i in range(6)]
    t = time.monotonic()
    out = await analysis.room_reply("q", cast, [{"speaker": "P0", "content": "x", "turn": 1}], "t")
    elapsed = time.monotonic() - t
    assert len(out["replies"]) == min(6, analysis.MAX_ROOM_PERSONAS)
    assert [r["speaker"] for r in out["replies"]] == [c["name"] for c in cast][: len(out["replies"])]
    assert elapsed < 0.6, f"replies ran one after another ({elapsed:.2f} s for 0.2 s each)"


def test_a_reply_past_the_deadline_is_a_clear_504_and_stores_nothing(client, monkeypatch):
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=2)):
        run_id = _start(client)["run_id"]
        _wait_complete(client, run_id)
    tid = client.post(f"/api/runs/{run_id}/threads", json={"target": "analyst"}).json()["id"]

    async def slow(*_a, **_k):
        await asyncio.sleep(1)

    monkeypatch.setattr(service, "ASIDE_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(service, "_aside_reply", slow)
    r = client.post(f"/api/threads/{tid}/messages", json={"content": "Anything?"})
    assert r.status_code == 504
    assert "nothing was saved" in r.json()["detail"]
    # No orphaned question: the thread is exactly as it was.
    assert client.get(f"/api/threads/{tid}").json()["messages"] == []


def test_a_normal_reply_still_stores_question_and_answer(client):
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=2)):
        run_id = _start(client)["run_id"]
        _wait_complete(client, run_id)
    tid = client.post(f"/api/runs/{run_id}/threads", json={"target": "analyst"}).json()["id"]
    r = client.post(f"/api/threads/{tid}/messages", json={"content": "Anything?"})
    assert r.status_code == 201, r.text
    roles = [m["role"] for m in client.get(f"/api/threads/{tid}").json()["messages"]]
    assert roles == ["user", "target"]


# --------------------------------------------------------------------------- #
# Background replies: the aside worker
# --------------------------------------------------------------------------- #


def _thread(client):
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=2)):
        run_id = _start(client)["run_id"]
        _wait_complete(client, run_id)
    return client.post(f"/api/runs/{run_id}/threads", json={"target": "analyst"}).json()["id"]


def test_deployed_the_question_is_stored_dispatched_and_answered_202(client, monkeypatch):
    tid = _thread(client)
    sent = []

    async def fake_dispatch(thread_id, owner, message, model):
        sent.append((thread_id, owner, message))

    monkeypatch.setenv("ASIDE_FUNCTION", "matrix-studio-aside")
    monkeypatch.setattr(service, "dispatch_aside", fake_dispatch)
    r = client.post(f"/api/threads/{tid}/messages", json={"content": "Why?"})
    assert r.status_code == 202 and r.json()["pending"] is True
    assert sent and sent[0][0] == tid and sent[0][2] == "Why?"
    msgs = client.get(f"/api/threads/{tid}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user"]

    # A second question while the first is pending would race two workers.
    assert client.post(f"/api/threads/{tid}/messages", json={"content": "And?"}).status_code == 409


def test_an_abandoned_question_does_not_lock_the_thread():
    now = 10_000.0
    msgs = [{"role": "user", "created_at": now - service.ASIDE_PENDING_SECONDS - 1}]
    assert service.pending_question(msgs, now=now) is None
    assert service.pending_question([{"role": "user", "created_at": now - 5}], now=now) is not None
    assert service.pending_question([{"role": "user"}, {"role": "target"}], now=now) is None


@pytest.mark.asyncio
async def test_the_worker_stores_the_reply_with_the_larger_budget(db, monkeypatch):
    calls = []
    monkeypatch.setattr(analysis, "_acompletion", _capture(calls))
    await db.create_run(run_id="bg1", topic="t", cast=[{"name": "A", "persona": "p"}], name="bg1")
    tid = await _new_thread(db, "bg1")
    await db.add_thread_message(thread_id=tid, role="user", speaker="user", content="Why?")
    out = await service.answer_aside_in_background(db, tid, "Why?")
    assert out["stored"] is True
    assert calls == [analysis.ASIDE_BACKGROUND_MAX_TOKENS]
    roles = [m["role"] for m in await db.get_thread_messages(tid)]
    assert roles == ["user", "target"]


@pytest.mark.asyncio
async def test_a_worker_failure_is_recorded_in_the_thread(db, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("bedrock down")

    monkeypatch.setattr(analysis, "_acompletion", boom)
    await db.create_run(run_id="bg2", topic="t", cast=[{"name": "A", "persona": "p"}], name="bg2")
    tid = await _new_thread(db, "bg2")
    await db.add_thread_message(thread_id=tid, role="user", speaker="user", content="Why?")
    out = await service.answer_aside_in_background(db, tid, "Why?")
    assert out["stored"] is False
    last = (await db.get_thread_messages(tid))[-1]
    assert last["role"] == "error" and "ask again" in last["content"]


async def _new_thread(db, run_id):
    import uuid
    tid = str(uuid.uuid4())
    await db.create_thread(thread_id=tid, run_id=run_id, target="analyst")
    return tid


# --------------------------------------------------------------------------- #
# A requested summary: the same worker, for the same reason (30.4 s / 56.9 s on a 40-turn run)
# --------------------------------------------------------------------------- #


def test_deployed_a_requested_summary_is_dispatched_and_answered_202(client, monkeypatch):
    with patch("matrix_studio.api.manager.run_simulation", make_fake_run(turns=2)):
        run_id = _start(client)["run_id"]
        _wait_complete(client, run_id)
    sent = []

    async def fake_dispatch(rid, owner, **kw):
        sent.append((rid, kw))

    monkeypatch.setenv("ASIDE_FUNCTION", "matrix-studio-aside")
    monkeypatch.setattr(service, "dispatch_summary", fake_dispatch)
    before = client.get(f"/api/runs/{run_id}/summary").json()["generated"]
    r = client.post(f"/api/runs/{run_id}/summary", json={"focus": "cost"})
    assert r.status_code == 202 and r.json()["pending"] is True
    assert sent and sent[0][0] == run_id and sent[0][1]["focus"] == "cost"
    # Nothing was generated in the request: the current summary is returned unchanged.
    assert r.json()["generated"] == before


@pytest.mark.asyncio
async def test_the_worker_stores_a_requested_summary(db, monkeypatch):
    async def fake(messages, model=None, temperature=0.4, max_tokens=None):
        return {"content": '{"overview": "done", "evidence_plan": [], "conditional_recommendation": ""}',
                "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.001}

    monkeypatch.setattr(analysis, "_acompletion", fake)
    await db.create_run(run_id="bs1", topic="t", cast=[{"name": "A", "persona": "p"}], name="bs1")
    out = await service.summarise_in_background(db, "bs1")
    assert out["stored"] is True
    [row] = [r for r in await db.get_summaries("bs1") if r["kind"] == "generated"]
    assert row["payload"]["overview"] == "done"


@pytest.mark.asyncio
async def test_the_aside_worker_routes_a_summary_event(monkeypatch):
    from matrix_studio import step_handlers

    seen = []

    async def fake_bound(owner):
        return "db"

    async def fake_summarise(db, run_id, **kw):
        seen.append((run_id, kw["focus"]))
        return {"run_id": run_id, "stored": True}

    monkeypatch.setattr(step_handlers, "_bound", fake_bound)
    monkeypatch.setattr(service, "summarise_in_background", fake_summarise)
    out = await step_handlers._aside({"kind": "summary", "run_id": "r9", "owner_sub": "o", "focus": "f"})
    assert out["stored"] is True and seen == [("r9", "f")]
