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
