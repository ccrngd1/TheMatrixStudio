# SPDX-License-Identifier: Apache-2.0
"""Where each persona ended a run (docs/MOBILE-UI.md §6.1, option 1): dissenters hold, a flagged shift
that is not a dissenter supports, everyone else is unstated — and with no dissenter list, nobody has a
stance at all."""

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
    import json

    assert json.loads((await db.get_run("st1"))["stance_json"]) == {"Ada": "holding", "Bo": "support"}
    await service.generate_and_store_summary(db, await db.get_run("st1"))
    assert json.loads((await db.get_run("st1"))["stance_json"] or "null") is None


async def test_a_stance_for_a_missing_run_is_refused_not_invented(db):
    assert await db.set_run_stance("ghost", {"Ada": "holding"}) is False
    assert await db.get_run("ghost") is None
