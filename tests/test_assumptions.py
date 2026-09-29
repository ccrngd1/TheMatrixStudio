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
                                    "gap": "churn, asked by Dana twice",
                                    "asks": ["I need the churn number before I decide"]}}
                    if propose else {"assumption": None})
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
    assert am.parse_proposal({"assumption": None})[0] is None
    assert am.parse_proposal({"assumption": {"statement": "  "}})[0] is None
    assert am.parse_proposal(None)[0] is None


CONV = [{"speaker": "Dana", "content": "I need the churn number before I decide anything."},
        {"speaker": "Marcus", "content": "Fine, but what\u2019s the churn number, roughly?"},
        {"speaker": "Dana", "content": "Nobody here knows it."}]


async def test_a_proposal_must_quote_two_real_asks():
    ok, why = am.parse_proposal({"assumption": {"statement": "Churn is 7%", "asks": [
        "I need the churn number before I decide", "what's the churn number, roughly"]}}, CONV)
    assert ok is not None and ok["statement"] == "Churn is 7%" and why == ""
    one, why = am.parse_proposal({"assumption": {"statement": "Churn is 7%", "asks": [
        "I need the churn number before I decide", "we must know churn before launch"]}}, CONV)
    assert one is None and "1 message" in why, "an invented ask must not count"
    none, _ = am.parse_proposal({"assumption": {"statement": "Churn is 7%"}}, CONV)
    assert none is None


async def test_the_same_words_from_two_messages_are_two_asks():
    conv = [{"speaker": "A", "content": "We need the churn number."}, {"speaker": "B", "content": "We need the churn number."}]
    assert am.parse_proposal({"assumption": {"statement": "s", "asks": ["we need the churn number"]}}, conv)[0]


async def test_the_moderator_adds_one_and_later_turns_reason_from_it(db):
    log, checks = [], []
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_dynamic(log, checks)):
        await run_simulation(_dyn_request(every=1, limit=1, turns=3), db=db, run_id="dy1")
    made = await _events(db, "dy1", "assumption.made")
    # After turn 1 there is only one ask in the room, so the check must refuse; after turn 2, two.
    assert [(m["id"], m["source"], m["turn"]) for m in made] == [("A1", "moderator", 2)]
    assert made[0]["gap"] == "churn, asked by Dana twice" and made[0]["asks"]
    assert all("A1:" not in p for p in log[:2]) and "A1: Churn is about 7%" in log[-1]
    checked = await _events(db, "dy1", "assumption.checked")
    assert [c["proposed"] for c in checked] == [False, True] and checked[1]["cost_usd"] > 0


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
    # Made after turn 2, in turn 3's slice, which rebuilt the ledger from the log; a 4th slice would
    # rebuild it again. Turn 3's prompts carry it.
    assert "A1: Churn is about 7%" in log[-1] and "A1:" not in log[0]
    assert len(checks) == 2, "checked after turns 1 and 2, then the cap of 1 holds across slices"


async def test_operator_and_moderator_ids_do_not_collide(db):
    log, checks = [], []
    req = _dyn_request(every=1, limit=1, turns=3)
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


async def test_a_discarded_proposal_is_recorded_with_its_reason(db):
    def fake(*_a, **kw):
        text = " ".join(m["content"] for m in kw["messages"])
        if "You keep a discussion moving" in text:
            return _Resp(json.dumps({"assumption": {"statement": "The plan will work", "asks": ["nobody said this"]}}))
        if "conversation moderator" in text:
            return _Resp(json.dumps({"speaker": "Dana", "reason": "turn"}))
        return _Resp("I need the churn number before I decide.")

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=fake):
        await run_simulation(_dyn_request(every=1, limit=2, turns=2), db=db, run_id="dy6")
    assert await _events(db, "dy6", "assumption.made") == []
    [c] = await _events(db, "dy6", "assumption.checked")
    assert c["proposed"] is False and "0 message(s)" in c["rejected"]
    assert c["proposal"]["statement"] == "The plan will work"


# --------------------------------------------------------------------------- #
# Slice C: fork with a different assumption, or with it withdrawn
# --------------------------------------------------------------------------- #


async def _parent_and_branch(db, mutation, *, parent="fk-parent"):
    from matrix_studio import branching

    req = _request([{"statement": "Pilot churn is about 7%"}])
    req["config"]["max_messages"] = 3
    await db.create_run(run_id=parent, topic=req["topic"], cast=req["cast"], config=req["config"])
    log = []
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake(log)):
        await orchestration.prepare_run(db, parent)
        for t in range(3):
            await orchestration.execute_slice(db, parent, turn=t, turn_budget=1)
        meta = await branching.create_branch_run(db, await db.get_run(parent), from_turn=1, mutation=mutation)
        bid = meta["run_id"]
        log.clear()
        payload = await orchestration.prepare(db, bid, mode="branch", parent_run_id=parent, from_turn=1,
                                              mutation=mutation)
        while not payload["done"]:
            payload = await orchestration.execute_slice(db, bid, turn=payload["turn"], turn_budget=1)
    return bid, log


async def test_a_fork_replaces_the_assumption_for_every_turn_after_it(db):
    bid, log = await _parent_and_branch(
        db, {"kind": "replace_assumption", "assumption_id": "A1", "statement": "Pilot churn is about 12%"})
    assert log and all("A1: Pilot churn is about 12%" in p and "7%" not in p for p in log)
    made = await _events(db, bid, "assumption.made")
    assert made[-1]["statement"] == "Pilot churn is about 12%" and made[-1]["replaces"] == "Pilot churn is about 7%"
    # The parent is untouched.
    assert [m["statement"] for m in await _events(db, "fk-parent", "assumption.made")] == ["Pilot churn is about 7%"]
    # The record shows the one the branch actually used.
    from matrix_studio import export as ex
    model = await ex.run_model(db, await db.get_run(bid))
    assert [a.statement for a in am.from_events(await db.get_events(bid))] == ["Pilot churn is about 12%"]
    assert "12%" in ex.render(model, "md")


async def test_a_fork_can_withdraw_it(db):
    bid, log = await _parent_and_branch(
        db, {"kind": "withdraw_assumption", "assumption_id": "A1"}, parent="fk-parent-2")
    assert log and all("Working assumptions" not in p for p in log)
    assert am.from_events(await db.get_events(bid)) == []


async def test_forking_an_assumption_that_is_not_in_force_is_refused(db):
    from matrix_studio.engine.simulator import BranchMutationError

    with pytest.raises(BranchMutationError, match="no assumption 'A9'"):
        await _parent_and_branch(
            db, {"kind": "replace_assumption", "assumption_id": "A9", "statement": "x"}, parent="fk-parent-3")


async def test_the_api_normalises_the_two_kinds():
    from matrix_studio.api.app import BranchMutationModel, _SUPPORTED_MUTATION_KINDS

    assert {"replace_assumption", "withdraw_assumption"} <= _SUPPORTED_MUTATION_KINDS
    m = BranchMutationModel(kind="replace_assumption", assumption_id="A1", statement="  Churn  is 12% ")
    assert m.statement.strip() == "Churn  is 12%"


# --------------------------------------------------------------------------- #
# Cited and disputed
# --------------------------------------------------------------------------- #

_T = [
    {"speaker": "Marcus", "turn": 1, "message": "Using A1's 2-5% range as our baseline, we ship."},
    {"speaker": "Dana", "turn": 2, "message": "I can work with it. But A1 is a benchmark, not a guarantee our users behave the same way."},
    {"speaker": "Marcus", "turn": 3, "message": "If A2 doesn't hold and we get 12 responses, we pause."},
    {"speaker": "Dana", "turn": 4, "message": "A12 is irrelevant here. That 7% is soft."},
]


async def test_usage_counts_citations_and_flags_disputes_but_not_conditionals():
    u = am.usage(["A1", "A2"], _T)
    assert u["A1"]["cited"] == 2 and [d["speaker"] for d in u["A1"]["disputes"]] == ["Dana"]
    assert "not a guarantee" in u["A1"]["disputes"][0]["sentence"]
    # "If A2 doesn't hold" reasons from the assumption; it does not dispute it.
    assert u["A2"] == {"cited": 1, "disputes": []}


async def test_an_id_matches_as_a_word_only_and_an_unnamed_dispute_is_not_counted():
    u = am.usage(["A1"], [_T[3]])
    assert u["A1"] == {"cited": 0, "disputes": []}, "A12 is not A1, and 'that 7% is soft' names no id"


async def test_the_brief_lists_disputed_ones_first_and_the_report_quotes_them():
    from matrix_studio import brief as br
    from matrix_studio import export as ex
    from tests.test_export import _run_model

    dispute = {"speaker": "Dana", "turn": 2, "sentence": "A2 is not a guarantee."}
    m = _run_model(assumptions=[
        {"id": "A1", "statement": "Churn is 7%", "basis": "", "source": "operator", "turn": 0, "cited": 3, "disputes": []},
        {"id": "A2", "statement": "Launch in May", "basis": "", "source": "moderator", "turn": 2, "cited": 1,
         "disputes": [dispute]},
    ])
    assert br.run_brief(m)["assumptions"] == ["A2 (disputed ×1): Launch in May", "A1: Churn is 7%"]
    md = ex.render(m, "md")
    assert "appears DISPUTED in 1: Dana (turn 2): “A2 is not a guarantee.”" in md
    assert "cited in 3 message(s)" in md


async def test_the_check_is_told_to_ask_a_consultant_first_only_when_there_is_one():
    from matrix_studio.experts import Expert

    with_c = am.propose_messages("t", [], [], consultants=[Expert("Ada", "the statute")])[0]["content"]
    assert "Ada: the statute" in with_c and "the room should ask, not assume" in with_c
    without = am.propose_messages("t", [], [])[0]["content"]
    assert "consultant" not in without.lower()


async def test_on_a_run_with_consultants_the_check_really_receives_them(db):
    log, checks = [], []
    req = _dyn_request(every=1, limit=1, turns=2)
    req["config"]["retrieval"] = {"enabled": True, "k": 2, "max_chars": 900}
    req["config"]["experts"] = [{"name": "Ada", "expertise": "the churn records",
                                 "document_texts": [{"title": "n", "text": "Churn was 6% last year."}]}]
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake_dynamic(log, checks)):
        await run_simulation(req, db=db, run_id="dy7")
    assert checks and "Ada: the churn records" in checks[0] and "the room should ask" in checks[0]


async def test_a_repeat_of_an_assumption_in_force_is_rejected_and_a_written_id_is_dropped():
    ledger = [am.Assumption("A2", "Of the 900 weekly signups, 60-70% arrive from the partner channel", source=am.MODERATOR, turn=16)]
    conv = [{"speaker": "A", "content": "what share come from partners?"}, {"speaker": "B", "content": "what share come from partners?"}]
    dup, why = am.parse_proposal({"assumption": {"statement": "A3: Of the 900 weekly signups, 60-70% arrive from the partner channel.",
                                                "asks": ["what share come from partners"]}}, conv, ledger)
    assert dup is None and why == "repeats A2, already in force"
    ok, _ = am.parse_proposal({"assumption": {"statement": "A3: Records are retrievable in 85% of cases",
                                             "asks": ["what share come from partners"]}}, conv, ledger)
    assert ok["statement"] == "Records are retrievable in 85% of cases"
