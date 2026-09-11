# SPDX-License-Identifier: Apache-2.0
"""Phase 5b-2 tests — the document API over the real app (litellm mocked).

Covers attach (inline text and by path), list with persona scoping, delete,
reindex, and the retrieval-inspection endpoint. The inspection endpoint carries
most of the weight here: BM25 is lexical, so retrieval quality has to be
*measurable* rather than trusted, and that endpoint is the instrument.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from matrix_studio.api.app import create_app


@pytest.fixture(autouse=True)
def _storage_backend(aws_backend):
    """Every test in this file builds the FastAPI app.

    The app's lifespan connects to DynamoDB, so without a mocked account it reaches
    real AWS — which surfaces as `ExpiredTokenException` on a `Scan` and reads like a
    credentials problem rather than a missing fixture. Autouse and explicit here
    rather than hidden in `conftest.py`, so the dependency is visible in the file that
    has it.
    """


class _Resp:
    def __init__(self, content):
        self.choices = [MagicMock(message=MagicMock(content=content))]
        self.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
        self._hidden_params = {"response_cost": 0.001}


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        return {
            "name": "trusted-robot", "description": "t",
            "slug": "trusted-robot", "source": "llm",
        }

    monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.api.app.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.branching.generate_run_name", fake_name)
    app = create_app(db_path=str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def _wait(client, ref, tries=300):
    for _ in range(tries):
        r = client.get(f"/api/runs/{ref}")
        if r.status_code == 200 and r.json()["status"] in ("complete", "failed"):
            return r.json()
        time.sleep(0.02)
    return client.get(f"/api/runs/{ref}").json()


REQUEST = {
    "topic": "egress inspection and token accounting",
    "cast": [
        {"name": "Dana", "persona": "distribution lead", "goals": ["protect install"]},
        {"name": "Marcus", "persona": "cost analyst", "goals": ["measure spend"]},
    ],
    "config": {"max_messages": 1, "generate_avatars": False},
}

DANA_TEXT = (
    "Egress inspection provides auditable evidence for the compliance auditor.\n\n"
    "A single choke point makes exfiltration visible in the logs."
)
MARCUS_TEXT = (
    "Token accounting must show a measured delta before a feature ships.\n\n"
    "Unexplained spend during a demonstration ends the conversation."
)


def _fake(*args, **kwargs):
    text = " ".join(m["content"] for m in kwargs["messages"])
    if "conversation moderator" in text:
        return _Resp(json.dumps({"speaker": "Dana", "reason": "her turn"}))
    return _Resp("A reply.")


@pytest.fixture
def run_ref(client):
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake):
        created = client.post("/api/runs", json=REQUEST).json()
        ref = created["run_id"]
        _wait(client, ref)
    return ref


# --------------------------------------------------------------------------- #
# Attach
# --------------------------------------------------------------------------- #


def test_attach_inline_text(client, run_ref):
    r = client.post(
        f"/api/runs/{run_ref}/documents",
        json={"persona_name": "Dana", "title": "egress.md", "text": DANA_TEXT},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["title"] == "egress.md"
    assert body["persona_name"] == "Dana"
    assert body["chunk_count"] >= 1
    assert body["char_count"] > 0
    assert body["document_id"]


def test_attach_by_path(client, run_ref, tmp_path):
    p = tmp_path / "notes.md"
    p.write_text(MARCUS_TEXT)
    r = client.post(
        f"/api/runs/{run_ref}/documents",
        json={"persona_name": "Marcus", "path": str(p)},
    )
    assert r.status_code == 201, r.text
    assert r.json()["title"] == "notes.md"
    assert r.json()["media_type"] == "md"


def test_attach_cast_wide_when_persona_omitted(client, run_ref):
    r = client.post(
        f"/api/runs/{run_ref}/documents",
        json={"title": "shared.md", "text": "Shared briefing material for everyone."},
    )
    assert r.status_code == 201
    assert r.json()["persona_name"] is None
    # Visible to every persona.
    for who in ("Dana", "Marcus"):
        listed = client.get(f"/api/runs/{run_ref}/documents?persona={who}").json()
        assert "shared.md" in {d["title"] for d in listed["documents"]}


def test_attach_requires_exactly_one_of_text_or_path(client, run_ref, tmp_path):
    both = client.post(
        f"/api/runs/{run_ref}/documents",
        json={"title": "x", "text": "a", "path": str(tmp_path / "y.md")},
    )
    assert both.status_code == 422
    assert "exactly one" in both.json()["detail"]

    neither = client.post(f"/api/runs/{run_ref}/documents", json={"title": "x"})
    assert neither.status_code == 422


def test_attach_text_requires_title(client, run_ref):
    r = client.post(f"/api/runs/{run_ref}/documents", json={"text": "some content"})
    assert r.status_code == 422
    assert "title is required" in r.json()["detail"]


def test_attach_rejects_persona_not_in_cast(client, run_ref):
    """Otherwise the operator silently creates material nobody can retrieve."""
    r = client.post(
        f"/api/runs/{run_ref}/documents",
        json={"persona_name": "Nobody", "title": "x.md", "text": "content"},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "Nobody" in detail and "Dana" in detail


def test_attach_missing_path_is_422_with_reason(client, run_ref, tmp_path):
    r = client.post(
        f"/api/runs/{run_ref}/documents",
        json={"persona_name": "Dana", "path": str(tmp_path / "absent.pdf")},
    )
    assert r.status_code == 422
    assert "not found" in r.json()["detail"]


def test_attach_unsupported_type_is_422(client, run_ref, tmp_path):
    p = tmp_path / "sheet.xlsx"
    p.write_text("nope")
    r = client.post(
        f"/api/runs/{run_ref}/documents", json={"persona_name": "Dana", "path": str(p)}
    )
    assert r.status_code == 422
    assert "Unsupported" in r.json()["detail"]


def test_attach_unknown_run_is_404(client):
    r = client.post(
        "/api/runs/no-such-run/documents", json={"title": "x", "text": "y"}
    )
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# List
# --------------------------------------------------------------------------- #


def test_list_documents_and_totals(client, run_ref):
    client.post(f"/api/runs/{run_ref}/documents",
                json={"persona_name": "Dana", "title": "a.md", "text": DANA_TEXT})
    client.post(f"/api/runs/{run_ref}/documents",
                json={"persona_name": "Marcus", "title": "b.md", "text": MARCUS_TEXT})
    body = client.get(f"/api/runs/{run_ref}/documents").json()
    assert {d["title"] for d in body["documents"]} == {"a.md", "b.md"}
    assert body["total_chunks"] >= 2
    assert body["total_chars"] > 0


def test_list_scoped_by_persona_excludes_other_personas(client, run_ref):
    client.post(f"/api/runs/{run_ref}/documents",
                json={"persona_name": "Dana", "title": "dana.md", "text": DANA_TEXT})
    client.post(f"/api/runs/{run_ref}/documents",
                json={"persona_name": "Marcus", "title": "marcus.md", "text": MARCUS_TEXT})
    dana = client.get(f"/api/runs/{run_ref}/documents?persona=Dana").json()
    assert {d["title"] for d in dana["documents"]} == {"dana.md"}


def test_list_unknown_run_is_404(client):
    assert client.get("/api/runs/nope/documents").status_code == 404


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #


def test_delete_removes_document_and_its_index_entries(client, run_ref):
    doc_id = client.post(
        f"/api/runs/{run_ref}/documents",
        json={"persona_name": "Dana", "title": "gone.md", "text": DANA_TEXT},
    ).json()["document_id"]

    found = client.get(f"/api/runs/{run_ref}/documents/search?q=egress inspection").json()
    assert found["passages"]

    r = client.delete(f"/api/runs/{run_ref}/documents/{doc_id}")
    assert r.status_code == 200 and r.json()["deleted"] is True

    after = client.get(f"/api/runs/{run_ref}/documents/search?q=egress inspection").json()
    assert after["passages"] == []
    assert client.get(f"/api/runs/{run_ref}/documents").json()["documents"] == []


def test_delete_unknown_document_is_404(client, run_ref):
    assert client.delete(f"/api/runs/{run_ref}/documents/nope").status_code == 404


def test_cannot_delete_a_document_belonging_to_another_run(client, run_ref):
    """A document id from another run must not be removable through this route."""
    doc_id = client.post(
        f"/api/runs/{run_ref}/documents",
        json={"persona_name": "Dana", "title": "a.md", "text": DANA_TEXT},
    ).json()["document_id"]

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake):
        other = client.post("/api/runs", json=REQUEST).json()["run_id"]
        _wait(client, other)

    r = client.delete(f"/api/runs/{other}/documents/{doc_id}")
    assert r.status_code == 404
    # And the original is untouched.
    assert client.get(f"/api/runs/{run_ref}/documents").json()["documents"]


# --------------------------------------------------------------------------- #
# Reindex — the documented recovery path
# --------------------------------------------------------------------------- #


def test_reindex_reports_chunk_count_and_is_idempotent(client, run_ref):
    client.post(f"/api/runs/{run_ref}/documents",
                json={"persona_name": "Dana", "title": "a.md", "text": DANA_TEXT})
    first = client.post(f"/api/runs/{run_ref}/documents/reindex")
    assert first.status_code == 200
    assert first.json()["reindexed_chunks"] >= 1

    client.post(f"/api/runs/{run_ref}/documents/reindex")
    # Search still returns each passage once, not duplicated by the rebuild.
    passages = client.get(
        f"/api/runs/{run_ref}/documents/search?q=egress inspection"
    ).json()["passages"]
    assert len({p["chunk_id"] for p in passages}) == len(passages)


def test_reindex_reports_the_chunk_count_and_leaves_search_working(client, run_ref):
    """The reindex endpoint's contract changed from "rebuild" to "report".

    This test used to wipe the persistent FTS5 index with `delete-all`, drive
    `corpus=database` to show the wipe had broken it, call reindex, and show it
    recovered. Every step needed a persistent derived index that could go stale.

    There is none: the BM25 index is built per query from the stored document text and
    discarded, so it cannot be stale, and the vectors ARE the vector index — rebuilding
    them means re-embedding, which the embed endpoint already does incrementally.

    So the endpoint's honest job is to report how many chunks exist, which is what an
    operator asking "is my index consistent" actually wants to know now that the answer
    is "it cannot be otherwise". Kept as an API-level test because the route is still
    there and a caller still depends on its shape.

    The property the old test protected — a lost derived structure must not make
    material unfindable — is asserted by
    `test_lexical_search_survives_losing_every_vector` in test_retrieval.py, against
    the derived structure that can still be lost.
    """
    client.post(f"/api/runs/{run_ref}/documents",
                json={"persona_name": "Dana", "title": "a.md", "text": DANA_TEXT})
    base = f"/api/runs/{run_ref}/documents/search?q=egress inspection"
    assert client.get(base).json()["passages"]

    reported = client.post(f"/api/runs/{run_ref}/documents/reindex")
    assert reported.status_code == 200, reported.text
    count = reported.json()["reindexed_chunks"]
    assert count >= 1, reported.json()

    # It is a report, so it does not change anything — calling it twice must give the
    # same answer rather than accumulating.
    assert client.post(
        f"/api/runs/{run_ref}/documents/reindex"
    ).json()["reindexed_chunks"] == count

    # And search is unaffected either way, because it never consulted a derived index.
    assert client.get(base).json()["passages"]
    assert (
        client.get(f"{base}&corpus=database").json()["passages"]
        == client.get(f"{base}&corpus=run").json()["passages"]
    ), "the two corpus values diverged, which is what the v0.6 contamination bug was"

def test_search_reports_which_corpus_scored_it(client, run_ref):
    """
    A score is only comparable to another from the same corpus, so say which.

    Without this the two scorers return the same field with different meanings, and a
    number copied out of this endpoint into a measurement is unattributable.
    """
    client.post(f"/api/runs/{run_ref}/documents",
                json={"persona_name": "Dana", "title": "a.md", "text": DANA_TEXT})
    base = f"/api/runs/{run_ref}/documents/search?q=egress inspection"
    assert client.get(base).json()["corpus"] == "run"
    assert client.get(f"{base}&corpus=database").json()["corpus"] == "database"
    # A typo must not silently pick a scorer.
    assert client.get(f"{base}&corpus=wholedb").status_code == 422


def test_reindex_unknown_run_is_404(client):
    assert client.post("/api/runs/nope/documents/reindex").status_code == 404


# --------------------------------------------------------------------------- #
# Retrieval inspection — the measurement instrument
# --------------------------------------------------------------------------- #


def test_search_returns_sanitised_query_and_scored_passages(client, run_ref):
    client.post(f"/api/runs/{run_ref}/documents",
                json={"persona_name": "Dana", "title": "egress.md", "text": DANA_TEXT})
    body = client.get(
        f"/api/runs/{run_ref}/documents/search?q=egress inspection evidence"
    ).json()
    assert body["fts_query"] == '"egress" OR "inspection" OR "evidence"'
    assert body["terms"] == ["egress", "inspection", "evidence"]
    assert body["passages"]
    first = body["passages"][0]
    assert "Egress inspection" in first["content"]
    assert first["citation"].startswith("egress.md #")
    assert isinstance(first["score"], float)
    assert body["matched_before_budget"] >= len(body["passages"])


def test_search_scoping_matches_engine_scoping(client, run_ref):
    client.post(f"/api/runs/{run_ref}/documents",
                json={"persona_name": "Dana", "title": "dana.md", "text": DANA_TEXT})
    client.post(f"/api/runs/{run_ref}/documents",
                json={"persona_name": "Marcus", "title": "marcus.md", "text": MARCUS_TEXT})
    q = "egress inspection token accounting"
    dana = client.get(f"/api/runs/{run_ref}/documents/search?q={q}&persona=Dana").json()
    marcus = client.get(f"/api/runs/{run_ref}/documents/search?q={q}&persona=Marcus").json()
    assert {p["title"] for p in dana["passages"]} == {"dana.md"}
    assert {p["title"] for p in marcus["passages"]} == {"marcus.md"}


def test_search_respects_budget(client, run_ref):
    big = "\n\n".join(
        f"Paragraph {i} about egress inspection evidence." for i in range(200)
    )
    client.post(f"/api/runs/{run_ref}/documents",
                json={"persona_name": "Dana", "title": "big.md", "text": big})
    body = client.get(
        f"/api/runs/{run_ref}/documents/search?q=egress inspection&k=10&max_chars=300"
    ).json()
    assert body["total_chars"] <= 300


def test_search_with_no_usable_terms_says_so(client, run_ref):
    body = client.get(f"/api/runs/{run_ref}/documents/search?q=the a of to").json()
    assert body["passages"] == []
    assert body["fts_query"] == ""
    assert "stopwords" in body["note"]


def test_search_operators_in_the_query_do_not_error(client, run_ref):
    """A user typing FTS5 syntax must not produce a 500."""
    client.post(f"/api/runs/{run_ref}/documents",
                json={"persona_name": "Dana", "title": "a.md", "text": DANA_TEXT})
    for q in ['egress AND "inspection', "NEAR(egress inspection)", "egress*", "-egress"]:
        r = client.get(f"/api/runs/{run_ref}/documents/search", params={"q": q})
        assert r.status_code == 200, f"{q!r} -> {r.status_code}"


def test_search_requires_a_query(client, run_ref):
    assert client.get(f"/api/runs/{run_ref}/documents/search").status_code == 422


def test_search_is_read_only(client, run_ref):
    """Inspection must not mutate the canonical event log."""
    client.post(f"/api/runs/{run_ref}/documents",
                json={"persona_name": "Dana", "title": "a.md", "text": DANA_TEXT})
    before = client.get(f"/api/runs/{run_ref}/events").json()
    client.get(f"/api/runs/{run_ref}/documents/search?q=egress inspection")
    after = client.get(f"/api/runs/{run_ref}/events").json()
    assert before == after


def test_search_unknown_run_is_404(client):
    assert client.get("/api/runs/nope/documents/search?q=egress").status_code == 404


# --------------------------------------------------------------------------- #
# Dossier surface — what the persona has, and what it actually drew on
# --------------------------------------------------------------------------- #


RETRIEVAL_REQUEST = {
    "topic": "egress inspection and token accounting",
    "cast": [
        {"name": "Dana", "persona": "distribution lead", "goals": ["protect install"]},
        {"name": "Marcus", "persona": "cost analyst", "goals": ["measure spend"]},
    ],
    "config": {
        "max_messages": 2,
        "generate_avatars": False,
        "retrieval": {"enabled": True, "k": 2, "max_chars": 900},
    },
}


def test_dossier_lists_attached_documents_and_retrievals(client, tmp_path):
    dana_doc = tmp_path / "dana.md"
    dana_doc.write_text(DANA_TEXT)
    request = json.loads(json.dumps(RETRIEVAL_REQUEST))
    request["cast"][0]["documents"] = [str(dana_doc)]

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake):
        ref = client.post("/api/runs", json=request).json()["run_id"]
        _wait(client, ref)

    body = client.get(f"/api/runs/{ref}/agents/Dana/dossier").json()
    assert [d["title"] for d in body["documents"]] == ["dana.md"]
    assert body["documents"][0]["cast_wide"] is False
    assert body["documents"][0]["chunk_count"] >= 1

    assert body["document_retrievals"], "dossier did not surface what Dana drew on"
    first = body["document_retrievals"][0]
    assert first["query"]
    assert first["passages"] and first["passages"][0]["title"] == "dana.md"
    assert first["total_chars"] <= 900


def test_dossier_does_not_leak_another_personas_documents(client, tmp_path):
    dana_doc = tmp_path / "dana.md"
    dana_doc.write_text(DANA_TEXT)
    request = json.loads(json.dumps(RETRIEVAL_REQUEST))
    request["cast"][0]["documents"] = [str(dana_doc)]

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake):
        ref = client.post("/api/runs", json=request).json()["run_id"]
        _wait(client, ref)

    marcus = client.get(f"/api/runs/{ref}/agents/Marcus/dossier").json()
    assert [d["title"] for d in marcus["documents"]] == []
    assert marcus["document_retrievals"] == []


def test_dossier_is_empty_not_synthesized_when_retrieval_off(client, run_ref):
    """A run without retrieval must report nothing rather than invent material."""
    body = client.get(f"/api/runs/{run_ref}/agents/Dana/dossier").json()
    assert body["documents"] == []
    assert body["document_retrievals"] == []


def test_dossier_shows_cast_wide_documents_to_every_persona(client, run_ref):
    client.post(f"/api/runs/{run_ref}/documents",
                json={"title": "shared.md", "text": "Shared briefing material."})
    for who in ("Dana", "Marcus"):
        body = client.get(f"/api/runs/{run_ref}/agents/{who}/dossier").json()
        assert [d["title"] for d in body["documents"]] == ["shared.md"]
        assert body["documents"][0]["cast_wide"] is True


def test_api_request_contract_carries_documents_and_retrieval(client, tmp_path):
    """Regression: PersonaModel/RunConfigModel must DECLARE the Phase 5 fields.

    Pydantic drops undeclared fields, so an omission here makes cast-level
    attachment and the retrieval config work from the CLI but silently vanish
    through the API. This asserts the round-trip end to end.
    """
    doc = tmp_path / "dana.md"
    doc.write_text(DANA_TEXT)
    request = json.loads(json.dumps(RETRIEVAL_REQUEST))
    request["cast"][0]["documents"] = [str(doc)]

    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=_fake):
        ref = client.post("/api/runs", json=request).json()["run_id"]
        _wait(client, ref)

    # The retrieval config survived onto the stored run...
    stored = client.get(f"/api/runs/{ref}").json()
    assert stored["config"]["retrieval"]["enabled"] is True
    assert stored["config"]["retrieval"]["max_chars"] == 900
    # ...the cast document was ingested...
    assert [d["title"] for d in client.get(f"/api/runs/{ref}/documents").json()["documents"]] == ["dana.md"]
    # ...and it was actually retrieved during the run.
    events = client.get(f"/api/runs/{ref}/events").json()
    kinds = {e["event_type"] for e in events["events"]}
    assert "document.ingested" in kinds
    assert "document.retrieved" in kinds


# --------------------------------------------------------------------------
# SPA cache policy
#
# Not a performance nit — an observed hard failure. Vite emits content-hashed
# bundles and deletes old ones on rebuild. index.html was served with an etag but
# no Cache-Control, so a browser reused a cached shell pointing at a bundle that no
# longer existed: the page hung blank with nothing in the network log to explain it.
# Most likely to bite on upgrade, where a new image sits behind the same URL.
# --------------------------------------------------------------------------


def test_the_html_shell_is_never_cached(client, tmp_path):
    """The shell names the current bundle, so a stale copy names a deleted one."""
    from matrix_studio.api import app as app_mod

    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text('<script src="/assets/index-AAAAAAAA.js"></script>')
    (static / "assets" / "index-AAAAAAAA.js").write_text("console.log(1)")

    with patch.object(app_mod, "STATIC_DIR", static):
        with TestClient(app_mod.create_app(db_path=str(tmp_path / "t.db"))) as c:
            r = c.get("/")
            assert r.status_code == 200
            cc = r.headers.get("cache-control", "")
            assert "no-cache" in cc, f"shell is cacheable: {cc!r}"

            # A deep link falls back to the shell and must carry the same policy.
            deep = c.get("/some/client/route")
            assert "no-cache" in deep.headers.get("cache-control", "")


def test_hashed_assets_are_cached_immutably(client, tmp_path):
    """Safe precisely BECAUSE the filename changes when the content does — and it is
    what makes the no-cache shell cheap rather than a per-load download."""
    from matrix_studio.api import app as app_mod

    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html></html>")
    (static / "assets" / "index-BBBBBBBB.js").write_text("console.log(2)")

    with patch.object(app_mod, "STATIC_DIR", static):
        with TestClient(app_mod.create_app(db_path=str(tmp_path / "t.db"))) as c:
            r = c.get("/assets/index-BBBBBBBB.js")
            assert r.status_code == 200
            assert "immutable" in r.headers.get("cache-control", "")


def test_an_upload_stores_the_original_text_not_a_reassembly(client, run_ref):
    """`text_is_original` must be true on the one path a user actually uses.

    Without passing `text=`, the stored body is `join_chunks(chunks)` — not an exact
    inverse of chunking. Measured on two real documents it shifts 2–7% of chunk
    boundaries, so an `ordinal` can point at different text from the vector built at
    that ordinal, and hybrid retrieval fuses the arms BY chunk id: a passage cited under
    an ordinal that does not contain it.

    The route was omitting it, so every upload wrote `text_is_original: false` — exactly
    the condition the §4a correction exists to avoid, on the only path that matters.
    Found by reading a real API response after deploying.
    """
    resp = client.post(f"/api/runs/{run_ref}/documents",
                       json={"persona_name": "Dana", "title": "a.md", "text": DANA_TEXT})
    assert resp.status_code == 201, resp.text
    docs = client.get(f"/api/runs/{run_ref}/documents").json()["documents"]
    assert docs
    assert docs[0].get("text_is_original") is True, docs[0]


def test_the_documents_response_does_not_leak_storage_keys(client, run_ref):
    """`pk`/`sk`/`s3_key` are how the backend addresses an item, not client data.

    A client that started reading them would be coupled to the key design — and that is
    the part most likely to change, since Phase 6 re-partitions documents by knowledge
    base. `owner_sub` goes too: the caller knows who they are, and echoing a sub back
    would be a disclosure if it were ever the wrong one.
    """
    client.post(f"/api/runs/{run_ref}/documents",
                json={"persona_name": "Dana", "title": "a.md", "text": DANA_TEXT})
    docs = client.get(f"/api/runs/{run_ref}/documents").json()["documents"]
    assert docs
    for leaked in ("pk", "sk", "s3_key", "owner_sub"):
        assert leaked not in docs[0], f"{leaked} reached the client: {docs[0]}"
    # And the fields a client legitimately needs are still there.
    for kept in ("id", "title", "chunk_count", "char_count", "persona_name"):
        assert kept in docs[0], f"{kept} is missing: {docs[0]}"
