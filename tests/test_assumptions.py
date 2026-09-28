# SPDX-License-Identifier: Apache-2.0
"""
Working assumptions (`matrix_studio/assumptions.py`), set by the operator before a run.

Pinned by reading the prompts and events the engine actually produces:

- every persona's prompt carries the assumptions, with the rule that they are not evidence;
- a run without assumptions is byte-identical to one from before (no block, no event);
- each assumption is recorded at turn 0 on BOTH paths — local and the deployed prepare/slice path —
  and the deployed slices still put the block in the prompt (it is read from the stored config);
- the export, the brief and the summary analyst all see them, marked as not established.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio import assumptions as am
from matrix_studio import orchestration
from matrix_studio.engine import run_simulation

pytestmark = pytest.mark.asyncio

GIVEN = [{"statement": "Pilot churn is about 7%", "basis": "last quarter's cohort"},
         {"statement": "  "}, {"statement": "The launch date is fixed"}]


class _Resp:
    def __init__(self, content):
        self.choices = [MagicMock(message=MagicMock(content=content), finish_reason="stop")]
        self.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        self._hidden_params = {"response_cost": 0.001}


def _fake(log):
    state = {"i": 0}

    def fake(*_a, **kw):
        msgs = kw["messages"]
        text = " ".join(m["content"] for m in msgs)
        if "conversation moderator" in text:
            who = ("Dana", "Marcus")[state["i"] % 2]
            state["i"] += 1
            return _Resp(json.dumps({"speaker": who, "reason": "turn"}))
        if "consistency validator" in text:
            return _Resp(json.dumps({"violation": False}))
        log.append(msgs[0]["content"])
        return _Resp("If A1 holds, I would ship.")

    return fake


def _request(assumptions=GIVEN):
    config = {"max_messages": 2, "generate_avatars": False}
    if assumptions is not None:
        config["assumptions"] = assumptions
    return {"topic": "Should we launch", "config": config, "cast": [
        {"name": "Dana", "persona": "a distribution lead", "goals": ["protect the install"]},
        {"name": "Marcus", "persona": "a cost analyst", "goals": ["measure spend"]},
    ]}


async def _events(db, run_id, kind):
    out = []
    for r in await db.get_events(run_id):
        if r["event_type"] == kind:
            p = r["payload"]
            out.append({"turn": r["turn"], **(json.loads(p) if isinstance(p, str) else p)})
    return out


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


async def test_blank_statements_are_skipped_and_the_rest_numbered_in_order():
    got = am.from_config({"assumptions": GIVEN})
    assert [(a.id, a.statement) for a in got] == [("A1", "Pilot churn is about 7%"), ("A2", "The launch date is fixed")]
    assert got[0].basis == "last quarter's cohort" and got[0].source == am.OPERATOR


async def test_the_block_says_they_are_not_evidence_and_may_be_disputed():
    block = am.assumptions_block(am.from_config({"assumptions": GIVEN}))
    assert "A1: Pilot churn is about 7% (basis: last quarter's cohort)" in block
    assert "NOT established facts" in block and "not evidence" in block and "by its id" in block
    assert am.assumptions_block([]) == ""


async def test_the_cap_holds():
    many = [{"statement": f"s{i}"} for i in range(20)]
    assert len(am.from_config({"assumptions": many})) == am.MAX_ASSUMPTIONS


# --------------------------------------------------------------------------- #
# The engine, local path
# --------------------------------------------------------------------------- #


async def test_every_persona_reasons_from_them_and_they_are_recorded_at_turn_zero(db):
    log = []
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake(log)):
        await run_simulation(_request(), db=db, run_id="as1")
    assert log and all("A1: Pilot churn is about 7%" in p and "A2: The launch date is fixed" in p for p in log)
    made = await _events(db, "as1", "assumption.made")
    assert [(m["turn"], m["id"], m["source"]) for m in made] == [(0, "A1", "operator"), (0, "A2", "operator")]


async def test_no_assumptions_means_no_block_and_no_event(db):
    log = []
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake(log)):
        await run_simulation(_request(assumptions=None), db=db, run_id="as2")
    assert log and all("Working assumptions" not in p for p in log)
    assert await _events(db, "as2", "assumption.made") == []


# --------------------------------------------------------------------------- #
# The deployed path: prepare records them, every slice reads them from the stored config
# --------------------------------------------------------------------------- #


async def test_the_deployed_path_records_and_prompts_them(db):
    req = _request()
    await db.create_run(run_id="as3", topic=req["topic"], cast=req["cast"], config=req["config"])
    log = []
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake(log)):
        await orchestration.prepare_run(db, "as3")
        for t in range(2):
            await orchestration.execute_slice(db, "as3", turn=t, turn_budget=1)
    assert [m["id"] for m in await _events(db, "as3", "assumption.made")] == ["A1", "A2"]
    assert len(log) == 2 and all("A1: Pilot churn is about 7%" in p for p in log)


# --------------------------------------------------------------------------- #
# Where they are read: the export, the brief, the summary analyst
# --------------------------------------------------------------------------- #


async def test_the_export_and_brief_list_them_as_not_established(db):
    from matrix_studio import brief as br
    from matrix_studio import export as ex

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake([])):
        await run_simulation(_request(), db=db, run_id="as4")
    model = await ex.run_model(db, await db.get_run("as4"))
    assert [a["id"] for a in model["assumptions"]] == ["A1", "A2"]
    md = ex.render(model, "md")
    assert "## Working assumptions" in md and ex.ASSUMPTIONS_NOTE in md and "last quarter's cohort" in md
    brief = br.render_markdown(br.run_brief(model))
    assert "Assumed, not established" in brief and "A1: Pilot churn is about 7%" in brief


async def test_the_summary_analyst_is_told_they_are_not_facts(db, monkeypatch):
    from matrix_studio import analysis, service

    seen = []

    async def fake(messages, model=None, temperature=0.4, max_tokens=None):
        seen.append(messages[1]["content"])
        return {"content": '{"overview": "o"}', "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0}

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake([])):
        await run_simulation(_request(), db=db, run_id="as5")
    monkeypatch.setattr(analysis, "_acompletion", fake)
    await service.generate_and_store_summary(db, await db.get_run("as5"))
    assert "NOT established facts" in seen[-1] and "A1: Pilot churn is about 7%" in seen[-1]


async def test_the_api_takes_them_and_refuses_an_empty_statement():
    from pydantic import ValidationError

    from matrix_studio.api.app import CreateRunModel

    body = _request([{"statement": "Churn is 7%", "basis": "cohort"}])
    assert CreateRunModel(**body).config.assumptions[0].statement == "Churn is 7%"
    with pytest.raises(ValidationError):
        CreateRunModel(**_request([{"statement": ""}]))


# --------------------------------------------------------------------------- #
# Slice B: the moderator adds one when a gap blocks the room
# --------------------------------------------------------------------------- #


def _fake_dynamic(log, checks, propose=True):
    state = {"i": 0}

    def fake(*_a, **kw):
        msgs = kw["messages"]
        text = " ".join(m["content"] for m in msgs)
        if "You keep a discussion moving" in text:
            checks.append(text)
            body = ({"assumption": {"statement": "Churn is about 7%", "basis": "both guessed 5-9%",
                                    "gap": "churn, asked by Dana twice"}} if propose else {"assumption": None})
            return _Resp(json.dumps(body))
        if "conversation moderator" in text:
            who = ("Dana", "Marcus")[state["i"] % 2]
            state["i"] += 1
            return _Resp(json.dumps({"speaker": who, "reason": "turn"}))
        if "consistency validator" in text:
            return _Resp(json.dumps({"violation": False}))
        log.append(msgs[0]["content"])
        return _Resp("I need the churn number before I decide.")

    return fake


def _dyn_request(every=1, limit=1, turns=3):
    req = _request(assumptions=None)
    req["config"]["max_messages"] = turns
    req["config"]["dynamic_assumptions"] = {"enabled": True, "every": every, "limit": limit}
    return req


async def test_due_respects_every_the_cap_and_a_retried_turn():
    s = am.DynamicSettings(True, every=2, limit=1)
    assert not am.due(s, 1, []) and am.due(s, 2, [])
    made = [am.Assumption("A1", "x", source=am.MODERATOR, turn=2)]
    assert not am.due(s, 2, made), "a retried slice must not add a second one for the same turn"
    assert not am.due(s, 4, made), "cap spent"
    assert am.due(am.DynamicSettings(True, 2, 2), 4, made + [am.Assumption("A2", "y")])
    assert not am.due(am.DynamicSettings(), 4, [])


async def test_a_malformed_proposal_is_no_assumption():
    assert am.parse_proposal({"assumption": None}) is None
    assert am.parse_proposal({"assumption": {"statement": "  "}}) is None
    assert am.parse_proposal(None) is None
    assert am.parse_proposal({"assumption": {"statement": "x", "basis": "b"}})["statement"] == "x"


async def test_the_moderator_adds_one_and_later_turns_reason_from_it(db):
    log, checks = [], []
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_dynamic(log, checks)):
        await run_simulation(_dyn_request(every=1, limit=1, turns=3), db=db, run_id="dy1")
    made = await _events(db, "dy1", "assumption.made")
    assert [(m["id"], m["source"], m["turn"]) for m in made] == [("A1", "moderator", 1)]
    assert made[0]["gap"] == "churn, asked by Dana twice"
    assert "A1: Churn is about 7%" not in log[0] and all("A1: Churn is about 7%" in p for p in log[1:])
    checked = await _events(db, "dy1", "assumption.checked")
    assert len(checked) == 1 and checked[0]["proposed"] is True and checked[0]["cost_usd"] > 0


async def test_a_check_that_finds_no_gap_is_still_recorded_and_adds_nothing(db):
    log, checks = [], []
    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_fake_dynamic(log, checks, propose=False)):
        await run_simulation(_dyn_request(every=1, limit=3, turns=3), db=db, run_id="dy2")
    assert await _events(db, "dy2", "assumption.made") == []
    assert [c["proposed"] for c in await _events(db, "dy2", "assumption.checked")] == [False, False]
    assert all("Working assumptions" not in p for p in log)


async def test_off_by_default_means_no_check_at_all(db):
    log, checks = [], []
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_dynamic(log, checks)):
        await run_simulation(_request(assumptions=None), db=db, run_id="dy3")
    assert checks == [] and await _events(db, "dy3", "assumption.checked") == []


async def test_the_deployed_slices_carry_the_moderator_s_assumption_forward(db):
    req = _dyn_request(every=1, limit=1, turns=3)
    await db.create_run(run_id="dy4", topic=req["topic"], cast=req["cast"], config=req["config"])
    log, checks = [], []
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_dynamic(log, checks)):
        await orchestration.prepare_run(db, "dy4")
        for t in range(3):
            await orchestration.execute_slice(db, "dy4", turn=t, turn_budget=1)
    assert [m["id"] for m in await _events(db, "dy4", "assumption.made")] == ["A1"]
    # Made in turn 2's slice; turn 3's slice is a fresh call that must rebuild it from the log.
    # (More prompts than turns: the fake does not answer the validator, so turns are regenerated.)
    assert "A1:" not in log[0] and len(log) >= 3 and all("A1: Churn is about 7%" in p for p in log[1:])
    assert len(checks) == 1, "the cap must hold across slices, not per slice"


async def test_operator_and_moderator_ids_do_not_collide(db):
    log, checks = [], []
    req = _dyn_request(every=1, limit=1, turns=2)
    req["config"]["assumptions"] = [{"statement": "Launch date is fixed"}]
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_dynamic(log, checks)):
        await run_simulation(req, db=db, run_id="dy5")
    assert [(m["id"], m["source"]) for m in await _events(db, "dy5", "assumption.made")] == [
        ("A1", "operator"), ("A2", "moderator")]


async def test_the_forecast_prices_checks_or_says_it_cannot():
    from matrix_studio import forecast

    req = _dyn_request(every=4, turns=40)
    kw = dict(default_model="m", default_max=40, avatars_default=False, avatar_price=0.08)
    part = next(p for p in forecast.forecast_run(req, forecast.History(), **kw)["parts"]
                if p["part"] == "assumption checks")
    assert part["measured"] is False and "9 check(s)" in part["basis"]
    priced = forecast.History(assumption_check_costs=[0.0005, 0.001])
    part = next(p for p in forecast.forecast_run(req, priced, **kw)["parts"] if p["part"] == "assumption checks")
    assert part["measured"] and part["high"] == pytest.approx(0.04)
