# SPDX-License-Identifier: Apache-2.0
"""Phase 6 step 1: knowledge bases, grants, and the authorisation decision.

`docs/PHASE6-KB-DESIGN.md` §2 is the reason this file is separate and thorough. Sharing
introduces the **first exception to §3's tenancy invariant**: a shared KB is read by
someone who does not own it, so access stops being a partition constraint
`dynamodb:LeadingKeys` can enforce and becomes an authorisation *decision*. §8b names
this as the second place after retrieval scoping that deserves a dedicated suite, "including
the negative cases (revoked grant, grant to a group the user has left, binding to a KB the
user never had a grant for)".

The negative cases come first here, deliberately. A permission suite that only proves
access works is the shape of a suite that would pass with the check deleted.
"""

import pytest

from tests.support import TEST_OWNER

pytestmark = pytest.mark.asyncio

OTHER = "someone-else-sub"


# --------------------------------------------------------------------------- #
# The negative cases — what must NOT be readable
# --------------------------------------------------------------------------- #


async def test_a_stranger_cannot_read_a_kb(db):
    kb = await db.create_knowledge_base("migration policy")
    assert await db.may_read_kb(kb["id"], OTHER) is False


async def test_a_revoked_grant_stops_working(db):
    """The plan's own 'done when': revocation takes effect at QUERY time.

    A binding created while the grant existed must stop retrieving. This is the leak
    §8b names, and the only thing that closes it is re-checking per query rather than
    per binding.
    """
    kb = await db.create_knowledge_base("board materials")
    await db.grant_kb(kb["id"], user=OTHER, granted_by=TEST_OWNER)
    assert await db.may_read_kb(kb["id"], OTHER) is True

    await db.revoke_kb(kb["id"], user=OTHER)
    assert await db.may_read_kb(kb["id"], OTHER) is False


async def test_a_group_grant_stops_working_when_the_user_leaves_the_group(db):
    """Modelled as the caller no longer presenting the group.

    Groups come from the verified token, so 'left the group' means the next token omits
    it. The bounded staleness window that implies is stated in the design doc §2.3; what
    this asserts is that presenting no group means no access.
    """
    kb = await db.create_knowledge_base("sre runbooks")
    await db.grant_kb(kb["id"], group="sre", granted_by=TEST_OWNER)
    assert await db.may_read_kb(kb["id"], OTHER, groups=["sre"]) is True
    assert await db.may_read_kb(kb["id"], OTHER, groups=[]) is False
    assert await db.may_read_kb(kb["id"], OTHER) is False


async def test_a_group_grant_does_not_leak_to_a_different_group(db):
    kb = await db.create_knowledge_base("legal")
    await db.grant_kb(kb["id"], group="legal", granted_by=TEST_OWNER)
    assert await db.may_read_kb(kb["id"], OTHER, groups=["marketing"]) is False


async def test_a_group_cannot_impersonate_a_user_by_name(db):
    """The key namespaces are distinct on purpose.

    If user and group principals shared one sort-key space, a Cognito group named after
    somebody's `sub` would silently inherit their grants.
    """
    kb = await db.create_knowledge_base("payroll")
    await db.grant_kb(kb["id"], user=OTHER)
    # A group *named* like the user must not match the user's grant.
    assert await db.may_read_kb(kb["id"], "third-party", groups=[OTHER]) is False


async def test_a_missing_kb_is_not_readable(db):
    """False rather than an exception: 'no such KB' and 'not yours' are the same
    answer to a caller who should not learn which."""
    assert await db.may_read_kb("no-such-kb", TEST_OWNER) is False


async def test_a_grant_on_one_kb_does_not_grant_another(db):
    a = await db.create_knowledge_base("a")
    b = await db.create_knowledge_base("b")
    await db.grant_kb(a["id"], user=OTHER)
    assert await db.may_read_kb(a["id"], OTHER) is True
    assert await db.may_read_kb(b["id"], OTHER) is False


# --------------------------------------------------------------------------- #
# The positive cases — non-vacuity for everything above
# --------------------------------------------------------------------------- #


async def test_the_owner_needs_no_grant_row(db):
    """Without this shortcut every private KB needs a grant to its own creator — a row
    that can be forgotten, failing in a way that looks like a bug in sharing."""
    kb = await db.create_knowledge_base("mine")
    assert await db.list_kb_grants(kb["id"]) == []
    assert await db.may_read_kb(kb["id"], TEST_OWNER) is True


async def test_a_user_grant_permits_a_stranger(db):
    kb = await db.create_knowledge_base("shared")
    await db.grant_kb(kb["id"], user=OTHER, granted_by=TEST_OWNER)
    assert await db.may_read_kb(kb["id"], OTHER) is True


async def test_revoking_one_principal_leaves_the_others(db):
    kb = await db.create_knowledge_base("team")
    await db.grant_kb(kb["id"], user=OTHER)
    await db.grant_kb(kb["id"], user="third")
    await db.revoke_kb(kb["id"], user=OTHER)
    assert await db.may_read_kb(kb["id"], OTHER) is False
    assert await db.may_read_kb(kb["id"], "third") is True


async def test_revoking_a_grant_that_never_existed_is_not_an_error(db):
    """A revoke racing another revoke, or an operator being thorough."""
    kb = await db.create_knowledge_base("k")
    await db.revoke_kb(kb["id"], user="nobody")


# --------------------------------------------------------------------------- #
# searchable_kbs — the intersection, and failing closed
# --------------------------------------------------------------------------- #


async def test_searchable_is_the_intersection_of_bindings_and_grants(db):
    """Binding is not permission. §8b: a grant says MAY read, a binding says DOES read.

    A run bound to a KB whose grant was revoked must retrieve nothing from it — the
    stale-binding leak.
    """
    mine = await db.create_knowledge_base("mine")
    shared = await db.create_knowledge_base("shared", owner_sub=OTHER)
    await db.grant_kb(shared["id"], user=TEST_OWNER)
    forbidden = await db.create_knowledge_base("theirs", owner_sub=OTHER)

    bound = [mine["id"], shared["id"], forbidden["id"]]
    got = await db.searchable_kbs(bound, TEST_OWNER)
    assert got == [mine["id"], shared["id"]], (
        "a KB that is bound but not granted must be excluded"
    )


async def test_searchable_drops_a_kb_after_its_grant_is_revoked(db):
    """The binding is unchanged; only the grant moved. This is the query-time re-check."""
    shared = await db.create_knowledge_base("shared", owner_sub=OTHER)
    await db.grant_kb(shared["id"], user=TEST_OWNER)
    bound = [shared["id"]]
    assert await db.searchable_kbs(bound, TEST_OWNER) == [shared["id"]]

    await db.revoke_kb(shared["id"], user=TEST_OWNER)
    assert await db.searchable_kbs(bound, TEST_OWNER) == [], (
        "the binding still names the KB, so only a query-time check can exclude it"
    )


async def test_searchable_fails_closed(db, monkeypatch):
    """An authorisation failure DENIES; it does not degrade.

    This is the opposite of the rule everywhere else in retrieval, where a failure falls
    back to lexical. The natural `try/except` written for availability would return
    'everything bound' and hand over another tenant's corpus.
    """
    mine = await db.create_knowledge_base("mine")

    async def boom(*_a, **_k):
        raise RuntimeError("dynamodb is having a day")

    monkeypatch.setattr(db, "may_read_kb", boom)
    assert await db.searchable_kbs([mine["id"]], TEST_OWNER) == []


async def test_searchable_preserves_order_and_drops_duplicates(db):
    """Fan-out has to be deterministic, and a KB bound at both run and persona level
    must be queried once rather than twice."""
    a = await db.create_knowledge_base("a")
    b = await db.create_knowledge_base("b")
    got = await db.searchable_kbs([b["id"], a["id"], b["id"]], TEST_OWNER)
    assert got == [b["id"], a["id"]]


async def test_searchable_with_nothing_bound_is_empty(db):
    assert await db.searchable_kbs([], TEST_OWNER) == []
    assert await db.searchable_kbs(None, TEST_OWNER) == []


# --------------------------------------------------------------------------- #
# CRUD and listing
# --------------------------------------------------------------------------- #


async def test_a_kb_records_its_owner_and_a_generated_id(db):
    kb = await db.create_knowledge_base("policy", description="the migration policy")
    assert kb["id"] and kb["owner_sub"] == TEST_OWNER
    assert kb["name"] == "policy" and kb["description"] == "the migration policy"
    # Every declared field is present, absent ones as None — the `_row` contract that
    # exists because a dropped None becomes a KeyError at a subscripting call site.
    for field in ("embedding_model", "document_count", "created_at"):
        assert field in kb


async def test_creating_a_kb_twice_with_one_id_is_refused(db):
    """An unconditional put would overwrite a KB's metadata including its OWNER, which
    is the one field authorisation depends on."""
    await db.create_knowledge_base("first", kb_id="fixed")
    with pytest.raises(Exception):
        await db.create_knowledge_base("second", kb_id="fixed")
    assert (await db.get_knowledge_base("fixed"))["name"] == "first"


async def test_the_embedding_model_is_per_kb_and_starts_unset(db):
    """It was a GLOBAL singleton. Two KBs may hold vectors from different models, and a
    query embedded with one against an index built with the other is nonsense."""
    kb = await db.create_knowledge_base("k")
    assert kb["embedding_model"] is None
    pinned = await db.create_knowledge_base("p", embedding_model="titan-v2")
    assert (await db.get_knowledge_base(pinned["id"]))["embedding_model"] == "titan-v2"


async def test_listing_includes_mine_and_shared_with_me(db):
    """Two reads, because ownership is an attribute and a grant is a row in another
    table — a single query cannot express 'mine or shared with me' across partitions."""
    mine = await db.create_knowledge_base("mine")
    shared = await db.create_knowledge_base("shared", owner_sub=OTHER)
    await db.grant_kb(shared["id"], user=TEST_OWNER)
    await db.create_knowledge_base("invisible", owner_sub=OTHER)

    ids = {k["id"] for k in await db.list_knowledge_bases()}
    assert mine["id"] in ids
    assert shared["id"] in ids
    assert len(ids) == 2, "a KB neither owned nor granted must not be listed"


async def test_listing_does_not_duplicate_a_kb_granted_to_its_owner(db):
    """A redundant self-grant is legal and must not produce two rows."""
    kb = await db.create_knowledge_base("mine")
    await db.grant_kb(kb["id"], user=TEST_OWNER)
    listed = await db.list_knowledge_bases()
    assert [k["id"] for k in listed].count(kb["id"]) == 1


async def test_grants_are_listable_for_an_owner_to_audit(db):
    kb = await db.create_knowledge_base("k")
    await db.grant_kb(kb["id"], user=OTHER, granted_by=TEST_OWNER)
    await db.grant_kb(kb["id"], group="sre", granted_by=TEST_OWNER)
    grants = await db.list_kb_grants(kb["id"])
    assert {(g["kind"], g["principal"]) for g in grants} == {
        ("user", OTHER), ("group", "sre")
    }
    assert all(g["granted_by"] == TEST_OWNER for g in grants)


async def test_a_grant_names_exactly_one_principal(db):
    """Both or neither is a programming error, and a silent default would grant the
    wrong thing."""
    kb = await db.create_knowledge_base("k")
    with pytest.raises(ValueError, match="exactly one"):
        await db.grant_kb(kb["id"])
    with pytest.raises(ValueError, match="exactly one"):
        await db.grant_kb(kb["id"], user="a", group="b")


async def test_kbs_are_not_under_a_user_partition(db):
    """§8b's structural point, asserted so a future 'tidy' cannot undo it.

    Moving KBs under `USER#{sub}` would make sharing inexpressible — the partition a
    reader is pinned to would never contain someone else's KB.
    """
    import boto3

    from tests.support import TEST_TABLE_PREFIX

    kb = await db.create_knowledge_base("k")
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(
        f"{TEST_TABLE_PREFIX}-knowledge-bases"
    )
    item = table.get_item(Key={"pk": f"KB#{kb['id']}", "sk": "META"}).get("Item")
    assert item, "a KB must be keyed KB#{id}/META"
    assert not str(item["pk"]).startswith("USER#")
