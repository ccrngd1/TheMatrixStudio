# SPDX-License-Identifier: Apache-2.0
"""
A persona never carries a real, well-known person's name — `matrix_studio/real_names.py`.

The false-positive side is tested as carefully as the true-positive side, because both failures are
real: a missed "Jeff Bezos" puts a real person's name on simulated speech, and a renamed "Mike
Johnson" tells somebody they typed a famous name when they typed an ordinary one.

Every model call here is a fake. `tests/conftest.py` makes every name "not famous" by default; the
tests of the model path install their own reply.
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from matrix_studio import real_names as rn
from matrix_studio.models import LOW_VARIANCE_MODEL
from matrix_studio.real_names import _complete as REAL_COMPLETE  # captured before conftest patches it
from tests.support import TEST_OWNER

NOT_FAMOUS = {"famous": False, "confidence": "high", "who": "", "parody": ""}


def fake_model(replies=None):
    """A stand-in for `real_names._complete`: name -> reply dict, raw string, or exception."""
    replies = replies or {}
    calls = []

    async def _complete(messages, model):
        name = json.loads(messages[-1]["content"].split("Name: ", 1)[1])
        calls.append((name, model))
        reply = replies.get(name, NOT_FAMOUS)
        if isinstance(reply, Exception):
            raise reply
        return {"content": reply if isinstance(reply, str) else json.dumps(reply),
                "cost_usd": 0.0004, "tokens_in": 300, "tokens_out": 40}

    _complete.calls = calls
    return _complete


@pytest.fixture
def model(monkeypatch):
    """Install a fake model; returns a function to set its replies."""
    def install(replies=None):
        fake = fake_model(replies)
        monkeypatch.setattr(rn, "_complete", fake)
        return fake
    return install


def famous(parody, who="a famous person"):
    return {"famous": True, "confidence": "high", "who": who, "parody": parody}


# --------------------------------------------------------------------------- #
# The curated list
# --------------------------------------------------------------------------- #


class TestTheCuratedList:
    def test_it_covers_the_obvious_tech_and_ai_leaders(self):
        names = {f.name for f in rn.figures()}
        for who in ("Jeff Bezos", "Werner Vogels", "Andy Jassy", "Elon Musk", "Sam Altman", "Dario Amodei",
                    "Demis Hassabis", "Sundar Pichai", "Satya Nadella", "Mark Zuckerberg", "Jensen Huang",
                    "Tim Cook", "Bill Gates", "Steve Jobs", "Geoffrey Hinton", "Yann LeCun", "Lisa Su"):
            assert who in names, who
        assert len(names) >= 200

    def test_heads_of_state_and_widely_known_figures_are_there_too(self):
        names = {f.name for f in rn.figures()}
        for who in ("Donald Trump", "Xi Jinping", "Narendra Modi", "Keir Starmer", "Emmanuel Macron",
                    "Vladimir Putin", "Volodymyr Zelenskyy", "Taylor Swift", "Albert Einstein"):
            assert who in names, who

    def test_every_spelling_is_a_full_name(self):
        """A single word must never match, so the list may not contain one."""
        for f in rn.figures():
            for spelling in f.spellings:
                assert len(rn.normalise(spelling).split()) >= 2, (f.name, spelling)

    def test_no_spelling_names_two_people(self):
        owner = {}
        for f in rn.figures():
            for spelling in f.spellings:
                for form in rn._forms(spelling):
                    if len(form.split()) >= 2:
                        assert owner.setdefault(form, f.name) == f.name, (form, owner[form], f.name)

    def test_every_parody_passes_the_rules_a_model_suggestion_must_pass(self):
        """Hand-written or not, a parody is spelled differently in every word, is not too close, is not
        unkind and is not itself on the list."""
        for f in rn.figures():
            assert rn.acceptable_parody(f.name, f.parody), (f.name, f.parody)

    def test_no_parody_is_another_listed_person(self):
        for f in rn.figures():
            assert rn.lookup(f.parody) is None, (f.name, f.parody)

    def test_the_list_ships_with_the_installed_package(self):
        """The images run `pip install .`, which copies only declared package data. Without this the
        deployed check would have no list and every create would fail on a missing file."""
        import tomllib
        from pathlib import Path

        pyproject = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
        assert "public_figures.json" in pyproject["tool"]["setuptools"]["package-data"]["matrix_studio"]
        assert rn.DATA_FILE.name == "public_figures.json" and rn.DATA_FILE.parent.name == "matrix_studio"


# --------------------------------------------------------------------------- #
# Matching: the true-positive side
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("typed, who", [
    ("Jeff Bezos", "Jeff Bezos"),
    ("jeff bezos", "Jeff Bezos"),
    ("JEFF BEZOS", "Jeff Bezos"),
    ("  Jeff   Bezos ", "Jeff Bezos"),
    ("Jeffrey Bezos", "Jeff Bezos"),
    ("Jeffrey P. Bezos", "Jeff Bezos"),
    ("Dr. Jeff Bezos", "Jeff Bezos"),
    ("Jeff Bezos (founder)", "Jeff Bezos"),
    ("Jeff Bezos, CEO", "Jeff Bezos"),
    ("Werner Vogels", "Werner Vogels"),
    ("Tobi Lütke", "Tobi Lütke"),
    ("Tobi Lutke", "Tobi Lütke"),
    ("TOBI LÜTKE", "Tobi Lütke"),
    ("Recep Erdogan", "Recep Tayyip Erdoğan"),
    ("Viktor Orban", "Viktor Orbán"),
    ("Antonio Guterres", "António Guterres"),
    ("J.D. Vance", "JD Vance"),
    ("J. D. Vance", "JD Vance"),
    ("Kim Jong-un", "Kim Jong Un"),
    ("Fei Fei Li", "Fei-Fei Li"),
    ("Tim Berners Lee", "Tim Berners-Lee"),
    ("Martin Luther King, Jr.", "Martin Luther King Jr."),
    ("Elon Musk Jr", "Elon Musk"),
    ("Sir Keir Starmer", "Keir Starmer"),
    ("King Charles III", "King Charles III"),
    ("President Donald J. Trump", "Donald Trump"),
])
def test_a_listed_name_matches_whatever_its_case_accents_and_dress(typed, who):
    fig = rn.lookup(typed)
    assert fig is not None and fig.name == who, typed


# --------------------------------------------------------------------------- #
# Matching: the false-positive side
# --------------------------------------------------------------------------- #


SINGLE_NAMES = ["Ruth", "Jeff", "Elon", "Bezos", "Musk", "Sam", "Satya", "Oprah", "Dr. Morgan", "Ruth (CFO)",
                "The Skeptic", "Charles", "King", ""]
ORDINARY = ["Mike Johnson", "Sarah Chen", "Maria Garcia", "John Smith", "David Miller", "Priya Okonjo",
            "Ines Kowalski", "Dev Marchetti", "Jordan Reeves", "Lisa Morales", "Jeff Bezier", "Elon Muskrat",
            "Tim Cooke-Ramsey", "Mark Zucker", "Samuel Alton", "Billy Gate"]


@pytest.mark.parametrize("name", SINGLE_NAMES + ORDINARY)
def test_an_unlisted_name_does_not_match_the_list(name):
    assert rn.lookup(name) is None, name


@pytest.mark.parametrize("name", SINGLE_NAMES)
async def test_a_single_name_never_reaches_the_model(name, model):
    fake = model({name: famous("Anything")})
    verdict = await rn.check_name(name)
    assert not verdict.renamed
    assert fake.calls == [], "a single first name or surname cost a model call"


@pytest.mark.parametrize("name", ["Analyst 2", "Persona 3", "x" * 100,
                                  "the head of the regional sales team for the northern division"])
def test_a_label_is_not_a_full_name(name):
    assert not rn.looks_like_full_name(name)


@pytest.mark.parametrize("name", ORDINARY)
async def test_an_ordinary_name_the_model_calls_ordinary_stays(name, model):
    fake = model()
    verdict = await rn.check_name(name)
    assert not verdict.renamed and verdict.replacement == ""
    assert [n for n, _m in fake.calls] == [name]


@pytest.mark.parametrize("name", ["Stephen King", "Larry King", "Carole King"])
async def test_a_title_word_as_a_surname_still_reaches_the_model(name, model):
    """"King" is a style before a name and a surname after one. Dropping it everywhere would make a famous
    full name look like a lone first name, which the check never asks about."""
    fake = model({name: famous("Stevven Kyng")})
    assert rn.looks_like_full_name(name)
    assert (await rn.check_name(name)).renamed
    assert fake.calls[0][0] == name


async def test_medium_confidence_is_not_enough(model):
    """Where an ordinary name a minor public figure happens to share lands, and the reason for the bar."""
    model({"Mike Johnson": {"famous": True, "confidence": "medium", "who": "a politician",
                            "parody": "Myke Jonsun"}})
    assert not (await rn.check_name("Mike Johnson")).renamed


# --------------------------------------------------------------------------- #
# The model path
# --------------------------------------------------------------------------- #


class TestTheModelPath:
    async def test_a_famous_name_takes_the_models_parody(self, model):
        fake = model({"Quentin Blaywater": famous("Kwentin Blaywotter", "a famous novelist")})
        meter = rn.Meter()
        v = await rn.check_name("Quentin Blaywater", meter=meter)
        assert v.renamed and v.replacement == "Kwentin Blaywotter" and v.source == "model"
        assert v.who == "a famous novelist"
        # The parody is re-asked too: it must not itself be somebody.
        assert [n for n, _m in fake.calls] == ["Quentin Blaywater", "Kwentin Blaywotter"]
        assert meter.calls == 2 and meter.cost_usd == pytest.approx(0.0008)

    async def test_the_default_model_is_haiku(self, model):
        fake = model()
        await rn.check_name("Ines Kowalski")
        assert fake.calls[0][1] == LOW_VARIANCE_MODEL

    async def test_a_verdict_is_cached_in_process(self, model):
        fake = model({"Quentin Blaywater": famous("Kwentin Blaywotter")})
        await rn.check_name("Quentin Blaywater")
        await rn.check_name("quentin  BLAYWATER")
        assert [n for n, _m in fake.calls].count("Quentin Blaywater") == 1

    async def test_a_malformed_reply_is_not_famous_and_is_logged(self, model, caplog):
        fake = model({"Quentin Blaywater": "I think this might be a novelist? {not json"})
        with caplog.at_level(logging.WARNING, logger="matrix_studio.real_names"):
            v = await rn.check_name("Quentin Blaywater")
        assert not v.renamed
        assert "unreadable reply" in caplog.text
        # Cached: at temperature 0 the same question gets the same unreadable answer.
        await rn.check_name("Quentin Blaywater")
        assert len(fake.calls) == 1

    @pytest.mark.parametrize("reply", [
        {"famous": "yes", "confidence": "high", "who": "", "parody": ""},
        {"confidence": "high"},
        ["famous", True],
        "",
    ])
    async def test_any_unreadable_shape_is_not_famous(self, reply, model):
        model({"Quentin Blaywater": reply if isinstance(reply, str) else json.dumps(reply)})
        assert not (await rn.check_name("Quentin Blaywater")).renamed

    async def test_a_failed_call_is_not_famous_logged_and_not_cached(self, model, caplog):
        fake = model({"Quentin Blaywater": RuntimeError("throttled")})
        with caplog.at_level(logging.WARNING, logger="matrix_studio.real_names"):
            assert not (await rn.check_name("Quentin Blaywater")).renamed
        assert "throttled" in caplog.text
        await rn.check_name("Quentin Blaywater")
        assert len(fake.calls) == 2, "a transient failure was remembered as a verdict"

    @pytest.mark.parametrize("bad", [
        "Quentin Blaywotter",   # keeps the real first name
        "Quentyn Blaywater",    # keeps the real surname
        "Quentin Blaywater",    # the real name
        "Kwentin Bozowater",    # unkind
        "Shag Blaywotter",      # innuendo: the live check's first run suggested "Shag Rukk Kahn"
        "Jeff Bezos",           # a listed real person
        "K",                    # too short to stand in for a full name
        "Kwentin Blaywotter 3", # not a name
        "",
    ])
    async def test_a_bad_parody_is_replaced_by_the_fallback(self, bad, model):
        model({"Quentin Blaywater": famous(bad)})
        v = await rn.check_name("Quentin Blaywater")
        assert v.renamed and v.replacement != bad
        assert rn.acceptable_parody("Quentin Blaywater", v.replacement), v.replacement

    async def test_a_parody_that_is_itself_famous_is_not_used(self, model):
        model({"Quentin Blaywater": famous("Kwentin Blaywotter"),
               "Kwentin Blaywotter": famous("Something Else")})
        v = await rn.check_name("Quentin Blaywater")
        assert v.renamed and v.replacement != "Kwentin Blaywotter"

    async def test_the_real_call_uses_haiku_at_temperature_zero_with_a_schema(self, monkeypatch):
        """The seam itself, against a fake provider: the role's model, temperature 0, a JSON schema."""
        monkeypatch.setattr(rn, "_complete", REAL_COMPLETE)
        seen = {}

        async def acompletion(**kwargs):
            seen.update(kwargs)
            response = MagicMock()
            response.choices = [MagicMock(message=MagicMock(content=json.dumps(NOT_FAMOUS)))]
            response.usage = MagicMock(prompt_tokens=310, completion_tokens=30)
            response._hidden_params = {"response_cost": 0.00046}
            return response

        with patch("matrix_studio.real_names.litellm.acompletion", side_effect=acompletion):
            meter = rn.Meter()
            await rn.check_name("Ines Kowalski", meter=meter)
        assert seen["model"] == LOW_VARIANCE_MODEL
        assert seen["temperature"] == 0.0
        assert seen["response_format"]["type"] == "json_schema"
        assert set(seen["response_format"]["json_schema"]["schema"]["required"]) == {
            "famous", "confidence", "who", "parody"}
        assert meter.cost_usd == pytest.approx(0.00046)

    async def test_with_the_model_off_only_the_list_applies(self, model):
        fake = model({"Quentin Blaywater": famous("Kwentin Blaywotter")})
        assert not (await rn.check_name("Quentin Blaywater", use_model=False)).renamed
        assert (await rn.check_name("Jeff Bezos", use_model=False)).replacement == "Geoff Beesoh"
        assert fake.calls == []


# --------------------------------------------------------------------------- #
# Replacements are never real
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("real", ["Quentin Blaywater", "Sam Altman", "Luiz Inácio Lula da Silva",
                                  "Mohammed bin Salman", "Tim Berners-Lee", "Ada Lovelace", "Kim Jong Un"])
def test_the_fallback_is_deterministic_and_acceptable(real):
    once = rn.sound_alike(real)
    assert once == rn.sound_alike(real)
    assert rn.acceptable_parody(real, once), once
    assert rn.lookup(once) is None


def test_the_listed_replacement_is_the_hand_written_one():
    assert rn.listed_name("Jeff Bezos") == "Geoff Beesoh"
    assert rn.listed_name("Werner Vogels") == "Verner Fogles"
    assert rn.listed_name("Ruth") == "Ruth"


# --------------------------------------------------------------------------- #
# Applying it to a request
# --------------------------------------------------------------------------- #


def _request():
    return {
        "topic": "Should Jeff Bezos sign off on the plan?",
        "cast": [
            {"name": "Jeff Bezos", "persona": "You are Jeff Bezos, founder of a bookshop. Jeffrey Bezos speaks "
                                              "plainly.", "goals": ["Make Jeff Bezos proud"],
             "structured": {"role": "Jeff Bezos, founder",
                            "viewpoints": [{"position": "Bezos-style day one thinking", "firmness": "firm"}]},
             "document_texts": [{"title": "letter", "text": "Jeff Bezos wrote this in 1997."}]},
            {"name": "Ruth", "persona": "You report to Jeff Bezos and say so.", "goals": []},
        ],
        "config": {
            "experts": [{"name": "Werner Vogels", "expertise": "what Werner Vogels knows about systems"}],
            "assumptions": [{"statement": "Jeff Bezos has the final say", "basis": ""}],
            "injections": [{"after_turn": 1, "speaker": "Jeff Bezos", "content": "Jeff Bezos here, carry on."},
                           {"after_turn": 1, "speaker": "Board", "content": "Thanks, Jeff Bezos."}],
        },
    }


class TestScreenRequest:
    async def test_names_and_every_mention_are_replaced_consistently(self):
        request = _request()
        before = json.dumps(request, sort_keys=True)
        out, screening = await rn.screen_request(request)
        assert json.dumps(request, sort_keys=True) == before, "the input was mutated"

        jeff, ruth = out["cast"]
        assert jeff["name"] == "Geoff Beesoh"
        assert jeff["persona"] == "You are Geoff Beesoh, founder of a bookshop. Geoff Beesoh speaks plainly."
        assert jeff["goals"] == ["Make Geoff Beesoh proud"]
        assert jeff["structured"]["role"] == "Geoff Beesoh, founder"
        assert ruth["name"] == "Ruth" and ruth["persona"] == "You report to Geoff Beesoh and say so."
        assert out["topic"] == "Should Geoff Beesoh sign off on the plan?"
        expert = out["config"]["experts"][0]
        assert expert["name"] == "Verner Fogles"
        assert expert["expertise"] == "what Verner Fogles knows about systems"
        assert out["config"]["assumptions"][0]["statement"] == "Geoff Beesoh has the final say"
        assert out["config"]["injections"][0] == {
            "after_turn": 1, "speaker": "Geoff Beesoh", "content": "Geoff Beesoh here, carry on."}
        assert out["config"]["injections"][1]["speaker"] == "Board"
        assert screening.as_list() == [
            {"from": "Jeff Bezos", "to": "Geoff Beesoh", "reason": "real public figure", "source": "list",
             "role": "persona"},
            {"from": "Werner Vogels", "to": "Verner Fogles", "reason": "real public figure", "source": "list",
             "role": "consultant"},
        ]

    async def test_documents_are_evidence_and_are_not_rewritten(self):
        out, _ = await rn.screen_request(_request())
        assert out["cast"][0]["document_texts"][0]["text"] == "Jeff Bezos wrote this in 1997."

    async def test_a_surname_alone_is_left_as_written(self):
        """Only full names are rewritten: "Bezos-style" could be a reference rather than the persona."""
        out, _ = await rn.screen_request(_request())
        assert out["cast"][0]["structured"]["viewpoints"][0]["position"] == "Bezos-style day one thinking"

    async def test_a_clean_request_is_unchanged(self):
        request = {"topic": "t", "cast": [{"name": "Ruth", "persona": "p"}, {"name": "Ines Kowalski",
                                                                               "persona": "q"}]}
        out, screening = await rn.screen_request(request)
        assert out == request and out is not request
        assert screening.renamed == [] and screening.cost_usd == 0.0

    async def test_a_parody_never_lands_on_somebody_already_in_the_cast(self):
        request = {"topic": "t", "cast": [{"name": "Geoff Beesoh", "persona": "p"},
                                          {"name": "Jeff Bezos", "persona": "q"}]}
        out, screening = await rn.screen_request(request)
        names = [c["name"] for c in out["cast"]]
        assert names[0] == "Geoff Beesoh"
        assert names[1] not in ("Geoff Beesoh", "Jeff Bezos") and len(set(names)) == 2
        assert screening.renamed[0].replacement == names[1]

    async def test_the_model_path_applies_to_a_request(self, model):
        model({"Quentin Blaywater": famous("Kwentin Blaywotter")})
        out, screening = await rn.screen_request(
            {"topic": "t", "cast": [{"name": "Quentin Blaywater", "persona": "You are Quentin Blaywater."}]})
        assert out["cast"][0] == {"name": "Kwentin Blaywotter", "persona": "You are Kwentin Blaywotter."}
        assert screening.renamed[0].source == "model"
        assert screening.cost_usd == pytest.approx(0.0008)


# --------------------------------------------------------------------------- #
# Every entry point
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(aws_backend, monkeypatch):
    """The app, as `TEST_OWNER` in single-user mode, with naming and the engine faked."""
    from matrix_studio.api import identity
    from matrix_studio.api.app import create_app
    from tests.test_api import make_fake_run

    monkeypatch.setenv("AUTH_MODE", "single-user")
    monkeypatch.setattr(identity, "LOCAL_USER_SUB", TEST_OWNER, raising=False)

    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        return {"name": "named-run", "description": "d", "slug": "named-run", "source": "llm"}

    monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.api.manager.run_simulation", make_fake_run(turns=1))
    with TestClient(create_app()) as c:
        yield c


BODY = {
    "topic": "Is Jeff Bezos right about this?",
    "cast": [{"name": "Jeff Bezos", "persona": "You are Jeff Bezos.", "goals": ["g"]},
             {"name": "Ruth", "persona": "an engineer", "goals": ["g"]}],
    "config": {"max_messages": 1, "generate_avatars": False, "retrieval": {"enabled": True},
               "experts": [{"name": "Werner Vogels", "expertise": "systems"}]},
}

def _wait_done(client, run_id, tries=250):
    """Locally the run row is written by the background task, after the 201: wait for it to finish."""
    import time

    for _ in range(tries):
        r = client.get(f"/api/runs/{run_id}")
        if r.status_code == 200 and r.json()["status"] in ("complete", "failed"):
            return r.json()
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not finish")


RENAMED_JEFF = {"from": "Jeff Bezos", "to": "Geoff Beesoh", "reason": "real public figure", "source": "list",
                "role": "persona"}


class TestEntryPoints:
    async def test_run_creation_renames_and_reports(self, client, db):
        res = client.post("/api/runs", json=BODY)
        assert res.status_code == 201, res.text
        body = res.json()
        assert RENAMED_JEFF in body["renamed"]
        assert {"from": "Werner Vogels", "to": "Verner Fogles", "reason": "real public figure",
                "source": "list", "role": "consultant"} in body["renamed"]
        _wait_done(client, body["run_id"])
        run = await db.get_run(body["run_id"])
        cast = json.loads(run["cast_json"])
        assert [c["name"] for c in cast] == ["Geoff Beesoh", "Ruth"]
        assert cast[0]["persona"] == "You are Geoff Beesoh."
        assert run["topic"] == "Is Geoff Beesoh right about this?"
        assert json.loads(run["config_json"])["experts"][0]["name"] == "Verner Fogles"

    async def test_a_clean_run_reports_nothing(self, client):
        res = client.post("/api/runs", json={**BODY, "topic": "t", "cast": [BODY["cast"][1]],
                                             "config": {"max_messages": 1}})
        assert res.status_code == 201 and res.json()["renamed"] == []
        _wait_done(client, res.json()["run_id"])

    async def test_the_model_check_is_charged_to_the_owners_month(self, client, db, model):
        model({"Quentin Blaywater": famous("Kwentin Blaywotter")})
        res = client.post("/api/runs", json={"topic": "t", "config": {"max_messages": 1},
                                             "cast": [{"name": "Quentin Blaywater", "persona": "p"}]})
        assert res.status_code == 201
        assert res.json()["renamed"][0]["to"] == "Kwentin Blaywotter"
        # Charged before the 201, so no wait is needed for the spend — only so the run does not outlive the test.
        assert await db.user_spend() == pytest.approx(0.0008)
        _wait_done(client, res.json()["run_id"])

    async def test_ensemble_creation_renames_once_for_every_member(self, client, db):
        res = client.post("/api/ensembles", json={**BODY, "cells": [{"label": "base", "n": 2}]})
        assert res.status_code == 201, res.text
        body = res.json()
        assert RENAMED_JEFF in body["renamed"]
        parent = await db.get_ensemble(body["ensemble_id"])
        assert [c["name"] for c in json.loads(parent["cast_json"])] == ["Geoff Beesoh", "Ruth"]
        for member in body["members"]:
            _wait_done(client, member["run_id"])
            run = await db.get_run(member["run_id"])
            assert [c["name"] for c in json.loads(run["cast_json"])] == ["Geoff Beesoh", "Ruth"]

    async def test_a_template_is_saved_with_the_parody(self, client):
        res = client.post("/api/cast-templates", json={"name": "Board", "cast": BODY["cast"]})
        assert res.status_code == 201, res.text
        assert res.json()["renamed"] == [RENAMED_JEFF]
        listed = client.get("/api/cast-templates").json()["templates"]
        assert listed[0]["personas"] == ["Geoff Beesoh", "Ruth"]
        got = client.get("/api/cast-templates/Board").json()
        assert got["cast"][0]["name"] == "Geoff Beesoh" and got["renamed"] == []

    async def test_an_older_template_is_renamed_on_load_and_left_alone_in_storage(self, client, db):
        await db.save_cast_template("Legacy", [{"name": "Elon Musk", "persona": "You are Elon Musk."}], None)
        assert client.get("/api/cast-templates").json()["templates"][0]["personas"] == ["Ilon Moosk"]
        got = client.get("/api/cast-templates/Legacy").json()
        assert got["cast"] == [{"name": "Ilon Moosk", "persona": "You are Ilon Moosk."}]
        assert got["renamed"][0]["from"] == "Elon Musk"
        stored = await db.get_cast_template("Legacy")
        assert stored["cast"][0]["name"] == "Elon Musk", "a load rewrote stored data"

    async def test_the_persona_wizard_draft_is_checked(self, client, monkeypatch):
        async def drafted(brief, count, model=None):
            return [{"name": "Satya Nadella", "persona": "Satya Nadella is calm.", "goals": [],
                     "structured": {"role": "Satya Nadella's deputy"}},
                    {"name": "Ines", "persona": "blunt", "goals": [], "structured": {}}]

        monkeypatch.setattr("matrix_studio.api.app.suggest_cast", drafted)
        res = client.post("/api/personas/suggest", json={"brief": "a board meeting", "count": 2})
        assert res.status_code == 200, res.text
        body = res.json()
        assert [c["name"] for c in body["cast"]] == ["Satcha Nodella", "Ines"]
        assert body["cast"][0]["persona"] == "Satcha Nodella is calm."
        assert body["cast"][0]["structured"]["role"] == "Satcha Nodella's deputy"
        assert body["renamed"][0]["from"] == "Satya Nadella"

    async def test_check_names_reports_without_creating_anything(self, client, db, model):
        model({"Quentin Blaywater": famous("Kwentin Blaywotter")})
        res = client.post("/api/personas/check-names", json={
            "names": ["Jeff Bezos", "Ruth", "Ines Kowalski", "Quentin Blaywater"],
            "consultants": ["Werner Vogels"]})
        assert res.status_code == 200, res.text
        assert res.json()["renamed"] == [
            RENAMED_JEFF,
            {"from": "Quentin Blaywater", "to": "Kwentin Blaywotter", "reason": "real public figure",
             "source": "model", "role": "persona"},
            {"from": "Werner Vogels", "to": "Verner Fogles", "reason": "real public figure", "source": "list",
             "role": "consultant"},
        ]
        assert await db.list_runs() == []
        assert await db.user_spend() > 0, "the model calls were not charged"

    async def test_check_names_is_bounded(self, client):
        assert client.post("/api/personas/check-names", json={"names": ["A B"] * 41}).status_code == 422
        long = client.post("/api/personas/check-names", json={"names": ["Jeff Bezos" + " x" * 500]})
        assert long.status_code == 200 and long.json()["renamed"] == []

    async def test_over_the_cap_the_check_uses_the_list_only(self, client, db, model, monkeypatch):
        from matrix_studio.settings import Settings

        forced = Settings(max_user_monthly_cost_usd=1.0)
        monkeypatch.setattr("matrix_studio.settings.get_settings", lambda: forced)
        await db.add_user_spend(2.0)
        fake = model({"Quentin Blaywater": famous("Kwentin Blaywotter")})
        res = client.post("/api/personas/check-names", json={"names": ["Jeff Bezos", "Quentin Blaywater"]})
        assert res.json()["renamed"] == [RENAMED_JEFF]
        assert fake.calls == [], "a caller over their cap kept buying model calls"

    async def test_persona_packs_carry_no_real_names(self):
        from matrix_studio import persona_packs

        for pack in persona_packs.list_packs():
            member = pack["persona"]
            assert rn.lookup(member["name"]) is None, member["name"]
            text = json.dumps(member)
            for fig in rn.figures():
                for spelling in fig.spellings:
                    assert rn._pattern(spelling).search(text) is None, (member["name"], spelling)


class TestTheManagerItself:
    """`scripts/start_conversation.py` calls `RunManager` directly, with no route in front of it — which is
    why the check lives in the manager rather than in the routes."""

    async def test_create_run_renames_with_no_route_in_front(self, db, monkeypatch):
        from matrix_studio.api.manager import RunManager
        from tests.test_api import make_fake_run

        async def fake_name(topic, cast_names=None, model=None, name_exists=None):
            return {"name": "scripted-run", "description": "d", "slug": "scripted-run", "source": "llm"}

        monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
        monkeypatch.setattr("matrix_studio.api.manager.run_simulation", make_fake_run(turns=1))
        manager = RunManager(db)
        result = await manager.create_run(
            {"topic": "t", "cast": [{"name": "Jensen Huang", "persona": "p"}], "config": {"max_messages": 1}},
            owner_sub=TEST_OWNER,
        )
        assert result["renamed"][0]["to"] == "Jenzen Hwong"
        task = manager._tasks.get(result["run_id"])
        if task is not None:
            await task  # the local path writes the row from the background task
        run = await db.get_run(result["run_id"])
        assert json.loads(run["cast_json"])[0]["name"] == "Jenzen Hwong"


class TestTheBranchAndTheCli:
    async def test_add_persona_at_a_fork_is_renamed(self, aws_backend, monkeypatch):
        from tests import test_api_phase2b as b2

        async def fake_name(topic, cast_names=None, model=None, name_exists=None):
            # Unique per owner, as the real namer is: the branch and its parent need different names.
            name, n = "fork-run", 2
            while name_exists is not None and await name_exists(name):
                name, n = f"fork-run-{n}", n + 1
            return {"name": name, "description": "t", "slug": name, "source": "llm"}

        for target in ("matrix_studio.api.manager.generate_run_name", "matrix_studio.api.app.generate_run_name",
                       "matrix_studio.branching.generate_run_name"):
            monkeypatch.setattr(target, fake_name)
        from matrix_studio.api.app import create_app

        with TestClient(create_app()) as c:
            run_id = b2._make_run(c)
            with patch("matrix_studio.engine.simulator.litellm.acompletion",
                       side_effect=b2._always(["Ilon Moosk", "Ada", "Ben"])):
                meta = c.post(f"/api/runs/{run_id}/branch", json={
                    "from_turn": 2,
                    "mutation": {"kind": "add_persona", "name": "Elon Musk",
                                 "persona": "You are Elon Musk.", "goals": ["Be Elon Musk"]},
                }).json()
                detail = b2._wait_status(c, meta["run_id"])
        assert meta["renamed"][0]["from"] == "Elon Musk" and meta["renamed"][0]["to"] == "Ilon Moosk"
        mutation = detail["config"]["branch_mutation"]
        assert mutation["name"] == "Ilon Moosk"
        assert mutation["persona"] == "You are Ilon Moosk." and mutation["goals"] == ["Be Ilon Moosk"]

    async def test_the_cli_renames_before_running(self, tmp_path, monkeypatch, caplog):
        from matrix_studio import __main__ as cli

        seen = {}

        async def fake_run(request, db=None, **kwargs):
            seen.update(request)
            return {"status": "complete", "total_turns": 0, "total_cost_usd": 0.0}

        monkeypatch.setattr(cli, "run_simulation", fake_run)
        req = tmp_path / "req.json"
        req.write_text(json.dumps({"topic": "t", "cast": [{"name": "Sam Altman", "persona": "You are Sam Altman."}]}))
        out = tmp_path / "out.json"
        with caplog.at_level(logging.WARNING):
            code = await cli.run_from_file(req, out, None, no_db=True)
        assert code == 0
        assert seen["cast"] == [{"name": "Sahm Oltmun", "persona": "You are Sahm Oltmun."}]
        assert json.loads(out.read_text())["renamed"][0]["to"] == "Sahm Oltmun"
        assert "'Sam Altman' is a real public figure, so this persona is 'Sahm Oltmun'" in caplog.text
