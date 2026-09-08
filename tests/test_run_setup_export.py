# SPDX-License-Identifier: Apache-2.0
"""
GET /api/runs/{ref}/setup — a run's definition, shaped as a create-run request.

This backs "start a fresh conversation from this one": the setup is loaded into the
new-run form, edited, and submitted as a brand-new root run. Nothing is replayed and
nothing is inherited at runtime, which is what distinguishes it from branching.

The contract worth defending is that the response is *directly re-runnable*. Every
test here either round-trips it through POST /api/runs or covers a case where naive
copying would corrupt it — duplicated document text, an inherited `imported` config,
a cast-wide document quietly reassigned to a persona.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from matrix_studio.api.app import create_app
from matrix_studio.documents import chunk_text, join_chunks


class _Resp:
    def __init__(self, content):
        self.choices = [MagicMock(message=MagicMock(content=content))]
        self.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        self._hidden_params = {"response_cost": 0.0}


def _fake_llm():
    n = {"i": 0}

    def fake(*args, **kwargs):
        n["i"] += 1
        return _Resp("Priya" if n["i"] % 2 == 1 else "A short turn.")

    return fake


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def fake_name(topic, cast_names=None, model=None, name_exists=None):
        return {"name": "migration-debate", "description": "d",
                "slug": "migration-debate", "source": "llm"}

    monkeypatch.setattr("matrix_studio.api.manager.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.api.app.generate_run_name", fake_name)
    monkeypatch.setattr("matrix_studio.branching.generate_run_name", fake_name)
    app = create_app(db_path=str(tmp_path / "setup.db"))
    with TestClient(app) as c:
        yield c


REQUEST = {
    "topic": "Should we ship the migration this quarter?",
    "cast": [
        {
            "name": "Priya",
            "persona": "Staff engineer who owns the migration.",
            "goals": ["Ship without a rollback"],
            "structured": {
                "viewpoints": [
                    {
                        "position": "The cutover must be reversible",
                        "firmness": "non-negotiable",
                        "evidence_that_shifts": ["A tested rollback path"],
                        "underlying_concern": "I was on call for the last one",
                    }
                ],
                "preferences": {"dismisses": ["quarterly targets"]},
            },
        },
        {"name": "Dan", "persona": "Product lead answering to the board.",
         "goals": ["Hit the quarter"]},
    ],
    "config": {"max_messages": 2, "generate_avatars": False,
               "personas": {"enabled": True}},
    "name": "Migration debate",
    "description": "Whether to cut over now.",
}


def _wait(client, ref, tries=400):
    """Runs start in the background; documents are ingested during startup."""
    for _ in range(tries):
        r = client.get(f"/api/runs/{ref}")
        if r.status_code == 200 and r.json()["status"] in ("complete", "failed", "capped"):
            return r.json()
        time.sleep(0.02)
    return client.get(f"/api/runs/{ref}").json()


def _start(client, body=None):
    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_fake_llm()):
        r = client.post("/api/runs", json=body or REQUEST)
        assert r.status_code == 201, r.text
        run_id = r.json()["run_id"]
        # Wait: cast documents are ingested at run start, so reading /setup before
        # the run has got going would test an empty documents table.
        _wait(client, run_id)
        return run_id


def test_setup_is_re_runnable_as_a_fresh_root_run(client):
    """The exported setup is accepted verbatim by POST /api/runs."""
    run_id = _start(client)
    setup = client.get(f"/api/runs/{run_id}/setup").json()["setup"]

    with patch("matrix_studio.engine.simulator.litellm.acompletion",
               side_effect=_fake_llm()):
        again = client.post("/api/runs", json=setup)
    assert again.status_code == 201, again.text

    new_id = again.json()["run_id"]
    assert new_id != run_id
    # A fresh ROOT run: not a branch of the run it was copied from.
    detail = client.get(f"/api/runs/{new_id}").json()
    assert detail["parent_run_id"] is None
    assert detail["branch_turn"] is None


def test_setup_preserves_convictions_including_the_private_concern(client):
    """
    Convictions survive, `underlying_concern` included.

    The concern is withheld from the conversation and from the event log, but it is
    the operator's own authored input — losing it here would silently downgrade
    every persona on a re-run, and the loss would be invisible in the form.
    """
    run_id = _start(client)
    setup = client.get(f"/api/runs/{run_id}/setup").json()["setup"]

    priya = next(c for c in setup["cast"] if c["name"] == "Priya")
    vp = priya["structured"]["viewpoints"][0]
    assert vp["position"] == "The cutover must be reversible"
    assert vp["firmness"] == "non-negotiable"
    assert vp["evidence_that_shifts"] == ["A tested rollback path"]
    assert vp["underlying_concern"] == "I was on call for the last one"
    assert priya["structured"]["preferences"]["dismisses"] == ["quarterly targets"]

    # And the topic / naming / budget come back too.
    assert setup["topic"] == REQUEST["topic"]
    assert setup["config"]["max_messages"] == 2
    # Run naming normalises case, so compare case-insensitively rather than
    # asserting the submitted spelling.
    assert setup["name"].lower() == "migration debate"


def test_documents_come_back_as_text_without_duplicated_overlap(client):
    """
    A persona's document is exported as inline text, reassembled from its chunks.

    Chunks overlap by design, so the naive `"".join(chunks)` repeats ~150 characters
    at every boundary. That would come back as visibly stuttering text and be
    re-ingested that way on the next run, compounding each time.
    """
    # Long enough to force several overlapping chunks.
    paragraphs = [
        f"Paragraph {i}. It says something specific about the migration plan "
        f"and then continues for a while so the chunker has real work to do."
        for i in range(20)
    ]
    original = "\n\n".join(paragraphs)
    assert len(chunk_text(original)) >= 3, "fixture must span multiple chunks"

    body = json.loads(json.dumps(REQUEST))
    body["cast"][0]["document_texts"] = [{"title": "plan.md", "text": original}]
    body["config"]["retrieval"] = {"enabled": True}
    run_id = _start(client, body)

    setup = client.get(f"/api/runs/{run_id}/setup").json()["setup"]
    priya = next(c for c in setup["cast"] if c["name"] == "Priya")
    docs = priya["document_texts"]
    assert len(docs) == 1
    assert docs[0]["title"] == "plan.md"

    text = docs[0]["text"]
    # Every paragraph present exactly once — the real anti-duplication assertion.
    for i in range(20):
        assert text.count(f"Paragraph {i}.") == 1, f"Paragraph {i} duplicated or lost"
    assert len(text) <= len(original) + 8


def test_exported_documents_match_the_table_exactly(client):
    """
    The documents table is the single source, and each document appears once.

    The cast row also still holds the `document_texts` the run was created with, and
    the same documents were ingested into the table at run start. So the export must
    REPLACE rather than merge — appending to the original would attach every inline
    document twice, and each re-run would double it again.
    """
    body = json.loads(json.dumps(REQUEST))
    body["cast"][0]["document_texts"] = [
        {"title": "brief.txt", "text": "One short brief."},
        {"title": "second.txt", "text": "Another short brief."},
    ]
    body["config"]["retrieval"] = {"enabled": True}
    run_id = _start(client, body)

    listed = client.get(f"/api/runs/{run_id}/documents?persona=Priya").json()["documents"]
    expected = sorted(d["title"] for d in listed)
    assert len(expected) == 2, listed

    setup = client.get(f"/api/runs/{run_id}/setup").json()["setup"]
    priya = next(c for c in setup["cast"] if c["name"] == "Priya")
    assert sorted(d["title"] for d in priya["document_texts"]) == expected
    # `documents` holds SERVER-side paths, which a browser can neither read nor
    # resubmit; leaking them would put unusable entries in the form.
    assert "documents" not in priya


def test_cast_wide_documents_are_reported_not_reassigned(client):
    """
    A cast-wide document warns instead of being attached to someone.

    `document_texts` is per-persona only, so there is nowhere in a create-run request
    to put a shared document. Attaching it to the first persona would silently change
    who can retrieve it — a scoping change disguised as a copy.
    """
    run_id = _start(client)
    # Upload with no persona -> cast-wide.
    r = client.post(f"/api/runs/{run_id}/documents",
                    json={"title": "board-memo.txt", "text": "Shared context for everyone."})
    assert r.status_code == 201, r.text

    payload = client.get(f"/api/runs/{run_id}/setup").json()
    assert any("board-memo.txt" in w for w in payload["warnings"])
    assert any("cast-wide" in w for w in payload["warnings"])
    # Nobody silently acquired it.
    for member in payload["setup"]["cast"]:
        titles = [d["title"] for d in member.get("document_texts", [])]
        assert "board-memo.txt" not in titles


def test_branch_only_config_is_not_replayed_into_a_fresh_run(client):
    """
    Config is filtered to the create-run contract.

    A branch's config carries `mutation`/`imported` describing how that run was
    derived. Copying those into a fresh root run would assert a history it does not
    have — so only the declared setup keys survive.
    """
    run_id = _start(client)
    r = client.post(f"/api/runs/{run_id}/branch",
                    json={"from_turn": 0, "mutation": {"kind": "continue", "add_budget": 1}})
    assert r.status_code == 201, r.text
    branch_id = r.json()["run_id"]

    raw_config = client.get(f"/api/runs/{branch_id}").json()["config"]
    setup = client.get(f"/api/runs/{branch_id}/setup").json()["setup"]

    allowed = {"max_messages", "generate_avatars", "cognition", "retrieval", "personas"}
    assert set(setup["config"]) <= allowed
    # Guard against the test passing because the branch config was empty anyway.
    assert set(raw_config) - allowed, "fixture should have branch-only config keys"


def test_missing_run_is_404(client):
    assert client.get("/api/runs/nope/setup").status_code == 404


def test_join_chunks_is_the_exact_inverse_of_chunk_text():
    """Round-trip at several sizes, including one where overlap must be discovered."""
    text = "\n\n".join(
        f"Section {i}. Sentence one is here. Sentence two follows on. "
        f"Sentence three closes the section out."
        for i in range(15)
    )
    for chunk_chars, overlap in ((900, 150), (300, 100), (200, 20), (5000, 150)):
        chunks = chunk_text(text, chunk_chars=chunk_chars, overlap=overlap)
        rebuilt = join_chunks([c.content for c in chunks])
        assert rebuilt == text, f"round-trip failed at {chunk_chars}/{overlap}"


def test_join_chunks_never_loses_text_on_unfamiliar_chunks():
    """
    Chunks from some other source concatenate rather than being trimmed.

    The failure to avoid is silent deletion: guessing an overlap that was not there
    would drop real content, which is worse than repeating a little.
    """
    assert join_chunks(["alpha", "beta"]) == "alpha\n\nbeta"
    assert join_chunks([]) == ""
    assert join_chunks(["only"]) == "only"
