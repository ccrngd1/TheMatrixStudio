# SPDX-License-Identifier: Apache-2.0
"""
The speaker-selection eval harness survives a transient provider error.

Observed 2026-09-15: intermittent 404s from bedrock-runtime killed whole invocations of
scripts/eval_speaker_selection.py and discarded every transcript already scored — 6 of 24 paid passes.
What is pinned: a failed selection call is retried and then answers; a persistent failure is still
raised, not swallowed; and one failed replay does not stop the others from being scored.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import eval_speaker_selection as ev  # noqa: E402


def _reply(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(ev.asyncio, "sleep", instant)
    monkeypatch.setattr(ev, "RETRIES_USED", 0)


def _litellm(monkeypatch, outcomes):
    from matrix_studio import lazy_litellm

    calls = []

    async def acompletion(**kw):
        calls.append(kw)
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(lazy_litellm.litellm, "acompletion", acompletion, raising=False)
    return calls


async def test_a_transient_error_is_retried_and_the_answer_is_kept(monkeypatch):
    calls = _litellm(monkeypatch, [RuntimeError("404 Not Found"), _reply('{"speaker": "Dana"}')])
    name, _ = await ev.select("prompt", ["Dana", "Marcus"], "m")
    assert name == "Dana" and len(calls) == 2 and ev.RETRIES_USED == 1


async def test_a_persistent_error_is_still_raised(monkeypatch):
    calls = _litellm(monkeypatch, [RuntimeError("down")])
    with pytest.raises(RuntimeError):
        await ev.select("prompt", ["Dana"], "m")
    assert len(calls) == ev.SELECT_RETRIES + 1


async def test_one_failed_replay_does_not_stop_the_others(monkeypatch, capsys):
    async def replay(db, run_id, arm, *a, **k):
        if run_id == "bad-run":
            raise RuntimeError("still down")
        return {"gini": 0.1, "min_turns": 1, "max_turns": 2, "dyad_chain": 0, "coverage_turn": 3,
                "self_repeats": 0, "picks_from_last_two": 0, "unresolved": 0, "floor_fired": 0,
                "declines": 0, "declined_at": None, "never_spoke": []}

    class DB:
        def __init__(self, **k): pass
        async def connect(self): pass
        async def close(self): pass
        def for_owner(self, o): return self

    monkeypatch.setattr(ev, "replay", replay)
    monkeypatch.setattr(ev, "Database", DB)
    monkeypatch.setenv("DATA_BUCKET", "b")
    monkeypatch.setattr(sys, "argv", ["x", "--owner", "o", "--run", "bad-run", "--run", "good-run"])
    rc = await ev.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "bad-run" in out and "FAILED" in out
    assert "good-run" in out and "0.10" in out
