# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the Phase 1.5 analysis module (summary generation + aside
replies). The LLM seam ``analysis._acompletion`` is patched per-test so no live
call is made — we verify JSON parsing, the retry-then-plaintext fallback, and
that persona asides use the REAL stored persona text (never invented).
"""

import json

import pytest

from matrix_studio import analysis


CONVERSATION = [
    {"speaker": "Ada", "content": "We should require a vet sign-off.", "turn": 1},
    {"speaker": "Ben", "content": "That adds liability we can't absorb.", "turn": 2},
]


def _mk(content, cost=0.001):
    async def _fake(messages, model=None, temperature=0.4, max_tokens=None):
        return {"content": content, "tokens_in": 50, "tokens_out": 10, "cost_usd": cost}

    return _fake


@pytest.mark.asyncio
async def test_summary_parses_strict_json(monkeypatch):
    payload = {
        "consensus": ["vet sign-off gate"],
        "dissenters": [{"speaker": "Ben", "position": "liability"}],
        "key_ideas": ["tiered windows"],
        "open_questions": ["who audits?"],
        "overview": "A debate about a reauthorization policy.",
    }
    monkeypatch.setattr(analysis, "_acompletion", _mk(json.dumps(payload)))
    result = await analysis.generate_summary(CONVERSATION, topic="pet food")
    assert result["parsed"] is True
    assert result["payload"]["consensus"] == ["vet sign-off gate"]
    assert result["payload"]["dissenters"][0]["speaker"] == "Ben"
    assert result["cost_usd"] == 0.001


@pytest.mark.asyncio
async def test_summary_parses_fenced_json(monkeypatch):
    payload = {"overview": "fenced", "consensus": [], "dissenters": [],
               "key_ideas": [], "open_questions": []}
    fenced = f"Here you go:\n```json\n{json.dumps(payload)}\n```"
    monkeypatch.setattr(analysis, "_acompletion", _mk(fenced))
    result = await analysis.generate_summary(CONVERSATION, topic="t")
    assert result["parsed"] is True
    assert result["payload"]["overview"] == "fenced"


@pytest.mark.asyncio
async def test_summary_retries_then_plaintext_fallback(monkeypatch):
    """Two non-JSON replies → graceful plain-text overview, never a crash."""
    calls = {"n": 0}

    async def _fake(messages, model=None, temperature=0.4, max_tokens=None):
        calls["n"] += 1
        return {"content": "totally not json", "tokens_in": 5, "tokens_out": 5,
                "cost_usd": 0.0005}

    monkeypatch.setattr(analysis, "_acompletion", _fake)
    result = await analysis.generate_summary(CONVERSATION, topic="t")
    assert result["parsed"] is False
    assert calls["n"] == 2  # one attempt + one retry
    assert result["payload"]["overview"] == "totally not json"
    # Cost is accumulated across both attempts (honest accounting).
    assert result["cost_usd"] == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_summary_llm_exception_never_crashes(monkeypatch):
    async def _boom(messages, model=None, temperature=0.4, max_tokens=None):
        raise RuntimeError("provider down")

    monkeypatch.setattr(analysis, "_acompletion", _boom)
    result = await analysis.generate_summary(CONVERSATION, topic="t")
    assert result["parsed"] is False
    assert "unavailable" in result["payload"]["overview"].lower()


@pytest.mark.asyncio
async def test_summary_honors_field_subset(monkeypatch):
    payload = {"overview": "o", "consensus": ["c"], "dissenters": [],
               "key_ideas": ["k"], "open_questions": ["q"]}
    monkeypatch.setattr(analysis, "_acompletion", _mk(json.dumps(payload)))
    result = await analysis.generate_summary(
        CONVERSATION, topic="t", fields=["overview", "consensus"]
    )
    assert set(result["payload"].keys()) == {"overview", "consensus"}


@pytest.mark.asyncio
async def test_persona_reply_uses_real_persona_text(monkeypatch):
    captured = {}

    async def _fake(messages, model=None, temperature=0.4, max_tokens=None):
        captured["system"] = messages[0]["content"]
        return {"content": "In character reply.", "tokens_in": 1, "tokens_out": 1,
                "cost_usd": 0.0}

    monkeypatch.setattr(analysis, "_acompletion", _fake)
    persona_text = "Dr. Webb is a liability-obsessed corporate lawyer, ex-litigator."
    reply = await analysis.persona_reply(
        user_message="Expand on your liability point.",
        persona_name="Dr. Webb",
        persona_text=persona_text,
        conversation=CONVERSATION,
        topic="pet food",
    )
    assert reply["speaker"] == "Dr. Webb"
    # The REAL stored persona text must be embedded in the system prompt.
    assert persona_text in captured["system"]
    # And the aside must be framed as post-hoc reflection (not a live turn).
    assert "already FINISHED" in captured["system"]


@pytest.mark.asyncio
async def test_room_reply_calls_each_persona(monkeypatch):
    seen = []

    async def _fake(messages, model=None, temperature=0.4, max_tokens=None):
        seen.append(messages[0]["content"])
        return {"content": "reply", "tokens_in": 2, "tokens_out": 2, "cost_usd": 0.001}

    monkeypatch.setattr(analysis, "_acompletion", _fake)
    cast = [
        {"name": "Ada", "persona": "ethicist persona text"},
        {"name": "Ben", "persona": "engineer persona text"},
    ]
    reply = await analysis.room_reply(
        user_message="React to the proposal.",
        cast=cast,
        conversation=CONVERSATION,
        topic="t",
    )
    assert reply["speaker"] == "room"
    assert len(reply["replies"]) == 2
    assert {r["speaker"] for r in reply["replies"]} == {"Ada", "Ben"}
    # Aggregated cost across both persona calls.
    assert reply["cost_usd"] == pytest.approx(0.002)
    assert any("ethicist persona text" in s for s in seen)
    assert any("engineer persona text" in s for s in seen)
