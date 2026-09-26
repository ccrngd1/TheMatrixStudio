# SPDX-License-Identifier: Apache-2.0
"""
A run's cost is every model call it made — summed from events, the same number shown and charged.

Measured 2026-09-26 on brainstorm-opus against Bedrock's own invocation log: reported $4.25,
actual ~$5.15. The run page summed `agent.response` events only; the monthly cap summed the
engine's per-persona snapshot, a DIFFERENT subset. Neither counted speaker selection (35 Haiku
calls), the summary (the largest single uncounted call), or avatars; the page also missed
validation checks, rejected attempts and reflections.

The fix: every model call records its cost on the event it produced, the run's cost is the sum of
its events, and the cap charges that same sum. These tests price each kind of call DIFFERENTLY, so a
missing kind shows up as a wrong total rather than hiding inside one.
"""

import json
from unittest.mock import patch

from matrix_studio import orchestration
from tests.support import TEST_OWNER
from tests.test_orchestration import CAST, _Resp

VOICE, SELECTION = 0.100, 0.007


#: Every cost the fake provider billed, in order. The invariant under test is that the run's total
#: equals THIS — every call counted — rather than an expected formula, because the formula was
#: itself wrong the first time: the identical fake replies trip the repetition check, so turns
#: regenerate, and each rejected attempt is a real call. Counting billed calls is what caught it.
BILLED: list = []


def _priced_llm(*args, **kwargs):
    text = " ".join(m["content"] for m in kwargs["messages"])
    if "conversation moderator" in text:
        BILLED.append(SELECTION)
        return _Resp(json.dumps({"speaker": "Ada", "reason": "her turn"}), cost=SELECTION)
    if "consistency validator" in text:
        return _Resp(json.dumps({"violation": False}))
    BILLED.append(VOICE)
    return _Resp("A short contribution that moves the point along.", cost=VOICE)


async def _run(db, run_id, turns):
    BILLED.clear()
    await db.create_run(run_id=run_id, topic="t", cast=CAST, name=run_id,
                        config={"max_messages": turns, "generate_avatars": False},
                        owner_sub=TEST_OWNER)
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_priced_llm):
        spent = 0.0
        for turn in range(turns):
            out = await orchestration.execute_slice(db, run_id, turn=turn, turn_budget=1,
                                                   spent_before=spent)
            spent = float(out.get("total_cost_usd") or 0.0)
            if out.get("done"):
                break
    return spent


async def test_speaker_selection_is_on_the_event_and_in_the_total(db):
    await _run(db, "c1", turns=3)
    stats = await db.get_run_stats("c1")
    events = await db.get_events("c1")
    selected = [e for e in events if e["event_type"] == "speaker.selected"]
    assert selected and all(
        (e["payload"] if isinstance(e["payload"], dict) else json.loads(e["payload"]))
        .get("cost_usd") == SELECTION for e in selected
    ), "the selection call's cost was dropped again"
    assert stats["total_cost_usd"] == pytest_approx(sum(BILLED)), "a billed call was not counted"
    assert set(stats["cost_by_kind"]) >= {"agent.response", "speaker.selected"}


async def test_a_rejected_attempt_is_counted_once(db):
    """A regeneration's discarded attempt is real spend, and it was on no event — the page showed
    $0.321 for $0.521 billed in this very fixture. Counted on `validation.checked`; the KEPT attempt
    is counted on `agent.response`; nothing twice."""
    await _run(db, "c3", turns=3)
    stats = await db.get_run_stats("c3")
    # Discarded attempts are voice calls billed minus responses kept. Not "failed checks": a check
    # that fails on the LAST allowed attempt keeps that attempt (costed on its `agent.response`),
    # so counting failed checks over-counts — which is how this test was wrong the first time.
    voice_calls = sum(1 for c in BILLED if c == VOICE)
    discarded = voice_calls - stats["turn_count"]
    assert discarded > 0, "premise: this fixture regenerates"
    assert stats["cost_by_kind"]["validation.checked"] == pytest_approx(discarded * VOICE)
    assert stats["cost_by_kind"]["agent.response"] == pytest_approx(stats["turn_count"] * VOICE)
    assert stats["total_cost_usd"] == pytest_approx(sum(BILLED))


async def test_the_cap_charges_the_same_total_the_page_shows(db):
    """The two used to count different subsets. They must be one number."""
    charged = []

    async def spend(self, cost, owner_sub=None):
        charged.append(cost)

    from matrix_studio.storage.dynamo import DynamoStorage

    with patch.object(DynamoStorage, "add_user_spend", spend):
        final = await _run(db, "c2", turns=3)
    stats = await db.get_run_stats("c2")
    assert sum(charged) == pytest_approx(stats["total_cost_usd"])
    assert final == pytest_approx(stats["total_cost_usd"])


def test_an_avatar_is_charged_only_when_an_image_was_made():
    from matrix_studio.avatar import avatar_cost_usd
    import os

    assert avatar_cost_usd() == 0.08
    with patch.dict(os.environ, {"AVATAR_COST_USD": "0.05"}):
        assert avatar_cost_usd() == 0.05
    with patch.dict(os.environ, {"AVATAR_COST_USD": "not a number"}):
        assert avatar_cost_usd() == 0.08


def test_the_summary_honours_the_per_role_model():
    """`models.summary` was inert: the summary read only `config.model`. Measured on
    brainstorm-opus, pinned to Sonnet 5, summarised by Opus 5."""
    from matrix_studio.service import resolve_model

    run = {"config_json": json.dumps({"model": "opus", "models": {"summary": "sonnet"}})}
    assert resolve_model(run) == "sonnet"
    assert resolve_model(run, role="aside") == "opus", "an unnamed role still follows config.model"
    assert resolve_model(run, override="haiku") == "haiku", "an explicit override still wins"
    assert resolve_model({"config_json": json.dumps({"model": "opus"}), "parent_run_id": "p"}) is None


async def test_a_summary_is_charged_to_the_owner(db, monkeypatch):
    from matrix_studio import service

    await db.create_run(run_id="s1", topic="t", cast=CAST, name="s1", config={},
                        owner_sub=TEST_OWNER)

    async def fake_summary(**kw):
        return {"payload": {"overview": "x"}, "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.21,
                "instructions": None, "parsed": True}

    monkeypatch.setattr(service.analysis, "generate_summary", fake_summary)
    charged = []

    async def spend(cost, owner_sub=None):
        charged.append((cost, owner_sub))

    monkeypatch.setattr(db, "add_user_spend", spend)
    await service.generate_and_store_summary(db, await db.get_run("s1"))
    assert charged == [(0.21, TEST_OWNER)]


def pytest_approx(v):
    import pytest

    return pytest.approx(v, rel=1e-9, abs=1e-12)
