# SPDX-License-Identifier: Apache-2.0
"""
Saved cast templates: a named cast to start new conversations from.

Three behaviours are the point:

- **A clash asks, it does not clobber.** Saving over an existing name is a 409 unless the caller
  says to replace it — a template can be an afternoon's work.
- **Pasted documents are not kept, and the response says so.** They belong to one run; a template
  that re-pasted them into every conversation built from it would be a cost nobody chose.
- **Templates are the owner's own.** Another user can neither see, load nor delete one.
"""

import pytest
from fastapi.testclient import TestClient

from matrix_studio.api.app import create_app
from matrix_studio.api.identity import current_user
from tests.support import TEST_OWNER

OTHER = "sub-someone-else-9999"

CAST = [
    {
        "name": "Casey",
        "persona": "a compliance lead",
        "goals": ["keep it lawful"],
        "structured": {"viewpoints": [{"position": "renewals need a fresh review", "firmness": "firm"}]},
        "knowledge_bases": ["kb-statutes"],
        "document_texts": [{"title": "notes.txt", "text": "forty pages"}],
    },
    {"name": "Avery", "persona": "an operations manager"},
]


@pytest.fixture
def app(aws_backend, tmp_path):
    return create_app(db_path=str(tmp_path / "test.db"))


def _as(app, sub):
    app.dependency_overrides[current_user] = lambda: sub
    return TestClient(app)


def test_save_list_load_delete(app):
    with _as(app, TEST_OWNER) as c:
        res = c.post("/api/cast-templates", json={"name": "Board review", "description": "d", "cast": CAST})
        assert res.status_code == 201, res.text
        assert res.json()["dropped_documents"] == 1

        listed = c.get("/api/cast-templates").json()["templates"]
        assert [(t["name"], t["personas"]) for t in listed] == [("Board review", ["Casey", "Avery"])]

        got = c.get("/api/cast-templates/Board review").json()
        casey = got["cast"][0]
        assert casey["knowledge_bases"] == ["kb-statutes"]
        assert casey["structured"]["viewpoints"][0]["position"] == "renewals need a fresh review"
        assert "document_texts" not in casey

        assert c.delete("/api/cast-templates/Board review").json() == {"deleted": "Board review"}
        assert c.get("/api/cast-templates").json()["templates"] == []
        assert c.delete("/api/cast-templates/Board review").status_code == 404


def test_a_clash_is_a_409_unless_overwrite(app):
    with _as(app, TEST_OWNER) as c:
        c.post("/api/cast-templates", json={"name": "Board review", "cast": CAST})
        # Case-folded: "board review" is the same template, not a second one nobody can tell apart.
        res = c.post("/api/cast-templates", json={"name": "board review", "cast": CAST[:1]})
        assert res.status_code == 409
        assert len(c.get("/api/cast-templates/Board review").json()["cast"]) == 2

        res = c.post("/api/cast-templates", json={"name": "board review", "cast": CAST[:1], "overwrite": True})
        assert res.status_code == 201
        assert len(c.get("/api/cast-templates/Board review").json()["cast"]) == 1


def test_another_user_cannot_see_load_or_delete_it(app):
    with _as(app, TEST_OWNER) as c:
        c.post("/api/cast-templates", json={"name": "Mine", "cast": CAST})
    with _as(app, OTHER) as c:
        assert c.get("/api/cast-templates").json()["templates"] == []
        assert c.get("/api/cast-templates/Mine").status_code == 404
        assert c.delete("/api/cast-templates/Mine").status_code == 404
    with _as(app, TEST_OWNER) as c:
        assert c.get("/api/cast-templates/Mine").status_code == 200


@pytest.mark.parametrize("body", [
    {"name": "", "cast": CAST},
    {"name": "a/b", "cast": CAST},
    {"name": "empty", "cast": []},
    {"name": "bad", "cast": [{"name": "X"}]},  # a persona needs its description
])
def test_refuses_what_could_not_start_a_conversation(app, body):
    with _as(app, TEST_OWNER) as c:
        assert c.post("/api/cast-templates", json=body).status_code == 422


def test_a_template_never_appears_as_a_run(app):
    with _as(app, TEST_OWNER) as c:
        c.post("/api/cast-templates", json={"name": "Mine", "cast": CAST})
        assert c.get("/api/runs").json()["runs"] == []
        assert c.get("/api/ensembles").json()["ensembles"] == []
