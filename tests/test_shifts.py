# SPDX-License-Identifier: Apache-2.0
"""
Flagging an announced change of position (`matrix_studio/shifts.py`). Flag-only.

Pinned: realised shifts are found and prospective ones are not (the forms measured on stored runs); what a
shift credits comes from the names it uses and is never guessed; a defended persona moving with none of its
stated conditions named is marked; the engine records it beside the message on a real run, and the export
and brief carry it.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from matrix_studio import shifts as sh
from matrix_studio.engine import run_simulation
from matrix_studio.personas import parse_structured

pytestmark = pytest.mark.asyncio

REALISED = [
    "The pricing study is real, it moved me, and it's driving the plan.",
    "Mina drew the line I needed to hear, and I'll give ground on it rather than pretend otherwise.",
    "Those are separate questions, and I was wrong to let my confidence on pricing carry it.",
    "The pilot is the mechanism I asked for and it's what changed my mind, not the volume of voices.",
    "I'm content with the record — my position moved on the churn figure, and that is all.",
]
PROSPECTIVE = [
    "That's the kind of concrete condition that would actually move me off no launch needed.",
    "I need to see that number myself before I concede it.",
    "Nothing in this last round changes my position, so I'll keep it brief.",
    "If any regulator classed this as a paid service, that changes my position immediately.",
    "Nobody has produced the data, so nothing has actually moved me.",
    "What I'd actually change my mind on is a pilot showing churn under five percent.",
]


@pytest.mark.parametrize("text", REALISED)
async def test_a_realised_shift_is_found(text):
    assert sh.shift_sentences(text)


@pytest.mark.parametrize("text", PROSPECTIVE)
async def test_a_prospective_or_denied_shift_is_not(text):
    assert sh.shift_sentences(text) == []


async def test_credit_comes_from_the_names_used_and_is_never_guessed():
    kw = dict(personas=["Dana", "Mina", "Theo"], speaker="Theo", consultants=["Ada"],
              injected=["Customer"], assumptions=["A1", "A12"])
    msg = "Mina drew the line, and I'll give ground on it."
    assert sh.credited(sh.shift_sentences(msg), msg, **kw) == [{"kind": "persona", "name": "Mina"}]
    msg = "If A1 holds and the Customer is right, it moved me."
    assert sh.credited(sh.shift_sentences(msg), msg, **kw) == [
        {"kind": "assumption", "name": "A1"}, {"kind": "scheduled message", "name": "Customer"}]
    msg = "I was wrong about that."
    assert sh.credited(sh.shift_sentences(msg), msg, **kw) == [], "nobody named means nobody credited"
    msg = "Theo, it moved me."
    assert sh.credited(sh.shift_sentences(msg), msg, **kw) == [], "a speaker never credits itself"


STRUCTURED = parse_structured({"viewpoints": [
    {"position": "The free plan is fine", "firmness": "firm",
     "evidence_that_shifts": ["a regulation that the free plan would breach",
                              "a ruling that bundled support is a regulated service"]},
    {"position": "Price it low", "firmness": "negotiable", "evidence_that_shifts": ["churn data"]},
]})


async def test_a_defended_move_with_none_of_its_conditions_named_is_marked():
    f = sh.flag("Mina drew the line, and I'll give ground on it.", speaker="Theo", structured=STRUCTURED,
                personas=["Mina", "Theo"])
    assert f["no_listed_condition"] is True and len(f["conditions"]) == 2, "only defended positions count"
    g = sh.flag("The regulation the free plan would breach is real, so it moved me.", speaker="Theo",
                structured=STRUCTURED, personas=["Mina", "Theo"])
    assert g["no_listed_condition"] is False and g["matched_conditions"]
    assert sh.flag("Nothing has moved me.", speaker="Theo", structured=STRUCTURED, personas=[]) is None


# --------------------------------------------------------------------------- #
# On a run
# --------------------------------------------------------------------------- #


class _Resp:
    def __init__(self, content):
        self.choices = [MagicMock(message=MagicMock(content=content), finish_reason="stop")]
        self.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        self._hidden_params = {"response_cost": 0.001}


def _fake():
    state = {"i": 0}
    lines = {"Mina": "Nothing in that changes my position.",
             "Theo": "Mina drew the line I needed, and I'll give ground on it."}

    def fake(*_a, **kw):
        text = " ".join(m["content"] for m in kw["messages"])
        if "conversation moderator" in text:
            who = ("Mina", "Theo")[state["i"] % 2]
            state["i"] += 1
            return _Resp(json.dumps({"speaker": who, "reason": "turn"}))
        if "consistency validator" in text:
            return _Resp(json.dumps({"violation": False}))
        who = "Theo" if "a product lead" in kw["messages"][0]["content"] else "Mina"
        return _Resp(lines[who])

    return fake


def _request():
    return {"topic": "Should we offer a free plan", "config": {"max_messages": 2, "generate_avatars": False,
                                                             "personas": {"enabled": True}},
            "cast": [{"name": "Mina", "persona": "a compliance lead", "goals": ["stay compliant"]},
                     {"name": "Theo", "persona": "a product lead", "goals": ["ship the free plan"],
                      "structured": {"viewpoints": [
                          {"position": "The free plan is fine", "firmness": "firm",
                           "evidence_that_shifts": ["a regulation that the free plan would breach"]}]}}]}


async def test_the_engine_flags_it_beside_the_message_and_the_record_carries_it(db):
    from matrix_studio import brief as br
    from matrix_studio import export as ex

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake()):
        await run_simulation(_request(), db=db, run_id="sh1")
    shifts = [json.loads(e["payload"]) if isinstance(e["payload"], str) else e["payload"]
              for e in await db.get_events("sh1") if e["event_type"] == "position.shift"]
    assert len(shifts) == 1, "Mina's 'nothing changes my position' must not be flagged"
    s = shifts[0]
    assert s["speaker"] == "Theo" and s["credits"] == [{"kind": "persona", "name": "Mina"}]
    assert s["no_listed_condition"] is True

    model = await ex.run_model(db, await db.get_run("sh1"))
    md = ex.render(model, "md")
    assert "⚑ Theo says their position moved; credits Mina (persona)." in md
    assert "None of their stated conditions appears to be named" in md
    assert "1 position shift(s) flagged, 1 naming none" in br.render_markdown(br.run_brief(model))
