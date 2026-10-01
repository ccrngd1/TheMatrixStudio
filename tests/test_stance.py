# SPDX-License-Identifier: Apache-2.0
"""Where each persona ended a run (docs/MOBILE-UI.md §6.1).

The summary rule (option 1): dissenters hold, a flagged shift that is not a dissenter supports, everyone else
is unstated — and with no dissenter list, nobody has a stance at all. Since 2026-10-01 a closing statement
decides first, when the classifier's verdict is backed by a quote that really is in it.

Every name and statement here is invented: the repository is public, and real runs' casts and words stay out.
"""

import json

import pytest

from matrix_studio import stance
from tests.support import TEST_OWNER
from tests.test_orchestration import CAST

NAMES = ["Deputy Chair Voss", "Ada", "Bo"]


def test_dissenters_hold_shifts_support_the_rest_are_unstated():
    got = stance.stances(NAMES, {"dissenters": [{"speaker": "Ada", "position": "x"}]}, ["Bo"])
    assert got == {"Deputy Chair Voss": "unstated", "Ada": "holding", "Bo": "support"}


def test_a_dissenter_who_also_shifted_is_still_holding():
    # They moved at some point and still ended objecting: the end state is what the stance is.
    assert stance.stances(NAMES, {"dissenters": [{"speaker": "Bo"}]}, ["Bo"])["Bo"] == "holding"


def test_a_shortened_name_matches_the_one_cast_member_it_names():
    got = stance.stances(NAMES, {"dissenters": [{"speaker": "Voss"}]}, [])
    assert got["Deputy Chair Voss"] == "holding"


def test_an_ambiguous_or_unknown_name_matches_nobody():
    names = ["Ada Lane", "Ada Moss", "Bo"]
    got = stance.stances(names, {"dissenters": [{"speaker": "Ada"}, {"speaker": "Someone Else"}]}, [])
    assert set(got.values()) == {"unstated"}


def test_no_dissenter_list_means_no_stance():
    # Field switched off, or an unparsed reply: "not a dissenter" would be a guess.
    assert stance.stances(NAMES, {"overview": "x"}, ["Bo"]) is None
    assert stance.stances(NAMES, None, ["Bo"]) is None


def test_an_empty_dissenter_list_is_an_answer():
    assert stance.stances(NAMES, {"dissenters": []}, ["Ada"]) == {
        "Deputy Chair Voss": "unstated", "Ada": "support", "Bo": "unstated",
    }


def test_shifted_speakers_reads_the_event_log_once_each():
    events = [
        {"event_type": "agent.response", "payload": {"speaker": "Ada"}},
        {"event_type": "position.shift", "payload": {"speaker": "Bo"}},
        {"event_type": "position.shift", "payload": '{"speaker": "Ada"}'},
        {"event_type": "position.shift", "payload": {"speaker": "Bo"}},
    ]
    assert stance.shifted_speakers(events) == ["Bo", "Ada"]


async def test_generating_a_summary_stores_the_stances(db, monkeypatch):
    from matrix_studio import service

    await db.create_run(run_id="st1", topic="t", cast=CAST, name="st1", config={}, owner_sub=TEST_OWNER)
    await db.append_event("st1", 1, 1, "position.shift", {"speaker": "Bo"}, "Bo")
    replies = [
        {"payload": {"dissenters": [{"speaker": "Ada", "position": "no"}]}, "parsed": True},
        # A regenerate whose reply could not be parsed: the previous stances must not stand.
        {"payload": {"overview": "free text"}, "parsed": False},
    ]

    async def fake_summary(**kw):
        return {**replies.pop(0), "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0, "instructions": None}

    monkeypatch.setattr(service.analysis, "generate_summary", fake_summary)
    await service.generate_and_store_summary(db, await db.get_run("st1"))

    assert json.loads((await db.get_run("st1"))["stance_json"]) == {"Ada": "holding", "Bo": "support"}
    assert json.loads((await db.get_run("st1"))["stance_basis_json"])["personas"]["Ada"]["source"] == "summary"
    await service.generate_and_store_summary(db, await db.get_run("st1"))
    assert json.loads((await db.get_run("st1"))["stance_json"] or "null") is None
    assert json.loads((await db.get_run("st1"))["stance_basis_json"] or "null") is None, "the reasons go too"


async def test_a_stance_for_a_missing_run_is_refused_not_invented(db):
    assert await db.set_run_stance("ghost", {"Ada": "holding"}) is False
    assert await db.set_run_stance("ghost", {"Ada": "support"}, basis={"personas": {}, "classifier": None}) is False
    assert await db.get_run("ghost") is None


# --------------------------------------------------------------------------- #
# The closing statement (decided 2026-10-01): classified, quoted, checked
# --------------------------------------------------------------------------- #

CAST3 = [{"name": n, "persona": "p", "goals": []} for n in ("Ada", "Bo", "Cy")]
SAID = {
    "Ada": "I can live with the pilot as drafted. I’m signing the plan as written, and I want it on record.",
    "Bo": "The rota works for me only if the night desk gets a second person before launch. Until then I "
          "sign with that condition attached.",
    "Cy": "I still think the whole thing is backwards. I will not sign this plan.",
}


def _closing(run_id, said, turn=3):
    """The events a closing round leaves: one flagged `agent.response` per persona who spoke."""
    return [(run_id, turn, 100 + i, "agent.response", {"speaker": n, "message": m, "closing": True}, n)
            for i, (n, m) in enumerate(said.items())]


def _reply(*rows):
    return json.dumps({"personas": [{"name": n, "quote": q, "class": c} for n, q, c in rows]})


def _llm(classifier_reply, summary=None, calls=None, cost=0.004):
    """An `analysis._acompletion` answering the stance prompt with ``classifier_reply`` (a string, or an
    exception to raise) and the summary prompt with ``summary``."""
    summary = summary if summary is not None else {
        "dissenters": [{"speaker": "Cy", "position": "rejects it"}], "overview": "x"}

    async def fake(messages, model=None, temperature=0.4, max_tokens=None):
        is_stance = "closing statements" in messages[0]["content"]
        if calls is not None:
            calls.append({"stance": is_stance, "model": model, "temperature": temperature,
                          "user": messages[-1]["content"]})
        if is_stance:
            if isinstance(classifier_reply, Exception):
                raise classifier_reply
            return {"content": classifier_reply, "tokens_in": 300, "tokens_out": 90, "cost_usd": cost,
                    "finish_reason": "stop"}
        return {"content": json.dumps(summary), "tokens_in": 1000, "tokens_out": 200, "cost_usd": 0.02}

    return fake


async def _summarise(db, monkeypatch, run_id, events, llm, cast=CAST3):
    """Generate a run's summary through the real service path; return its (stance, basis) as stored."""
    from matrix_studio import service

    await db.create_run(run_id=run_id, topic="a rota", cast=cast, name=run_id, config={}, owner_sub=TEST_OWNER)
    for e in events:
        await db.append_event(*e)
    monkeypatch.setattr(service.analysis, "_acompletion", llm)
    await service.generate_and_store_summary(db, await db.get_run(run_id))
    row = await db.get_run(run_id)
    return json.loads(row["stance_json"] or "null"), json.loads(row["stance_basis_json"] or "null")


async def test_closing_statements_decide_the_stance_and_carry_their_quotes(db, monkeypatch):
    from matrix_studio.models import LOW_VARIANCE_MODEL

    calls = []
    reply = _reply(("Ada", "I’m signing the plan as written", "accepts"),
                   ("Bo", "Until then I sign with that condition attached.", "accepts_with_conditions"),
                   ("Cy", "I will not sign this plan.", "rejects"))
    state, basis = await _summarise(db, monkeypatch, "cl1", _closing("cl1", SAID), _llm(reply, calls=calls))
    assert state == {"Ada": "support", "Bo": "conditional", "Cy": "holding"}
    assert basis["personas"]["Ada"] == {"stance": "support", "source": "closing", "class": "accepts",
                                        "quote": "I’m signing the plan as written"}
    assert basis["personas"]["Bo"]["class"] == "accepts_with_conditions"
    # Stored as the statement has it: the model's trailing full stop is not part of the match.
    assert basis["personas"]["Cy"]["quote"] == "I will not sign this plan"
    # ONE call for the room, at temperature 0, on the stance role's model, with every statement in it.
    stance_calls = [c for c in calls if c["stance"]]
    assert len(stance_calls) == 1
    assert stance_calls[0]["temperature"] == 0.0
    assert stance_calls[0]["model"] == LOW_VARIANCE_MODEL
    assert all(text in stance_calls[0]["user"] for text in SAID.values())
    assert basis["classifier"]["cost_usd"] == pytest.approx(0.004) and basis["classifier"]["error"] is None


async def test_a_quote_that_is_not_in_the_statement_is_unclear_and_falls_back(db, monkeypatch):
    # The model claims Ada accepts, and backs it with words she never said: the claim does not count.
    reply = _reply(("Ada", "I fully endorse every part of this plan", "accepts"),
                   ("Bo", "Until then I sign with that condition attached.", "accepts_with_conditions"),
                   ("Cy", "I will not sign this plan.", "rejects"))
    events = _closing("cl2", SAID) + [("cl2", 2, 50, "position.shift",
                                       {"speaker": "Ada", "sentences": ["Your numbers moved me."]}, "Ada")]
    state, basis = await _summarise(db, monkeypatch, "cl2", events, _llm(reply))
    # Today's rule decides for her: not a dissenter and a shift flagged, so support — from the summary.
    assert state["Ada"] == "support"
    assert basis["personas"]["Ada"] == {
        "stance": "support", "source": "summary", "class": "unclear", "quote": "Your numbers moved me.",
        "fallback": "unverified_quote", "claimed": "accepts"}
    assert state["Bo"] == "conditional", "the others' verdicts still stand"


async def test_no_closing_round_is_todays_rule_exactly_and_makes_no_call(db, monkeypatch):
    calls = []
    events = [("nc1", 1, 1, "agent.response", {"speaker": "Ada", "message": "I'm signing it."}, "Ada"),
              ("nc1", 1, 2, "position.shift", {"speaker": "Bo", "sentences": ["I concede the point."]}, "Bo")]
    state, basis = await _summarise(db, monkeypatch, "nc1", events, _llm("unused", calls=calls))
    assert [c for c in calls if c["stance"]] == [], "no closing statements, no classifier call"
    assert state == stance.stances(["Ada", "Bo", "Cy"], {"dissenters": [{"speaker": "Cy"}]}, ["Bo"])
    assert state == {"Ada": "unstated", "Bo": "support", "Cy": "holding"}
    assert {b["source"] for b in basis["personas"].values()} == {"summary"}
    assert {b["fallback"] for b in basis["personas"].values()} == {"no_closing_round"}
    assert basis["personas"]["Cy"]["quote"] == "rejects it", "the summary's account of the objection"
    assert basis["personas"]["Bo"]["quote"] == "I concede the point."
    assert basis["classifier"] is None


async def test_a_persona_with_no_closing_statement_falls_back(db, monkeypatch):
    # Bo passed in the closing round, so there is no statement of his to read.
    said = {"Ada": SAID["Ada"], "Cy": SAID["Cy"]}
    reply = _reply(("Ada", "I’m signing the plan as written", "accepts"),
                   ("Cy", "I will not sign this plan.", "rejects"))
    state, basis = await _summarise(db, monkeypatch, "cl3", _closing("cl3", said), _llm(reply))
    assert state == {"Ada": "support", "Bo": "unstated", "Cy": "holding"}
    assert basis["personas"]["Bo"] == {"stance": "unstated", "source": "summary", "class": None, "quote": None,
                                       "fallback": "no_statement"}


@pytest.mark.parametrize("run_id,failure", [
    ("cf1", RuntimeError("the model is down")),
    ("cf2", "not json at all"),
    ("cf3", '{"verdicts": []}'),
])
async def test_a_failing_classifier_leaves_todays_rule_and_the_summary(db, monkeypatch, caplog, run_id, failure):
    with caplog.at_level("WARNING"):
        state, basis = await _summarise(db, monkeypatch, run_id, _closing(run_id, SAID), _llm(failure))
    assert state == {"Ada": "unstated", "Bo": "unstated", "Cy": "holding"}, "the summary rule, unchanged"
    assert {b["fallback"] for b in basis["personas"].values()} == {"classifier_failed"}
    assert basis["classifier"]["error"]
    assert "Closing-statement classifier" in caplog.text
    summaries = await db.get_summaries(run_id)
    assert summaries and summaries[0]["payload"]["overview"] == "x", "the summary is stored regardless"


async def test_the_classifier_is_charged_with_the_summary(db, monkeypatch):
    charged = []

    async def spend(cost, owner_sub=None):
        charged.append((cost, owner_sub))

    monkeypatch.setattr(db, "add_user_spend", spend)
    reply = _reply(("Ada", "I’m signing the plan as written", "accepts"))
    await _summarise(db, monkeypatch, "cc1", _closing("cc1", SAID), _llm(reply, cost=0.003))
    assert charged == [(pytest.approx(0.02 + 0.003), TEST_OWNER)]


def test_a_quote_is_checked_word_for_word_with_case_quotes_and_spacing_aside():
    said = "First point.  I’m   signing the “final” plan as written — nothing more."
    # Straight quotes, collapsed spaces, other case, a trailing full stop and wrapping quotes the model
    # added: still the same words, and what comes back is the statement's own text.
    assert stance.verified_quote("\"i'm signing the \"final\" plan as written.\"", said) == \
        "I’m   signing the “final” plan as written"
    assert stance.verified_quote("I'm signing the final plan as written", said) is None, "dropped quotes"
    assert stance.verified_quote("I'm signing the plan as written", said) is None, "a word left out"
    assert stance.verified_quote("I’m signing … as written", said) is None, "an ellipsis"
    assert stance.verified_quote("First point", said) is None, "too short to say anything"
    assert stance.verified_quote(None, said) is None


def test_closing_statements_are_only_the_flagged_responses():
    events = [
        {"event_type": "agent.response", "payload": {"speaker": "Ada", "message": "mid-argument"}},
        {"event_type": "agent.response", "payload": '{"speaker": "Ada", "message": " final ", "closing": true}'},
        {"event_type": "agent.passed", "payload": {"speaker": "Bo", "closing": True}},
        {"event_type": "speaker.selected", "payload": {"speaker": "Cy", "closing": True}},
    ]
    assert stance.closing_statements(events) == {"Ada": "final"}


def test_the_reply_is_read_strictly():
    obj = {"personas": [
        {"name": "ada", "quote": "I can live with the pilot as drafted.", "class": "Accepts"},  # case-blind
        {"name": "Bo", "quote": "Until then I sign with that condition attached.", "class": "mostly agrees"},
        # Cy skipped.
    ]}
    got = stance.read_verdicts(obj, SAID)
    assert got["Ada"] == {"class": "accepts", "quote": "I can live with the pilot as drafted"}
    assert got["Bo"] == {"class": "unclear", "quote": None, "fallback": "no_verdict"}, "an invented class"
    assert got["Cy"] == {"class": "unclear", "quote": None, "fallback": "no_verdict"}, "a persona skipped"
    # A map keyed by name instead of a list is the same answer.
    as_map = {"personas": {"Ada": {"quote": "I will not sign", "class": "rejects"}}}
    assert stance.read_verdicts(as_map, {"Ada": "I will not sign this."})["Ada"]["class"] == "rejects"


def test_an_unclear_verdict_keeps_its_quote_but_does_not_decide():
    said = {"Ada": SAID["Ada"]}
    verdicts = stance.read_verdicts(
        {"personas": [{"name": "Ada", "quote": "I can live with the pilot", "class": "unclear"}]}, said)
    state, basis = stance.resolve(["Ada"], {"dissenters": []}, [], said, verdicts)
    assert state == {"Ada": "unstated"}
    assert basis["Ada"]["fallback"] == "unclear" and basis["Ada"]["class"] == "unclear"
    assert basis["Ada"]["quote"] is None, "a summary-sourced unstated has nothing to show"


def test_without_a_dissenter_list_the_closing_statements_still_stand():
    said = {"Ada": SAID["Ada"]}
    verdicts = {"Ada": {"class": "accepts", "quote": "I can live with the pilot as drafted"}}
    state, _ = stance.resolve(["Ada", "Bo"], None, [], said, verdicts)
    # Bo has no statement, and the summary cannot say whether he dissented: unstated, not a guess.
    assert state == {"Ada": "support", "Bo": "unstated"}
    # Nobody settled by a statement and no dissenter list: no stance at all, exactly as before.
    assert stance.resolve(["Ada", "Bo"], None, [], said, None) == (None, None)
    assert stance.resolve(["Ada", "Bo"], None, [], {}, None) == (None, None)
