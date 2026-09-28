# SPDX-License-Identifier: Apache-2.0
"""
Consultants (`matrix_studio/experts.py`): experts outside the room whom personas can ask.

Pinned, by reading the prompts and events the engine actually produces:

- a persona is told who it may ask, and only when there is someone and retrieval is on;
- an ASK line is taken out of the persona's message and answered from the CONSULTANT's own sources
  — never another persona's — and the answer enters the conversation attributed to the consultant;
- the consultant never becomes a speaker, and the per-run cap holds;
- a branch replays the consultant's answer into the conversation it rebuilds;
- a request with consultants and retrieval off is refused, since the consultant would have nothing.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio import experts as ex
from matrix_studio.engine import run_simulation

pytestmark = pytest.mark.asyncio

ADA = ex.Expert("Ada", "the cost records")
COST_NOTE = (
    "Spend must be measured on a realistic run before a feature ships.\n\n"
    "The baseline is recorded per turn and every new call is a delta against it."
)
DANA_NOTE = "The install must stay one process with no external services."


class _Resp:
    def __init__(self, content):
        self.choices = [MagicMock(message=MagicMock(content=content), finish_reason="stop")]
        self.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        self._hidden_params = {"response_cost": 0.001}


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


async def test_parse_ask_takes_the_last_ask_to_a_listed_consultant():
    text = "We need the number.\nASK Ada: What does the baseline cost per turn?"
    cleaned, ask = ex.parse_ask(text, [ADA])
    assert cleaned == "We need the number."
    assert ask == (ADA, "What does the baseline cost per turn?")


async def test_an_ask_naming_nobody_is_left_as_said():
    cleaned, ask = ex.parse_ask("ASK Zed: anything at all?", [ADA])
    assert ask is None and cleaned == "ASK Zed: anything at all?"


async def test_the_block_lists_consultants_and_disappears_when_the_cap_is_spent():
    block = ex.consultants_block([ADA], 2)
    assert "Ada: the cost records" in block and "ASK <consultant name>:" in block and "2 left" in block
    assert ex.consultants_block([ADA], 0) == "" and ex.consultants_block([], 3) == ""


async def test_the_block_says_what_each_consultant_holds():
    [e] = ex.from_config({"experts": [{"name": "Ada", "expertise": "costs", "knowledge_bases": ["k1"],
                                       "document_texts": [{"title": "Cost observations", "text": "x"}]}]})
    block = ex.consultants_block([e], 2)
    assert 'holds: "Cost observations", 1 knowledge base(s)' in block


async def test_consults_are_counted_from_the_conversation():
    conv = [{"speaker": "Dana", "content": "x"}, {"speaker": "Ada (consultant)", "content": "y", "consultant": True}]
    assert ex.consults_used(conv) == 1


async def test_the_answer_prompt_forbids_guessing_and_opinions():
    msgs = ex.answer_messages(ADA, "q?", "Dana", "topic", [])
    system = msgs[0]["content"]
    assert ex.NOT_IN_SOURCES in system and "ONLY" in system and "no position" in system


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #


def _request(*, experts=True, limit=None, retrieval=True, max_messages=3):
    config = {"max_messages": max_messages, "generate_avatars": False}
    if retrieval:
        config["retrieval"] = {"enabled": True, "k": 2, "max_chars": 900}
    if experts:
        config["experts"] = [{"name": "Ada", "expertise": "the cost records",
                              "document_texts": [{"title": "cost-note", "text": COST_NOTE}]}]
    if limit is not None:
        config["consult_limit"] = limit
    return {
        "topic": "Must spend be measured on a realistic run before the feature ships",
        "cast": [
            {"name": "Dana", "persona": "distribution lead", "goals": ["protect the install"],
             "document_texts": [{"title": "dana-note", "text": DANA_NOTE}]},
            {"name": "Marcus", "persona": "cost analyst", "goals": ["measure spend"]},
        ],
        "config": config,
    }


def _fake(log):
    """Dana always asks Ada; Ada answers citing her note; the moderator alternates Dana and Marcus."""
    state = {"i": 0}

    def fake(*_a, **kw):
        msgs = kw["messages"]
        text = " ".join(m["content"] for m in msgs)
        if "conversation moderator" in text:
            who = ("Dana", "Marcus")[state["i"] % 2]
            state["i"] += 1
            return _Resp(json.dumps({"speaker": who, "reason": "turn"}))
        if "You are Ada, a consultant" in text:
            log.append(("consultant", text))
            return _Resp("Spend must be measured on a realistic run first [cost-note #0].")
        log.append(("persona", msgs[0]["content"], text))
        if "distribution lead" in msgs[0]["content"]:
            return _Resp("I need the number before I agree.\nASK Ada: Must spend be measured on a realistic run?")
        return _Resp("Measure it, then decide.")

    return fake


async def _events(db, run_id, kind):
    out = []
    for r in await db.get_events(run_id):
        if r["event_type"] == kind:
            p = r["payload"]
            out.append({"turn": r["turn"], "agent": r["agent_name"], "payload": json.loads(p) if isinstance(p, str) else p})
    return out


async def test_a_persona_asks_and_the_consultant_answers_from_its_own_sources(db):
    log = []
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake(log)):
        await run_simulation(_request(), db=db, run_id="ex1")

    responses = await _events(db, "ex1", "agent.response")
    dana = [r for r in responses if r["agent"] == "Dana"]
    assert dana and all("ASK Ada" not in r["payload"]["message"] for r in dana), "the ASK line was spoken"

    answers = await _events(db, "ex1", "expert.answered")
    assert answers, "no consultation happened"
    a = answers[0]["payload"]
    assert a["expert"] == "Ada" and a["asked_by"] == "Dana"
    assert a["question"] == "Must spend be measured on a realistic run?"
    assert "[cost-note #0]" in a["answer"] and a["cost_usd"] > 0

    # The consultant read ITS note, not Dana's.
    retrieved = [e for e in await _events(db, "ex1", "document.retrieved") if e["agent"] == "Ada"]
    assert retrieved and all(p["title"] == "cost-note" for e in retrieved for p in e["payload"]["passages"])
    consultant_prompts = [t for kind, t, *_ in log if kind == "consultant"]
    assert consultant_prompts and "one process" not in consultant_prompts[0]

    # A later speaker reads the answer, attributed to the consultant.
    later = [full for kind, _sys, full in (x for x in log if x[0] == "persona")]
    assert any("Ada (consultant): Spend must be measured" in t for t in later[1:])

    # Never a speaker.
    assert all(r["agent"] != "Ada" for r in responses)


async def test_the_cap_holds(db):
    log = []
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake(log)):
        await run_simulation(_request(limit=1, max_messages=5), db=db, run_id="ex2")
    assert len(await _events(db, "ex2", "expert.answered")) == 1
    # After the cap, personas are no longer told they can ask.
    persona_prompts = [sys for kind, sys, *_ in log if kind == "persona"]
    assert "Consultants you may ask" in persona_prompts[0]
    assert "Consultants you may ask" not in persona_prompts[-1]


async def test_no_consultants_means_no_block_and_no_consultation(db):
    log = []
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake(log)):
        await run_simulation(_request(experts=False), db=db, run_id="ex3")
    assert await _events(db, "ex3", "expert.answered") == []
    assert all("Consultants you may ask" not in sys for kind, sys, *_ in log if kind == "persona")


async def test_a_branch_replays_the_answer_into_the_conversation(db):
    from matrix_studio.branching import reconstruct_at_turn

    log = []
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake(log)):
        await run_simulation(_request(), db=db, run_id="ex4")
    run = await db.get_run("ex4")
    _topic, agents, conversation, _threads, _ledger = await reconstruct_at_turn(db, run, 3)
    consultant = [m for m in conversation if m.get("consultant")]
    assert consultant and consultant[0]["speaker"] == "Ada (consultant)"
    assert "Ada" not in agents and "Ada (consultant)" not in agents


# --------------------------------------------------------------------------- #
# The request contract
# --------------------------------------------------------------------------- #


async def test_consultants_without_retrieval_are_refused():
    from pydantic import ValidationError

    from matrix_studio.api.app import CreateRunModel

    with pytest.raises(ValidationError, match="retrieval"):
        CreateRunModel(**_request(retrieval=False))


async def test_a_consultant_cannot_share_a_persona_s_name():
    from pydantic import ValidationError

    from matrix_studio.api.app import CreateEnsembleModel, CreateRunModel

    body = _request()
    body["config"]["experts"][0]["name"] = "dana"
    with pytest.raises(ValidationError, match="already used"):
        CreateRunModel(**body)
    with pytest.raises(ValidationError, match="already used"):
        CreateEnsembleModel(**body)
