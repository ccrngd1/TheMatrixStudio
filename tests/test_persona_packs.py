# SPDX-License-Identifier: Apache-2.0
"""
The persona-pack library. What is pinned: every pack is a valid cast member of the real request model
(so it cannot fail at launch), every position carries its firmness and what would move it, packs are
labelled unqualified, and a pack is usable in a cast alongside the others (unique names).
"""

import pytest
from fastapi.testclient import TestClient

from matrix_studio import persona_packs as pp
from matrix_studio.api.app import CreateRunModel, create_app
from matrix_studio.api.identity import current_user
from tests.support import TEST_OWNER


@pytest.mark.parametrize("pack", pp.PACKS, ids=[p["id"] for p in pp.PACKS])
def test_every_pack_is_a_valid_cast_member(pack):
    CreateRunModel(topic="any brief", cast=[pack["persona"]])


@pytest.mark.parametrize("pack", pp.PACKS, ids=[p["id"] for p in pp.PACKS])
def test_every_position_says_how_firm_it_is_and_what_would_move_it(pack):
    vps = pack["persona"]["structured"]["viewpoints"]
    assert vps
    for v in vps:
        assert v["firmness"] in {"negotiable", "firm", "non-negotiable"}
        assert v["evidence_that_shifts"], f"{pack['id']}: a position with no exit is a wall"


def test_all_packs_fit_in_one_cast_and_have_unique_ids():
    names = [p["persona"]["name"] for p in pp.PACKS]
    assert len(set(names)) == len(names)
    assert len({p["id"] for p in pp.PACKS}) == len(pp.PACKS)
    CreateRunModel(topic="any brief", cast=[p["persona"] for p in pp.PACKS])


def test_the_route_lists_every_pack_labelled_unqualified(aws_backend, tmp_path):
    app = create_app(db_path=str(tmp_path / "t.db"))
    app.dependency_overrides[current_user] = lambda: TEST_OWNER
    with TestClient(app) as c:
        body = c.get("/api/persona-packs").json()
    assert [p["id"] for p in body["packs"]] == [p["id"] for p in pp.PACKS]
    assert all(p["qualification"] == "not yet qualified" for p in body["packs"])


def test_a_caller_cannot_edit_the_library():
    got = pp.list_packs()
    got[0]["persona"]["name"] = "changed"
    assert pp.PACKS[0]["persona"]["name"] != "changed"
