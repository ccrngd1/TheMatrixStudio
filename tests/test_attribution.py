# SPDX-License-Identifier: Apache-2.0
"""
Which passage a message QUOTES — `matrix_studio/attribution.py` and `GET /api/runs/{ref}/quotes`.

The rule under test is restraint: a message is attributed to a passage only on a verbatim run of
content words, never on shared vocabulary. Measured on stored runs, a best guess from vocabulary could
not separate in-view passages (the top one led by <0.10 in 350 of 377 messages), so reporting one
would present a coin toss as a finding.

The route reads passage text through the same access check as the source viewer, so it is also
tested for what it must not reach.
"""

import json

import pytest

from matrix_studio import attribution as at
from tests.test_api_knowledge_bases import (  # noqa: F401 - fixtures
    OTHER,
    _as,
    _create,
    _mock_embedder,
    _storage_backend,
    client,
)
from tests.test_api_sources import LONG, _add_kb_doc, _run

PASSAGE = "The board shall require an examination before a specialty plan is renewed."


class TestLongestQuote:
    def test_a_lifted_clause_is_a_quote_and_the_phrase_is_the_message_s_own_text(self):
        msg = "Frankly, the Board shall require an Examination before anything else happens."
        q = at.longest_quote(msg, PASSAGE)
        assert q["phrase"] == "the Board shall require an Examination before"
        assert q["content_words"] >= at.MIN_CONTENT_WORDS

    def test_shared_vocabulary_out_of_order_is_not_a_quote(self):
        msg = "Renewed plans need an examination; the specialty board decides what is required."
        assert at.longest_quote(msg, PASSAGE) is None

    def test_grammar_words_do_not_make_a_quote(self):
        # Seven words in order, but only "board" carries content.
        assert at.longest_quote("and it is the board that we", "and it is the board that we") is None

    def test_numbers_are_not_content(self):
        assert at.longest_quote("version 0 6 0 of 12 13 14", "version 0 6 0 of 12 13 14") is None


class TestAttribute:
    def test_strongest_first_and_unreadable_passages_skipped(self):
        passages = [
            {"chunk_id": 1, "document_id": "a", "title": "A", "ordinal": 0, "text": PASSAGE},
            {"chunk_id": 2, "document_id": "b", "title": "B", "ordinal": 3,
             "text": "Every renewal requires the board to require an examination before a specialty plan."},
            {"chunk_id": 3, "document_id": "c", "title": "C", "ordinal": 0, "text": None},
        ]
        msg = "the board shall require an examination before a specialty plan is renewed"
        out = at.attribute(msg, passages)
        assert [q["title"] for q in out][0] == "A"
        assert all(q["title"] != "C" for q in out)

    def test_nothing_quoted_is_an_empty_list(self):
        assert at.attribute("nothing in common here at all", [{"text": PASSAGE}]) == []


async def _events(db, run_id, message, doc_id, ordinal=1, refs=True):
    passage = {"chunk_id": 42, "document_id": doc_id, "title": "Iowa Admin Code Ch. 811",
               "ordinal": ordinal, "score": 1.0, "chars": 100}
    await db.append_event(run_id, 1, 1, "document.retrieved",
                          {"speaker": "Casey", "passages": [passage]}, "Casey")
    await db.append_event(run_id, 1, 2, "agent.response",
                          {"speaker": "Casey", "message": message,
                           **({"document_refs": [42]} if refs else {})}, "Casey")


class TestTheRoute:
    async def test_reports_the_quote_with_its_words(self, client, db):
        kb = _create(client, "statutes")
        doc = _add_kb_doc(client, kb["id"])
        await _run(db, "r1", kbs=[kb["id"]])
        await _events(db, "r1", "As written, the board shall require an examination before a "
                                "specialty plan is renewed, full stop.", doc)
        r = client.get("/api/runs/r1/quotes")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["messages_with_sources"] == 1 and body["messages_quoting"] == 1
        q = body["quotes"]["2"][0]
        assert q["document_id"] == doc and q["ordinal"] == 1
        assert "shall require an examination" in q["phrase"]

    async def test_paraphrase_is_not_attributed(self, client, db):
        kb = _create(client, "statutes")
        doc = _add_kb_doc(client, kb["id"])
        await _run(db, "r1", kbs=[kb["id"]])
        await _events(db, "r1", "I think exams matter before renewals.", doc)
        body = client.get("/api/runs/r1/quotes").json()
        assert body["messages_with_sources"] == 1 and body["quotes"] == {}

    async def test_a_document_outside_the_run_is_not_read(self, client, db):
        # The run binds nothing, so its own events cannot make it read an unbound collection's text.
        kb = _create(client, "statutes")
        doc = _add_kb_doc(client, kb["id"])
        await _run(db, "r1", kbs=[])
        await _events(db, "r1", "the board shall require an examination before a specialty plan", doc)
        assert client.get("/api/runs/r1/quotes").json()["quotes"] == {}

    async def test_another_user_cannot_read_the_run(self, client, db):
        kb = _create(client, "statutes")
        doc = _add_kb_doc(client, kb["id"])
        await _run(db, "r1", kbs=[kb["id"]])
        await _events(db, "r1", "the board shall require an examination before a specialty plan", doc)
        _as(client, OTHER)
        assert client.get("/api/runs/r1/quotes").status_code == 404
