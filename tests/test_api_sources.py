# SPDX-License-Identifier: Apache-2.0
"""
Opening the source behind a passage a conversation retrieved — `GET /api/runs/{ref}/sources/{doc}`.

A `document.retrieved` event records WHICH passage a persona was given and not its text, so a reader
could see that a persona drew on "Iowa Admin Code Ch. 811 #4" and not what it says. This route closes
that, and the tests are mostly about what it must NOT do, because it touches two boundaries.

**It is scoped to the run.** A document opens only if this run could have retrieved it: its own
attachment, or a document in a collection it binds that the caller may read. Anything else is a 404,
identical to a document that does not exist — so the route cannot be used to open arbitrary documents
by id, or to learn that one exists.

**Full text only for what the caller OWNS.** A shared collection's S3 body lives under its owner's
prefix, and docs/project/PHASE6-KB-DESIGN.md §8.2 is explicit that a grantee retrieves passages and does not
download the source. So a grantee gets the cited passage and its neighbours — exactly what a turn
already showed them — and a notice saying why they get no more.
"""

import pytest

from tests.support import TEST_OWNER
from tests.test_api_knowledge_bases import (  # noqa: F401 - fixtures
    OTHER,
    _as,
    _create,
    _mock_embedder,
    _storage_backend,
    client,
)

LONG = "\n\n".join(
    f"Paragraph {i}. The board shall require an examination before a specialty plan is "
    f"renewed, and section {i} explains the conditions under which that applies." * 3
    for i in range(12)
)


def _add_kb_doc(client, kb_id, title="Iowa Admin Code Ch. 811", text=LONG):
    r = client.post(f"/api/knowledge-bases/{kb_id}/documents", json={"title": title, "text": text})
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


async def _run(db, run_id, *, kbs=None, persona_kbs=None):
    await db.create_run(
        run_id=run_id, topic="plan renewal",
        cast=[{"name": "Casey", **({"knowledge_bases": persona_kbs} if persona_kbs else {})}],
        name=run_id, config={"knowledge_bases": kbs or []}, owner_sub=TEST_OWNER,
    )


def _get(client, run_id, doc_id, ordinal=None):
    q = f"?ordinal={ordinal}" if ordinal is not None else ""
    return client.get(f"/api/runs/{run_id}/sources/{doc_id}{q}")


class TestTheOwnerReadsTheWholeSource:
    async def test_a_bound_collection_s_document_opens_in_full(self, client, db):
        kb = _create(client, "statutes")
        doc = _add_kb_doc(client, kb["id"])
        await _run(db, "r1", kbs=[kb["id"]])

        r = _get(client, "r1", doc, ordinal=1)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["full"] is True
        assert body["owned"] is True
        assert body["kb_name"] == "statutes"
        assert body["cited_ordinal"] == 1
        assert len(body["chunks"]) > 1, "a multi-chunk source should come back as its chunks"

    async def test_chunk_ordinals_match_what_retrieval_recorded(self, client, db):
        """Ordinal N here must be ordinal N in the event, or the highlighted passage is the wrong
        one — re-chunked with the same function retrieval uses, and each chunk's OWN ordinal."""
        from matrix_studio.documents import chunk_text

        kb = _create(client, "statutes")
        doc = _add_kb_doc(client, kb["id"])
        await _run(db, "r1", kbs=[kb["id"]])

        chunks = _get(client, "r1", doc).json()["chunks"]
        expected = chunk_text(await db.document_text(doc))
        assert [c["ordinal"] for c in chunks] == [c.ordinal for c in expected]
        assert [c["text"] for c in chunks] == [c.content for c in expected]

    async def test_a_persona_level_binding_is_reachable_too(self, client, db):
        kb = _create(client, "casey-own")
        doc = _add_kb_doc(client, kb["id"])
        await _run(db, "r1", persona_kbs=[kb["id"]])
        assert _get(client, "r1", doc).status_code == 200

    async def test_a_researched_document_links_to_the_page_it_was_read_from(self, client, db):
        kb = _create(client, "research")
        doc = await db.add_kb_document(
            kb["id"], title="Texas Occupations Code Ch. 801", text=LONG, char_count=len(LONG),
            source_path="https://statutes.capitol.texas.gov/Docs/OC/htm/OC.801.htm",
            origin="researched", authority="controlling",
        )
        await _run(db, "r1", kbs=[kb["id"]])

        body = _get(client, "r1", doc).json()
        assert body["origin"] == "researched"
        assert body["authority"] == "controlling"
        # The link a reader needs in order to check the original rather than trust the copy.
        assert body["source_url"].startswith("https://statutes.capitol.texas.gov/")

    async def test_an_upload_s_filename_is_not_offered_as_a_link(self, client, db):
        kb = _create(client, "uploads")
        doc = _add_kb_doc(client, kb["id"])
        await _run(db, "r1", kbs=[kb["id"]])
        assert _get(client, "r1", doc).json()["source_url"] is None


class TestTheRouteIsScopedToTheRun:
    async def test_a_collection_this_run_does_not_bind_is_404(self, client, db):
        """Otherwise this is a way to read any of the caller's documents through any run, and
        the answer a reader gets would not be about THIS conversation."""
        bound = _create(client, "bound")
        other = _create(client, "not-bound")
        doc = _add_kb_doc(client, other["id"])
        await _run(db, "r1", kbs=[bound["id"]])
        assert _get(client, "r1", doc).status_code == 404

    async def test_a_document_that_does_not_exist_is_404(self, client, db):
        await _run(db, "r1")
        assert _get(client, "r1", "no-such-document").status_code == 404

    async def test_another_run_s_attachment_is_404(self, client, db):
        await _run(db, "r1")
        await _run(db, "r2")
        doc = await db.add_document("r2", title="r2 only", chunks=["private to r2"])
        assert _get(client, "r1", doc).status_code == 404
        assert _get(client, "r2", doc).status_code == 200

    async def test_somebody_else_s_run_is_404(self, client, db):
        kb = _create(client, "statutes")
        doc = _add_kb_doc(client, kb["id"])
        await _run(db, "r1", kbs=[kb["id"]])
        _as(client, OTHER)
        assert _get(client, "r1", doc).status_code == 404


class TestAGranteeGetsPassagesNotTheSource:
    async def _shared(self, client, db, monkeypatch):
        """A collection owned by OTHER, granted to TEST_OWNER, bound to TEST_OWNER's run."""
        _as(client, OTHER)
        kb = _create(client, "their-statutes")
        doc = _add_kb_doc(client, kb["id"])
        client.post(f"/api/knowledge-bases/{kb['id']}/grants", json={"user": TEST_OWNER}
                    ).raise_for_status()
        _as(client, TEST_OWNER)
        await _run(db, "r1", kbs=[kb["id"]])
        return kb, doc

    async def test_a_grantee_never_receives_the_full_text(self, client, db, monkeypatch):
        kb, doc = await self._shared(client, db, monkeypatch)
        read = []

        async def forbidden(self, document_id):
            read.append(document_id)
            return "THE WHOLE SOURCE"

        async def passages(self, kb_id, document_id, ordinals):
            return {o: f"passage {o}" for o in ordinals}

        from matrix_studio.storage.dynamo import DynamoStorage

        monkeypatch.setattr(DynamoStorage, "document_text", forbidden)
        monkeypatch.setattr(DynamoStorage, "kb_passages", passages)

        body = _get(client, "r1", doc, ordinal=4).json()
        assert body["full"] is False
        assert body["owned"] is False
        # The S3 body must not even be READ for a grantee, not merely withheld from the response:
        # §8.2's boundary is on the credentials, and a route that read it and discarded it would
        # be one refactor from returning it.
        assert read == []
        assert "THE WHOLE SOURCE" not in str(body)
        # The cited passage and one either side.
        assert [c["ordinal"] for c in body["chunks"]] == [3, 4, 5]
        assert "belongs to another account" in body["notice"]

    async def test_the_window_stops_at_the_start_of_the_document(self, client, db, monkeypatch):
        kb, doc = await self._shared(client, db, monkeypatch)
        asked = []

        async def passages(self, kb_id, document_id, ordinals):
            asked.extend(ordinals)
            return {o: "x" for o in ordinals}

        from matrix_studio.storage.dynamo import DynamoStorage

        monkeypatch.setattr(DynamoStorage, "kb_passages", passages)
        _get(client, "r1", doc, ordinal=0)
        assert min(asked) == 0

    async def test_a_revoked_grant_closes_the_source_immediately(self, client, db, monkeypatch):
        """The grant is re-checked on every request, as it is on every turn — the binding on the
        run is not permission to read."""
        kb, doc = await self._shared(client, db, monkeypatch)
        _as(client, OTHER)
        client.delete(f"/api/knowledge-bases/{kb['id']}/grants",
                      params={"user": TEST_OWNER}).raise_for_status()
        _as(client, TEST_OWNER)
        assert _get(client, "r1", doc).status_code == 404


class TestTheDocumentReadsOnce:
    async def test_display_drops_each_chunk_s_carried_overlap(self, client, db):
        """Chunks overlap by design; shown one after another, the boundary text appeared twice."""
        from matrix_studio.documents import chunk_text, join_chunks

        kb = _create(client, "statutes")
        doc = _add_kb_doc(client, kb["id"])
        await _run(db, "r1", kbs=[kb["id"]])
        chunks = _get(client, "r1", doc, ordinal=1).json()["chunks"]
        assert len(chunks) > 2
        # Reading the display parts in order is the document exactly once — the same text
        # join_chunks rebuilds — while `text` stays the passage as a persona was given it.
        assert "\n\n".join(c["display"] for c in chunks) == join_chunks([c["text"] for c in chunks])
        assert chunks[0]["display"] == chunks[0]["text"]
        assert all(len(c["display"]) < len(c["text"]) for c in chunks[1:])
        assert [c["text"] for c in chunks] == [c.content for c in chunk_text(LONG)]


class TestTheDossierNamesItsCollections:
    """A KB-bound persona's dossier listed `documents: []` beside passages it had demonstrably read."""

    async def _snapshot(self, db, run_id, names):
        from matrix_studio.state import AgentState, SimSnapshot

        await db.save_snapshot(SimSnapshot(
            run_id=run_id, turn=1, topic="t",
            agents={n: AgentState(name=n, persona="p", goals=[]) for n in names},
            conversation=[], status="running", created_at=1, total_turns=1,
        ))

    async def test_run_and_persona_collections_are_listed_with_their_scope(self, client, db):
        shared = _create(client, "statutes")
        own = _create(client, "casey notes")
        await _run(db, "r1", kbs=[shared["id"]], persona_kbs=[own["id"]])
        await self._snapshot(db, "r1", ["Casey"])
        body = client.get("/api/runs/r1/agents/Casey/dossier").json()
        got = {(k["name"], k["scope"], k["readable"]) for k in body["knowledge_bases"]}
        assert got == {("statutes", "run", True), ("casey notes", "persona", True)}

    async def test_a_revoked_collection_shows_as_unreadable_and_nameless(self, client, db):
        # The owner binds a KB, then another user reads the dossier of a run in THEIR account that
        # binds it without a grant: listed (it is in the config) but not readable, and not named.
        kb = _create(client, "private notes")
        _as(client, OTHER)
        await db.create_run(run_id="r2", topic="t", cast=[{"name": "Avery"}], name="r2",
                            config={"knowledge_bases": [kb["id"]]}, owner_sub=OTHER)
        from matrix_studio.state import AgentState, SimSnapshot
        await db.for_owner(OTHER).save_snapshot(SimSnapshot(
            run_id="r2", turn=1, topic="t", agents={"Avery": AgentState(name="Avery", persona="p", goals=[])},
            conversation=[], status="running", created_at=1, total_turns=1,
        ))
        [entry] = client.get("/api/runs/r2/agents/Avery/dossier").json()["knowledge_bases"]
        assert entry["readable"] is False and entry["name"] is None
