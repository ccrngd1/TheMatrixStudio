# SPDX-License-Identifier: Apache-2.0
"""
Phase 4b tests — latent/pending-thread state (the setups & payoffs ledger).

Covers:
  (1) threads OFF (default, incl. cognition-on runs): no thread events, no
      thread_refs, no thread schema in the generation prompt, snapshots carry
      an empty ledger (pre-4b shape preserved for parsing old snapshots).
  (2) open/resolve lifecycle: a planted thread emits thread.opened, rides the
      snapshot, is fed into the NEXT turn's prompt (causally real), and its id
      is the turn's thread_refs; a resolving turn emits thread.resolved and the
      thread stops being retrieved afterwards (proven by prompt inspection).
  (3) abandon lifecycle + honesty guards: resolving an id that was not
      in-context is ignored (no fabricated payoff); unknown thread_type
      degrades to 'setup'; open specs are capped at 2.
  (4) snapshot/branch/scrub survival: the ledger is in every per-turn snapshot;
      reconstruct_at_turn replays thread events losslessly (same ids/status);
      a branch continues the parent's ledger forward.
  (5) staleness: /pending-threads flags open threads older than
      thread_stale_after (default 5) as stale; dossier lists the agent's
      planted threads.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio.engine import run_simulation
from matrix_studio.state import PendingThread, SimSnapshot
from matrix_studio.storage import Database


@pytest.fixture(autouse=True)
def _storage_backend(aws_backend):
    """Every test in this file builds the FastAPI app.

    The app's lifespan connects to DynamoDB, so without a mocked account it reaches
    real AWS — which surfaces as `ExpiredTokenException` on a `Scan` and reads like a
    credentials problem rather than a missing fixture. Autouse and explicit here
    rather than hidden in `conftest.py`, so the dependency is visible in the file that
    has it.
    """


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

THREADS_COG = {"enabled": True, "threads": True, "memory": False,
               "reflection_every": 0}


async def _events(db, run_id, event_type):
    rows = await db.get_events(run_id)
    out = []
    for r in rows:
        if r["event_type"] != event_type:
            continue
        p = r["payload"]
        if isinstance(p, str):
            p = json.loads(p) if p else {}
        out.append({"turn": r["turn"], "agent": r["agent_name"], "payload": p})
    return out


def _resp_obj(utterance, thread_updates=None):
    obj = {"utterance": utterance, "rationale": "my reason", "goal_served": "none"}
    if thread_updates is not None:
        obj["thread_updates"] = thread_updates
    return obj


# --------------------------------------------------------------------------- #
# (1) threads OFF: no schema, no events, no refs
# --------------------------------------------------------------------------- #

async def test_threads_off_no_schema_events_or_refs(db):
    prompts = []

    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        prompts.append(text)
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Ada", "reason": "Ada leads"}))
        # model volunteers thread_updates anyway -> must be ignored
        return _Resp(json.dumps(_resp_obj(
            "A reply.", {"open": [{"description": "sneaky", "thread_type": "setup"}]})))

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 2, "generate_avatars": False,
                         "cognition": {"enabled": True, "memory": False,
                                       "reflection_every": 0}}
        await run_simulation(req, db=db, run_id="thr-off")

    assert await _events(db, "thr-off", "thread.opened") == []
    assert all("thread_updates" not in p for p in prompts if "conversation moderator" not in p)
    for ev in await _events(db, "thr-off", "agent.response"):
        assert "thread_refs" not in ev["payload"]
    snap = await db.get_snapshot("thr-off", 2)
    assert snap.pending_threads == []


# --------------------------------------------------------------------------- #
# (2) open -> feed-forward -> resolve lifecycle
# --------------------------------------------------------------------------- #

async def test_thread_lifecycle_open_feedforward_resolve(db):
    """Turn 1 plants a thread; turn 2's prompt must list it (causally real) and
    resolve it; turn 3's prompt must NOT list it anymore."""
    gen_prompts = []
    state = {"opened_id": None}

    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Ada", "reason": "Ada leads"}))
        gen_prompts.append(text)
        n = len(gen_prompts)
        if n == 1:
            return _Resp(json.dumps(_resp_obj(
                "Let me plant a seed of doubt about the audit.",
                {"open": [{"description": "the audit result is hidden",
                           "thread_type": "setup"}],
                 "resolved": [], "abandoned": []})))
        if n == 2:
            # Resolve the thread by citing the exact id shown in OUR prompt.
            import re
            m = re.search(r"\[([0-9a-f]{12})\]", text)
            tid = m.group(1) if m else "nope"
            state["opened_id"] = tid
            return _Resp(json.dumps(_resp_obj(
                "The audit result is out: we passed.",
                {"open": [], "resolved": [tid], "abandoned": []})))
        return _Resp(json.dumps(_resp_obj("Moving on to next topic.")))

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 3, "generate_avatars": False,
                         "cognition": THREADS_COG}
        await run_simulation(req, db=db, run_id="thr-life")

    opened = await _events(db, "thr-life", "thread.opened")
    assert len(opened) == 1
    tid = opened[0]["payload"]["id"]
    assert opened[0]["payload"]["description"] == "the audit result is hidden"
    assert opened[0]["payload"]["origin_turn"] == 1

    # Causally real: turn 2's generation prompt lists the open thread.
    assert "the audit result is hidden" in gen_prompts[1]
    assert tid in gen_prompts[1]
    # And the model resolved the exact id our engine showed it.
    assert state["opened_id"] == tid

    resolved = await _events(db, "thr-life", "thread.resolved")
    assert len(resolved) == 1
    assert resolved[0]["payload"]["id"] == tid
    assert resolved[0]["payload"]["resolved_turn"] == 2

    # A resolved thread STOPS being retrieved: turn 3's prompt has no thread.
    assert "the audit result is hidden" not in gen_prompts[2]
    assert "Unresolved threads" not in gen_prompts[2]

    # thread_refs are the causal in-context ids per turn.
    resp = await _events(db, "thr-life", "agent.response")
    assert resp[0]["payload"]["thread_refs"] == []
    assert resp[1]["payload"]["thread_refs"] == [tid]
    assert resp[2]["payload"]["thread_refs"] == []

    # Ledger state rides the snapshot.
    snap = await db.get_snapshot("thr-life", 3)
    assert len(snap.pending_threads) == 1
    t = snap.pending_threads[0]
    assert t.id == tid and t.status == "resolved" and t.resolved_turn == 2


# --------------------------------------------------------------------------- #
# (3) abandon + honesty guards
# --------------------------------------------------------------------------- #

async def test_thread_abandon_and_fabricated_resolution_ignored(db):
    gen_prompts = []

    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Ada", "reason": "Ada leads"}))
        gen_prompts.append(text)
        n = len(gen_prompts)
        if n == 1:
            # Try to resolve a NEVER-OPENED id (fabricated payoff) + open one
            # thread with a bogus type + 3 opens (must cap at 2).
            return _Resp(json.dumps(_resp_obj(
                "Planting things.",
                {"open": [
                    {"description": "thread one", "thread_type": "weird-type"},
                    {"description": "thread two", "thread_type": "promise"},
                    {"description": "thread three", "thread_type": "setup"},
                 ],
                 "resolved": ["deadbeef0000"], "abandoned": []})))
        if n == 2:
            import re
            ids = re.findall(r"\[([0-9a-f]{12})\]", text)
            return _Resp(json.dumps(_resp_obj(
                "That first idea no longer matters.",
                {"open": [], "resolved": [], "abandoned": [ids[0]]})))
        return _Resp(json.dumps(_resp_obj("Continuing.")))

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 2, "generate_avatars": False,
                         "cognition": THREADS_COG}
        await run_simulation(req, db=db, run_id="thr-guards")

    opened = await _events(db, "thr-guards", "thread.opened")
    assert len(opened) == 2  # capped at 2 per turn
    assert opened[0]["payload"]["thread_type"] == "setup"  # bogus type degraded
    assert opened[1]["payload"]["thread_type"] == "promise"

    # Fabricated resolution of an unseen id emitted NOTHING.
    assert await _events(db, "thr-guards", "thread.resolved") == []

    abandoned = await _events(db, "thr-guards", "thread.abandoned")
    assert len(abandoned) == 1
    assert abandoned[0]["payload"]["id"] == opened[0]["payload"]["id"]

    snap = await db.get_snapshot("thr-guards", 2)
    statuses = {t.id: t.status for t in snap.pending_threads}
    assert statuses[opened[0]["payload"]["id"]] == "abandoned"
    assert statuses[opened[1]["payload"]["id"]] == "open"


# --------------------------------------------------------------------------- #
# (4) snapshot / branch / scrub survival
# --------------------------------------------------------------------------- #

async def test_threads_survive_branch_reconstruction(db):
    """The ledger replayed by reconstruct_at_turn from thread events must match
    the snapshot ledger exactly (ids, status, resolved_turn) — and honour the
    fork point (a resolution AFTER the fork is not visible at the fork)."""
    from matrix_studio.branching import reconstruct_at_turn

    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Ada", "reason": "Ada leads"}))
        if "Unresolved threads" not in text:
            return _Resp(json.dumps(_resp_obj(
                "Planting the mystery of the missing logs.",
                {"open": [{"description": "the missing logs",
                           "thread_type": "setup"}]})))
        import re
        m = re.search(r"\[([0-9a-f]{12})\]", text)
        return _Resp(json.dumps(_resp_obj(
            "Found the logs.", {"open": [], "resolved": [m.group(1)],
                                "abandoned": []})))

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 2, "generate_avatars": False,
                         "cognition": THREADS_COG}
        await run_simulation(req, db=db, run_id="thr-branch")

    run = await db.get_run("thr-branch")

    # Fork at turn 1: thread open, not yet resolved.
    _, _, _, threads_at_1, _ = await reconstruct_at_turn(db, run, 1)
    assert len(threads_at_1) == 1
    assert threads_at_1[0].status == "open"
    assert threads_at_1[0].description == "the missing logs"

    # Fork at turn 2: same thread, now resolved — matches the stored snapshot.
    _, _, _, threads_at_2, _ = await reconstruct_at_turn(db, run, 2)
    snap = await db.get_snapshot("thr-branch", 2)
    assert [t.model_dump() for t in threads_at_2] == \
        [t.model_dump() for t in snap.pending_threads]
    assert threads_at_2[0].status == "resolved"


async def test_branch_continues_parent_ledger_forward(db):
    """execute_branch seeds the fork snapshot with the replayed ledger and the
    branch's first generated turn sees the still-open thread in its prompt."""
    from matrix_studio import branching

    def fake_parent(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Ada", "reason": "Ada leads"}))
        if "Unresolved threads" not in text:
            return _Resp(json.dumps(_resp_obj(
                "Planting a promise to revisit safety.",
                {"open": [{"description": "revisit safety later",
                           "thread_type": "promise"}]})))
        return _Resp(json.dumps(_resp_obj("Just talking.")))

    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=fake_parent):
        req = dict(REQUEST)
        req["config"] = {"max_messages": 2, "generate_avatars": False,
                         "cognition": THREADS_COG}
        await run_simulation(req, db=db, run_id="thr-parent")

    parent = await db.get_run("thr-parent")

    async def fake_name_exists(name):
        return False

    branch_prompts = []

    def fake_branch(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Ben", "reason": "Ben responds"}))
        branch_prompts.append(text)
        return _Resp(json.dumps(_resp_obj("Branch reply.")))

    meta = await branching.create_branch_run(
        db, parent, from_turn=1, name="thr-branch-child")
    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=fake_branch):
        await branching.execute_branch(
            db, parent, branch_run_id=meta["run_id"], from_turn=1,
            max_messages=meta["max_messages"],
        )

    # Fork snapshot carries the parent ledger.
    fork_snap = await db.get_snapshot(meta["run_id"], 1)
    assert len(fork_snap.pending_threads) == 1
    assert fork_snap.pending_threads[0].description == "revisit safety later"
    assert fork_snap.pending_threads[0].status == "open"

    # Causally real on the branch: the open thread is in the branch's prompts.
    assert branch_prompts, "branch generated no turns"
    assert any("revisit safety later" in p for p in branch_prompts)


def test_old_snapshot_json_without_threads_still_parses():
    """Pre-4b stored snapshots have no pending_threads key; parsing must
    default to []."""
    old = {
        "run_id": "r", "turn": 1, "topic": "t",
        "agents": {"Ada": {"name": "Ada", "persona": "p"}},
        "conversation": [], "status": "complete",
        "created_at": 0, "total_turns": 1,
    }
    snap = SimSnapshot.model_validate(old)
    assert snap.pending_threads == []


# --------------------------------------------------------------------------- #
# (5) staleness surfacing (API)
# --------------------------------------------------------------------------- #

def test_pending_threads_api_staleness(tmp_path, monkeypatch):
    import time as _time
    from fastapi.testclient import TestClient
    from matrix_studio.api.app import create_app

    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        return {"name": "thr-api", "description": "t", "slug": "thr-api",
                "source": "llm"}

    monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.api.app.generate_run_name", fake_name)

    def fake(*args, **kwargs):
        text = " ".join(m["content"] for m in kwargs["messages"])
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Ada", "reason": "Ada leads"}))
        if "Unresolved threads" not in text:
            return _Resp(json.dumps(_resp_obj(
                "Planting a dangling setup.",
                {"open": [{"description": "a dangling setup",
                           "thread_type": "setup"}]})))
        return _Resp(json.dumps(_resp_obj("Talking without resolving.")))

    app = create_app(db_path=str(tmp_path / "thr-api.db"))
    with TestClient(app) as client:
        with patch("matrix_studio.engine.simulator.litellm.acompletion",
                   side_effect=fake):
            req = dict(REQUEST)
            # stale_after=2 and 4 turns: opened turn 1, still open at turn 4
            # -> age 3 >= 2 -> stale.
            req["config"] = {"max_messages": 4, "generate_avatars": False,
                             "cognition": {"enabled": True, "threads": True,
                                           "memory": False, "reflection_every": 0,
                                           "thread_stale_after": 2}}
            run_id = client.post("/api/runs", json=req).json()["run_id"]
            for _ in range(300):
                r = client.get(f"/api/runs/{run_id}")
                if r.status_code == 200 and r.json()["status"] in ("complete", "failed"):
                    break
                _time.sleep(0.02)

        r = client.get(f"/api/runs/{run_id}/pending-threads")
        assert r.status_code == 200
        body = r.json()
        assert body["stale_after"] == 2
        open_threads = [t for t in body["threads"] if t["status"] == "open"]
        assert open_threads, "expected at least one open thread"
        # The turn-1 thread is dangling by now.
        first = min(open_threads, key=lambda t: t["origin_turn"])
        assert first["stale"] is True

        # Dossier lists the agent's planted threads with the stale flag.
        d = client.get(f"/api/runs/{run_id}/agents/Ada/dossier").json()
        assert any(t["stale"] for t in d["pending_threads"])
