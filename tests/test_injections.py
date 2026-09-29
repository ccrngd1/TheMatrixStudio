# SPDX-License-Identifier: Apache-2.0
"""
Scheduled injections (`matrix_studio/injections.py`): operator messages at a turn fixed in the config.

Pinned against the events and prompts the engine produces:

- delivered once, after the turn it follows, on the local AND the deployed one-turn-per-slice path;
- it never eats a generated turn: the run still generates `max_messages` turns of its own;
- a later speaker reads it; a voice outside the cast is not a participant anywhere, including when a
  branch rebuilds the run from its log;
- a fork at the injection's own turn does not deliver it a second time;
- the API refuses one that could never be delivered, and an ensemble may vary injections but not as a
  cast member's words.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio import injections as inj
from matrix_studio import orchestration
from matrix_studio.engine import run_simulation

pytestmark = pytest.mark.asyncio

LETTER = "The regulator writes: renewals without a new exam are under review this quarter."


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
        log.append(text)
        return _Resp("Noted, and it changes my view.")

    return fake


def _request(injections, turns=3):
    return {"topic": "Should renewals skip the exam", "config": {
        "max_messages": turns, "generate_avatars": False, "injections": injections},
        "cast": [{"name": "Dana", "persona": "a cautious lead", "goals": ["avoid risk"]},
                 {"name": "Marcus", "persona": "a growth lead", "goals": ["grow"]}]}


async def _events(db, run_id, kind):
    out = []
    for r in await db.get_events(run_id):
        if r["event_type"] == kind:
            p = r["payload"]
            out.append({"turn": r["turn"], "seq": r["seq"], **(json.loads(p) if isinstance(p, str) else p)})
    return out


LETTER_AFTER_1 = [{"after_turn": 1, "speaker": "Regulator", "content": LETTER}]


async def test_parsing_skips_incomplete_ones_and_numbers_the_rest():
    got = inj.from_config({"injections": [{"after_turn": 2, "speaker": "Customer", "content": "Hi"},
                                          {"after_turn": 3, "speaker": "", "content": "x"},
                                          {"after_turn": "x", "speaker": "A", "content": "y"},
                                          {"after_turn": 0, "speaker": "Letter", "content": "Z"}]})
    assert [(i.key, i.after_turn, i.speaker) for i in got] == [("I1", 2, "Customer"), ("I2", 0, "Letter")]
    assert inj.due(got, 2, [{"injection": "I1"}]) == []


async def test_delivered_once_after_its_turn_and_read_by_the_next_speaker(db):
    log = []
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake(log)):
        await run_simulation(_request(LETTER_AFTER_1), db=db, run_id="in1")
    responses = await _events(db, "in1", "agent.response")
    injected = [r for r in responses if r.get("injected")]
    assert len(injected) == 1 and injected[0]["speaker"] == "Regulator" and injected[0]["turn"] == 1
    assert injected[0]["source"] == "operator" and injected[0]["injection"] == "I1"
    # It sits after turn 1's own response and before turn 2's.
    order = [(r["turn"], bool(r.get("injected"))) for r in sorted(responses, key=lambda r: r["seq"])]
    assert order == [(1, False), (1, True), (2, False), (3, False)]
    # It never ate a turn: the run still generated its three.
    assert sum(1 for r in responses if not r.get("injected")) == 3
    # The first speaker never saw it; the later ones did.
    assert LETTER not in log[0] and all(LETTER in t for t in log[1:])


async def test_the_deployed_slices_deliver_it_once(db):
    req = _request(LETTER_AFTER_1)
    await db.create_run(run_id="in2", topic=req["topic"], cast=req["cast"], config=req["config"])
    log = []
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake(log)):
        await orchestration.prepare_run(db, "in2")
        for t in range(3):
            await orchestration.execute_slice(db, "in2", turn=t, turn_budget=1)
    responses = await _events(db, "in2", "agent.response")
    assert [r["speaker"] for r in responses if r.get("injected")] == ["Regulator"]
    assert sum(1 for r in responses if not r.get("injected")) == 3
    assert LETTER in log[-1]


async def test_a_fork_at_the_injection_s_turn_does_not_deliver_it_again(db):
    from matrix_studio import branching

    req = _request(LETTER_AFTER_1)
    await db.create_run(run_id="in3", topic=req["topic"], cast=req["cast"], config=req["config"])
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake([])):
        await orchestration.prepare_run(db, "in3")
        for t in range(3):
            await orchestration.execute_slice(db, "in3", turn=t, turn_budget=1)
        meta = await branching.create_branch_run(db, await db.get_run("in3"), from_turn=1)
        bid = meta["run_id"]
        payload = await orchestration.prepare(db, bid, mode="branch", parent_run_id="in3", from_turn=1)
        while not payload["done"]:
            payload = await orchestration.execute_slice(db, bid, turn=payload["turn"], turn_budget=1)
    injected = [r for r in await _events(db, bid, "agent.response") if r.get("injected")]
    assert len(injected) == 1, "the branch copied the injection and then delivered it again"


async def test_a_voice_outside_the_cast_is_never_made_a_participant_by_a_rebuild(db):
    from matrix_studio.branching import reconstruct_at_turn

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake([])):
        await run_simulation(_request(LETTER_AFTER_1), db=db, run_id="in4")
    _topic, agents, conversation, _t, _l = await reconstruct_at_turn(db, await db.get_run("in4"), 3)
    assert "Regulator" not in agents
    letter = [m for m in conversation if m["speaker"] == "Regulator"]
    assert letter and letter[0]["injection"] == "I1"


async def test_the_export_says_it_was_injected(db):
    from matrix_studio import export as ex

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake([])):
        await run_simulation(_request(LETTER_AFTER_1), db=db, run_id="in5")
    md = ex.render(await ex.run_model(db, await db.get_run("in5")), "md")
    assert "Regulator *(injected by the operator)*" in md


async def test_the_api_refuses_one_that_could_never_be_delivered():
    from pydantic import ValidationError

    from matrix_studio.api.app import CreateRunModel

    assert CreateRunModel(**_request([{"after_turn": 2, "speaker": "Customer", "content": "x"}])).config.injections
    with pytest.raises(ValidationError, match="never be delivered"):
        CreateRunModel(**_request([{"after_turn": 3, "speaker": "Customer", "content": "x"}]))


async def test_an_ensemble_may_vary_injections_and_assumptions_but_not_a_cast_member_s_words():
    from pydantic import ValidationError

    from matrix_studio import ensemble_spec as es
    from matrix_studio.api.app import CreateEnsembleModel

    es.validate([es.Cell("base", 2), es.Cell("with-letter", 2, {"injections": LETTER_AFTER_1}),
                 es.Cell("higher", 2, {"assumptions": [{"statement": "Churn is 12%"}]})])
    with pytest.raises(es.SpecError, match="personas"):
        es.validate([es.Cell("base", 2), es.Cell("lean", 2, {"personas.evidence_lean": True})])

    body = {**_request([]), "cells": [{"label": "base", "n": 2},
                                      {"label": "said", "n": 2, "overrides": {"injections": [
                                          {"after_turn": 1, "speaker": "dana", "content": "I give up."}]}}]}
    with pytest.raises(ValidationError, match="cast member"):
        CreateEnsembleModel(**body)


async def test_a_reply_that_opens_with_its_own_name_loses_it_and_nothing_else():
    from matrix_studio.engine.simulator import strip_own_name

    assert strip_own_name("Dana: I hear the urgency.", "Dana") == "I hear the urgency."
    assert strip_own_name("**Dana:** Fine.", "Dana") == "Fine."
    assert strip_own_name("Marcus: you are wrong.", "Dana") == "Marcus: you are wrong."
    assert strip_own_name("I told Dana: no.", "Dana") == "I told Dana: no."
    assert strip_own_name("Dana:", "Dana") == "Dana:"
