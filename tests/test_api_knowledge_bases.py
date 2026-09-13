# SPDX-License-Identifier: Apache-2.0
"""
The knowledge-base API — the routes that make Phase 6 reachable by a user.

Until these existed a KB could only be created by a script: storage, retrieval,
bindings, grants and the migration all worked and none of it was usable.

**Every route here is an authorisation decision, and there are two different ones.**
Getting them confused is the whole risk:

    READ  — `may_read_kb`: the owner, or a principal with a grant.
    WRITE — ownership ALONE. A grant says "may read" and §8b defines no other kind.

The natural bug is to reuse the read check for writes, because it is right there and it
returns True for the caller who is about to write. That would make "shared with me" mean
"mine to edit" — a grantee could add documents to someone else's collection, or delete
theirs, and the owner would have no way to find out. So the negative cases come first and
outnumber the positive ones, as in `test_knowledge_bases.py`.

A 404 rather than a 403 throughout, matching the run routes: distinguishing "exists but
not yours" from "does not exist" leaks the existence of other people's collections.
"""

import pytest
from fastapi.testclient import TestClient

from matrix_studio.api.app import create_app
from tests.support import TEST_OWNER

pytestmark = pytest.mark.asyncio

OTHER = "sub-other-user-8888"


@pytest.fixture(autouse=True)
def _storage_backend(aws_backend):
    """The app's lifespan connects to DynamoDB; without this it reaches real AWS."""


@pytest.fixture(autouse=True)
def _mock_embedder(monkeypatch):
    """No billable embedding calls, at the index's real width.

    `TEST_VECTOR_DIM` is 1024, the same as `EMBEDDING_DIMENSION`, so these vectors are
    the width `ensure_kb_index` creates a KB index at — a narrower fixture would be
    refused by the service and would only be caught in production. Direction varies with
    the text so two documents are not identical vectors.
    """
    from unittest.mock import patch

    from tests.support import unit_vector

    async def fake_embedding(model=None, input=None, **kwargs):
        texts = input if isinstance(input, list) else [input]
        return type(
            "R",
            (),
            {
                "data": [
                    {"embedding": unit_vector(1.0, (hash(t) % 97) / 100.0)} for t in texts
                ],
                "usage": type("U", (), {"prompt_tokens": 7})(),
            },
        )()

    with patch("litellm.aembedding", side_effect=fake_embedding), \
         patch("litellm.completion_cost", return_value=0.0000001):
        yield


@pytest.fixture
def client(monkeypatch):
    """A client authenticated as TEST_OWNER — the same identity the `db` fixture binds.

    This matters and was got wrong once already: a client resolving a DIFFERENT sub from
    the `db` fixture's owner made rejection tests pass for the wrong reason. Everything
    below is asserted against a single identity, and the second user is introduced only
    through the API.
    """
    monkeypatch.setenv("AUTH_MODE", "single-user")
    monkeypatch.setattr(
        "matrix_studio.api.identity.LOCAL_USER_SUB", TEST_OWNER, raising=False
    )
    from matrix_studio.api import identity

    monkeypatch.setattr(identity, "LOCAL_USER_SUB", TEST_OWNER, raising=False)
    app = create_app()
    with TestClient(app) as c:
        yield c


def _as(client, sub, groups=None):
    """Override the identity dependencies for one call, the way the JWT path would."""
    from matrix_studio.api import identity

    client.app.dependency_overrides[identity.current_user] = lambda: sub
    client.app.dependency_overrides[identity.current_groups] = lambda: list(groups or [])
    return client


def _create(client, name="policies", description=None):
    body = {"name": name}
    if description:
        body["description"] = description
    response = client.post("/api/knowledge-bases", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Writes require OWNERSHIP, not read access
# --------------------------------------------------------------------------- #


class TestWritesRequireOwnership:
    def test_a_grantee_cannot_add_a_document(self, client):
        """The central negative. `may_read_kb` returns True for this caller, so a route
        that used the read check would accept this."""
        kb = _create(client, "owners-corpus")
        client.post(
            f"/api/knowledge-bases/{kb['id']}/grants", json={"user": OTHER}
        ).raise_for_status()

        _as(client, OTHER)
        response = client.post(
            f"/api/knowledge-bases/{kb['id']}/documents",
            json={"title": "sneaked-in.md", "text": "material the owner did not add"},
        )
        assert response.status_code == 404, response.text

    def test_a_grantee_cannot_delete_a_document(self, client):
        kb = _create(client, "owners-corpus-2")
        created = client.post(
            f"/api/knowledge-bases/{kb['id']}/documents",
            json={"title": "policy.md", "text": "egress inspection at the border " * 20},
        )
        assert created.status_code == 201, created.text
        doc_id = created.json()["document_id"]
        client.post(
            f"/api/knowledge-bases/{kb['id']}/grants", json={"user": OTHER}
        ).raise_for_status()

        _as(client, OTHER)
        response = client.delete(
            f"/api/knowledge-bases/{kb['id']}/documents/{doc_id}"
        )
        assert response.status_code == 404

        # And it really is still there.
        _as(client, TEST_OWNER)
        detail = client.get(f"/api/knowledge-bases/{kb['id']}").json()
        assert [d["id"] for d in detail["documents"]] == [doc_id]

    def test_a_grantee_cannot_re_grant(self, client):
        """Read access spreading without the owner's knowledge, with no revocation path
        that could then find it all."""
        kb = _create(client, "owners-corpus-3")
        client.post(
            f"/api/knowledge-bases/{kb['id']}/grants", json={"user": OTHER}
        ).raise_for_status()

        _as(client, OTHER)
        response = client.post(
            f"/api/knowledge-bases/{kb['id']}/grants", json={"user": "sub-a-third-party"}
        )
        assert response.status_code == 404

    def test_a_grantee_cannot_revoke(self, client):
        kb = _create(client, "owners-corpus-4")
        client.post(
            f"/api/knowledge-bases/{kb['id']}/grants", json={"user": OTHER}
        ).raise_for_status()

        _as(client, OTHER)
        response = client.delete(
            f"/api/knowledge-bases/{kb['id']}/grants?user={OTHER}"
        )
        assert response.status_code == 404

    def test_a_stranger_cannot_write_either(self, client):
        kb = _create(client, "private-corpus")
        _as(client, OTHER)
        for call in (
            lambda: client.post(
                f"/api/knowledge-bases/{kb['id']}/documents",
                json={"title": "t", "text": "x" * 200},
            ),
            lambda: client.delete(f"/api/knowledge-bases/{kb['id']}/documents/anything"),
            lambda: client.post(
                f"/api/knowledge-bases/{kb['id']}/grants", json={"user": "x"}
            ),
        ):
            assert call().status_code == 404


# --------------------------------------------------------------------------- #
# Reads require a grant
# --------------------------------------------------------------------------- #


class TestReadsRequireAGrant:
    def test_a_stranger_gets_404_not_403(self, client):
        """403 would confirm the collection exists."""
        kb = _create(client, "private")
        _as(client, OTHER)
        assert client.get(f"/api/knowledge-bases/{kb['id']}").status_code == 404

    def test_a_stranger_does_not_see_it_in_the_listing(self, client):
        _create(client, "private-listing")
        _as(client, OTHER)
        listed = client.get("/api/knowledge-bases").json()
        assert listed["knowledge_bases"] == []
        assert listed["count"] == 0

    def test_a_grant_makes_it_readable_and_marked_shared(self, client):
        kb = _create(client, "shareable")
        client.post(
            f"/api/knowledge-bases/{kb['id']}/grants", json={"user": OTHER}
        ).raise_for_status()

        _as(client, OTHER)
        detail = client.get(f"/api/knowledge-bases/{kb['id']}")
        assert detail.status_code == 200
        assert detail.json()["shared"] is True

        listed = client.get("/api/knowledge-bases").json()
        assert [k["id"] for k in listed["knowledge_bases"]] == [kb["id"]]
        assert listed["knowledge_bases"][0]["shared"] is True

    def test_a_grantee_is_not_shown_who_else_it_is_shared_with(self, client):
        """Grants are made against subs, so the recipient list discloses other users'
        identifiers. The owner's business, not a grantee's."""
        kb = _create(client, "multi-shared")
        for principal in (OTHER, "sub-third-party-7777"):
            client.post(
                f"/api/knowledge-bases/{kb['id']}/grants", json={"user": principal}
            ).raise_for_status()

        owner_view = client.get(f"/api/knowledge-bases/{kb['id']}").json()
        assert len(owner_view["grants"]) == 2

        _as(client, OTHER)
        grantee_view = client.get(f"/api/knowledge-bases/{kb['id']}").json()
        assert grantee_view["grants"] is None
        assert client.get(f"/api/knowledge-bases/{kb['id']}/grants").status_code == 404

    def test_a_group_grant_is_honoured(self, client):
        kb = _create(client, "team-corpus")
        client.post(
            f"/api/knowledge-bases/{kb['id']}/grants", json={"group": "platform"}
        ).raise_for_status()

        _as(client, OTHER, groups=["platform"])
        assert client.get(f"/api/knowledge-bases/{kb['id']}").status_code == 200

        _as(client, OTHER, groups=["some-other-team"])
        assert client.get(f"/api/knowledge-bases/{kb['id']}").status_code == 404

    def test_revoking_takes_effect_immediately(self, client):
        kb = _create(client, "revocable")
        client.post(
            f"/api/knowledge-bases/{kb['id']}/grants", json={"user": OTHER}
        ).raise_for_status()
        _as(client, OTHER)
        assert client.get(f"/api/knowledge-bases/{kb['id']}").status_code == 200

        _as(client, TEST_OWNER)
        client.delete(
            f"/api/knowledge-bases/{kb['id']}/grants?user={OTHER}"
        ).raise_for_status()

        _as(client, OTHER)
        assert client.get(f"/api/knowledge-bases/{kb['id']}").status_code == 404

    def test_a_missing_kb_is_404_for_its_would_be_owner_too(self, client):
        assert client.get("/api/knowledge-bases/does-not-exist").status_code == 404


# --------------------------------------------------------------------------- #
# Creating and listing
# --------------------------------------------------------------------------- #


class TestCreateAndList:
    def test_the_owner_is_the_caller_and_cannot_be_supplied(self, client):
        """A body-supplied owner would let a request create a collection attributed to
        someone else — and then only that someone could manage it."""
        response = client.post(
            "/api/knowledge-bases",
            json={"name": "attributed", "owner_sub": "sub-somebody-else"},
        )
        assert response.status_code == 201
        assert response.json()["owner_sub"] == TEST_OWNER

    def test_a_blank_name_is_refused(self, client):
        assert client.post("/api/knowledge-bases", json={"name": "   "}).status_code == 422

    def test_a_new_kb_is_owned_not_shared_and_empty(self, client):
        kb = _create(client, "fresh", description="the description")
        assert kb["shared"] is False
        assert kb["document_count"] == 0
        assert kb["description"] == "the description"
        # `embedding_model` is None until the first write, so that two KBs may hold
        # vectors from different models.
        assert kb.get("embedding_model") is None

    def test_owned_collections_sort_before_shared_ones(self, client):
        theirs = _create(client, "theirs")
        client.post(
            f"/api/knowledge-bases/{theirs['id']}/grants", json={"user": OTHER}
        ).raise_for_status()
        _as(client, OTHER)
        mine = _create(client, "mine")

        listed = client.get("/api/knowledge-bases").json()["knowledge_bases"]
        assert [k["id"] for k in listed] == [mine["id"], theirs["id"]]

    def test_the_document_count_is_reported(self, client):
        kb = _create(client, "counted")
        client.post(
            f"/api/knowledge-bases/{kb['id']}/documents",
            json={"title": "a.md", "text": "egress inspection " * 40},
        ).raise_for_status()
        listed = client.get("/api/knowledge-bases").json()["knowledge_bases"]
        assert listed[0]["document_count"] == 1


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


class TestDocuments:
    def test_a_document_is_stored_and_embedded_in_one_call(self, client):
        """A stored-but-unembedded document is listed and permanently unretrievable,
        which reads as retrieval being bad rather than an upload half-failing."""
        kb = _create(client, "embedded")
        response = client.post(
            f"/api/knowledge-bases/{kb['id']}/documents",
            json={"title": "policy.md", "text": "egress inspection at the border " * 30},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["embedded"] >= 1
        assert body["model"]

    def test_an_embedding_failure_is_a_502_naming_the_stored_document(self, client, monkeypatch):
        """Not a 201 with `embedded: 0`. The document IS stored — deleting it here would
        be worse, since the text may be the operator's only copy — so the id is in the
        error so it can be removed deliberately."""
        kb = _create(client, "unembeddable")

        async def failing(*args, **kwargs):
            return {"embedded": 0, "error": "the KB was indexed with another model"}

        monkeypatch.setattr("matrix_studio.api.app.embed_pending_kb_chunks", failing)
        response = client.post(
            f"/api/knowledge-bases/{kb['id']}/documents",
            json={"title": "policy.md", "text": "x " * 200},
        )
        assert response.status_code == 502
        assert "another model" in response.json()["detail"]
        # The id is in the message, so the half-finished upload is findable.
        detail = client.get(f"/api/knowledge-bases/{kb['id']}").json()
        assert detail["documents"], "the document was rolled back, losing the text"
        assert detail["documents"][0]["id"] in response.json()["detail"]

    def test_blank_text_is_refused(self, client):
        kb = _create(client, "blank")
        response = client.post(
            f"/api/knowledge-bases/{kb['id']}/documents",
            json={"title": "empty.md", "text": "   "},
        )
        assert response.status_code == 422

    def test_text_over_the_limit_is_refused(self, client):
        kb = _create(client, "toolong")
        from matrix_studio.settings import get_settings

        limit = get_settings().max_document_chars
        response = client.post(
            f"/api/knowledge-bases/{kb['id']}/documents",
            json={"title": "big.md", "text": "x" * (limit + 1)},
        )
        assert response.status_code == 422
        assert str(limit) in response.json()["detail"]

    def test_deleting_a_document_removes_it_from_the_collection(self, client):
        kb = _create(client, "deletable")
        doc_id = client.post(
            f"/api/knowledge-bases/{kb['id']}/documents",
            json={"title": "policy.md", "text": "egress inspection " * 40},
        ).json()["document_id"]

        assert client.delete(
            f"/api/knowledge-bases/{kb['id']}/documents/{doc_id}"
        ).status_code == 200
        assert client.get(f"/api/knowledge-bases/{kb['id']}").json()["documents"] == []

    def test_deleting_an_absent_document_is_404(self, client):
        kb = _create(client, "empty-delete")
        assert client.delete(
            f"/api/knowledge-bases/{kb['id']}/documents/nope"
        ).status_code == 404

    def test_a_document_added_to_one_kb_is_not_in_another(self, client):
        """A document belongs to exactly one collection (§8b)."""
        first = _create(client, "first")
        second = _create(client, "second")
        client.post(
            f"/api/knowledge-bases/{first['id']}/documents",
            json={"title": "a.md", "text": "egress " * 60},
        ).raise_for_status()

        assert client.get(f"/api/knowledge-bases/{second['id']}").json()["documents"] == []


# --------------------------------------------------------------------------- #
# Grants
# --------------------------------------------------------------------------- #


class TestGrants:
    def test_exactly_one_principal_is_required(self, client):
        kb = _create(client, "grants")
        both = client.post(
            f"/api/knowledge-bases/{kb['id']}/grants",
            json={"user": "u", "group": "g"},
        )
        assert both.status_code == 422
        neither = client.post(f"/api/knowledge-bases/{kb['id']}/grants", json={})
        assert neither.status_code == 422

    def test_revoking_requires_exactly_one_principal(self, client):
        kb = _create(client, "grants-2")
        assert client.delete(
            f"/api/knowledge-bases/{kb['id']}/grants?user=u&group=g"
        ).status_code == 422
        assert client.delete(
            f"/api/knowledge-bases/{kb['id']}/grants"
        ).status_code == 422

    def test_a_grant_records_who_made_it(self, client):
        """`granted_by` is the caller, not a field the body can set: an audit trail a
        request can write is not an audit trail."""
        kb = _create(client, "audited")
        grant = client.post(
            f"/api/knowledge-bases/{kb['id']}/grants",
            json={"user": OTHER, "granted_by": "sub-someone-else"},
        ).json()
        assert grant["granted_by"] == TEST_OWNER


# --------------------------------------------------------------------------- #
# The two defects found by calling the deployed route
# --------------------------------------------------------------------------- #


class TestNoScanAndGroupListing:
    """Both of these shipped and both were caught only on the live stack.

    `moto` does not evaluate IAM and `verify_kb_grants.py` drove the storage layer with
    ADMIN credentials, so nothing before the deploy touched the path a user's request
    actually takes: through the tenant role, whose session policy grants
    `dynamodb:*Item` and `dynamodb:Query` and deliberately never `Scan`.
    """

    def test_listing_never_scans(self, client, monkeypatch):
        """A Scan here is a 500 for every real user, and a correctness issue besides.

        The session policy withholds `Scan` on purpose: a tenant able to Scan the
        knowledge-bases or kb-grants tables could read every other tenant's collections
        and every grant in the system. So the fix was to change the query, not the
        policy — and this is the assertion that keeps it changed. It reproduces the IAM
        refusal locally, which is the only way the suite can see it at all.
        """
        from matrix_studio.storage.dynamo import DynamoStorage

        async def refused(self, table, **kwargs):
            raise AssertionError(
                f"Scan on {table!r}: the tenant session policy grants no dynamodb:Scan, "
                "so this is a 500 for every authenticated user."
            )

        monkeypatch.setattr(DynamoStorage, "_scan_all", refused)

        kb = _create(client, "queried-not-scanned")
        client.post(
            f"/api/knowledge-bases/{kb['id']}/grants", json={"user": OTHER}
        ).raise_for_status()

        assert client.get("/api/knowledge-bases").status_code == 200
        _as(client, OTHER)
        listed = client.get("/api/knowledge-bases")
        assert listed.status_code == 200
        assert [k["id"] for k in listed.json()["knowledge_bases"]] == [kb["id"]]

    def test_a_group_granted_kb_appears_in_the_LISTING(self, client):
        """The gap the Scan hid.

        `_granted_kb_ids` filtered `principal = {sub}`, so a KB shared with a group never
        appeared in any member's listing — while `may_read_kb` would happily open it by
        id. A collection shared with a team that nobody on the team could find. The
        existing group test checked the DETAIL route, which is why it passed.
        """
        kb = _create(client, "team-listing")
        client.post(
            f"/api/knowledge-bases/{kb['id']}/grants", json={"group": "platform"}
        ).raise_for_status()

        _as(client, OTHER, groups=["platform"])
        listed = client.get("/api/knowledge-bases").json()
        assert [k["id"] for k in listed["knowledge_bases"]] == [kb["id"]]
        assert listed["knowledge_bases"][0]["shared"] is True

    def test_a_non_member_still_sees_nothing(self, client):
        kb = _create(client, "team-listing-2")
        client.post(
            f"/api/knowledge-bases/{kb['id']}/grants", json={"group": "platform"}
        ).raise_for_status()

        _as(client, OTHER, groups=["a-different-team"])
        assert client.get("/api/knowledge-bases").json()["knowledge_bases"] == []

    def test_a_kb_granted_both_ways_is_listed_once(self, client):
        """Two grants, one collection. Without de-duplication it would appear twice and
        the count would disagree with the rows."""
        kb = _create(client, "both-ways")
        for body in ({"user": OTHER}, {"group": "platform"}):
            client.post(
                f"/api/knowledge-bases/{kb['id']}/grants", json=body
            ).raise_for_status()

        _as(client, OTHER, groups=["platform"])
        listed = client.get("/api/knowledge-bases").json()
        assert [k["id"] for k in listed["knowledge_bases"]] == [kb["id"]]
        assert listed["count"] == 1


class TestTheVectorIndex:
    """Creating the index is privileged, and it happens at KB creation.

    Found by calling the deployed route: adding a document returned 500 because neither
    role could create the KB's index. Every `s3vectors` grant in the stack went to the
    TENANT role, and index creation had only ever run in scripts with Admin credentials.

    The fix was not to grant the tenant role `CreateIndex`. The ceiling is 10,000 indexes
    per vector bucket, so a tenant able to create them could exhaust the install's whole
    capacity for collections — so creation moved to the API function's own role, and the
    tenant role gained only `GetIndex` (which `ensure_kb_index` needs for its check).
    """

    def _indexes(self, db):
        import os

        return {
            i["indexName"]
            for i in db._vectors_client().list_indexes(
                vectorBucketName=os.environ["VECTOR_BUCKET"]
            )["indexes"]
        }

    def test_creating_a_kb_creates_its_index(self, client):
        """Eagerly, not on first upload: a collection whose index appears only when a
        document is added has a window where it exists and cannot be written to, and the
        failure surfaces as a 502 on the upload rather than at creation."""
        from matrix_studio.storage import Database
        from matrix_studio.storage.vectors import kb_index_name
        from tests.support import TEST_DATA_BUCKET, TEST_TABLE_PREFIX

        db = Database(
            table_prefix=TEST_TABLE_PREFIX, bucket=TEST_DATA_BUCKET, region="us-east-1"
        )
        kb = _create(client, "with-an-index")
        assert kb_index_name(kb["id"], TEST_TABLE_PREFIX) in self._indexes(db)

    def test_the_TENANT_role_cannot_create_an_index(self, client, monkeypatch):
        """The property the stack change encodes, reproduced locally.

        `moto` does not evaluate IAM, so an index creation attempted with tenant-scoped
        credentials succeeds in the suite and fails in the deployment. This fake supplies
        the missing asymmetry: a store BOUND to an owner (`for_owner(...)`, which assumes
        the tenant role) is refused `create_index`, exactly as the deployed policy does,
        while the unscoped store keeps the real client.

        Without this the healing test below passed with the fix removed — `store_kb_vectors`
        calls `ensure_kb_index` itself and moto let it through, so the route-level ensure
        was invisible. That mutant survived until this fake existed.
        """
        from botocore.exceptions import ClientError

        from matrix_studio.storage.dynamo import DynamoStorage
        from matrix_studio.storage.vectors import kb_index_name
        from tests.support import TEST_DATA_BUCKET, TEST_TABLE_PREFIX

        real = DynamoStorage._vectors_client

        def refuse_create_when_bound(self):
            client = real(self)
            if not getattr(self, "_owner_sub", None):
                return client

            class TenantScoped:
                def __getattr__(inner, name):
                    if name == "create_index":
                        def denied(**kwargs):
                            raise ClientError(
                                {"Error": {"Code": "AccessDeniedException", "Message":
                                           "not authorized to perform: "
                                           "s3vectors:CreateIndex"}},
                                "CreateIndex",
                            )
                        return denied
                    return getattr(client, name)

            return TenantScoped()

        monkeypatch.setattr(DynamoStorage, "_vectors_client", refuse_create_when_bound)

        db = DynamoStorage(
            table_prefix=TEST_TABLE_PREFIX, bucket=TEST_DATA_BUCKET, region="us-east-1"
        )
        # Creation still succeeds, because the route uses the function's OWN credentials.
        kb = _create(client, "tenant-cannot-create")
        assert kb_index_name(kb["id"], TEST_TABLE_PREFIX) in self._indexes(db)

        # And the upload works, because the index already exists by then.
        response = client.post(
            f"/api/knowledge-bases/{kb['id']}/documents",
            json={"title": "policy.md", "text": "egress inspection " * 40},
        )
        assert response.status_code == 201, response.text

    def test_a_kb_whose_index_is_missing_is_healed_on_upload(self, client, monkeypatch):
        """A KB created before the index was made eagerly, or one whose creation
        half-failed, must not be permanently unwritable — under the deployed policy the
        tenant role cannot create the index, so nothing else could rescue it.

        The same tenant fake as above, or this passes with the fix removed.
        """
        from botocore.exceptions import ClientError
        from unittest.mock import patch as mock_patch

        from matrix_studio.storage.dynamo import DynamoStorage
        from matrix_studio.storage.vectors import kb_index_name
        from tests.support import TEST_DATA_BUCKET, TEST_TABLE_PREFIX

        real = DynamoStorage._vectors_client

        def refuse_create_when_bound(self):
            client = real(self)
            if not getattr(self, "_owner_sub", None):
                return client

            class TenantScoped:
                def __getattr__(inner, name):
                    if name == "create_index":
                        def denied(**kwargs):
                            raise ClientError(
                                {"Error": {"Code": "AccessDeniedException",
                                           "Message": "s3vectors:CreateIndex"}},
                                "CreateIndex",
                            )
                        return denied
                    return getattr(client, name)

            return TenantScoped()

        db = DynamoStorage(
            table_prefix=TEST_TABLE_PREFIX, bucket=TEST_DATA_BUCKET, region="us-east-1"
        )

        # A KB created with the index step disabled, reproducing the older state. A scoped
        # `patch` rather than `monkeypatch.undo()`: undo reverts EVERY monkeypatch in
        # scope, including the `client` fixture's identity patches, so the caller stopped
        # being the owner and the upload 404'd for the wrong reason.
        async def skip(*args, **kwargs):
            return False

        with mock_patch("matrix_studio.storage.vectors.ensure_kb_index", skip):
            kb = _create(client, "unindexed")
        assert kb_index_name(kb["id"], TEST_TABLE_PREFIX) not in self._indexes(db)

        monkeypatch.setattr(DynamoStorage, "_vectors_client", refuse_create_when_bound)
        response = client.post(
            f"/api/knowledge-bases/{kb['id']}/documents",
            json={"title": "policy.md", "text": "egress inspection " * 40},
        )
        assert response.status_code == 201, response.text
        assert kb_index_name(kb["id"], TEST_TABLE_PREFIX) in self._indexes(db)


class TestTheDocumentCount:
    def test_the_detail_route_counts_rather_than_reading_a_stale_field(self, client):
        """`document_count` was a stored attribute nothing incremented, so it read 0 for
        ever — and the deployed detail route returned that 0 beside a list of one
        document. A derived value no write path maintains can only be wrong."""
        kb = _create(client, "counted-detail")
        client.post(
            f"/api/knowledge-bases/{kb['id']}/documents",
            json={"title": "a.md", "text": "egress inspection " * 40},
        ).raise_for_status()

        detail = client.get(f"/api/knowledge-bases/{kb['id']}").json()
        assert len(detail["documents"]) == 1
        assert detail["document_count"] == 1, "the count disagrees with the list"

    def test_the_list_and_detail_counts_agree(self, client):
        kb = _create(client, "agreeing")
        for title in ("a.md", "b.md"):
            client.post(
                f"/api/knowledge-bases/{kb['id']}/documents",
                json={"title": title, "text": f"egress {title} " * 40},
            ).raise_for_status()

        listed = client.get("/api/knowledge-bases").json()["knowledge_bases"][0]
        detail = client.get(f"/api/knowledge-bases/{kb['id']}").json()
        assert listed["document_count"] == detail["document_count"] == 2
