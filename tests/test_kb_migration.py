# SPDX-License-Identifier: Apache-2.0
"""
Phase 6 step 6 — the migration script, exercised before it touches the live account.

A migration script tested only by running it on real data has no test; it has an
outcome. These run the real functions from `scripts/migrate_documents_to_kbs.py` against
`moto`, which implements `CreateIndex`, `PutVectors` and `ListVectors` — everything this
script uses. (`QueryVectors` is the one it does not implement, and this script never
queries.)

The properties that matter are not "it copies vectors". They are:

- an item that is **not a document** must not have a knowledge base minted for it, and an
  UNRECOGNISED one must stop the run rather than be skipped;
- the mapping must preserve persona scoping, or a persona-scoped document silently
  becomes cast-wide — a disclosure, not a bug;
- two tenants' documents must never land in one KB, which is why the owner is part of the
  grouping key;
- it must be re-runnable, because a migration that half-completes will be re-run;
- a dry run must write **nothing**.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from matrix_studio.storage import vectors as vecmod
from tests.support import TEST_OWNER, unit_vector

pytestmark = pytest.mark.asyncio

OTHER_USER = "sub-other-tenant-4321"


def _load_script():
    """Import the script by path — `scripts/` is not a package."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "migrate_documents_to_kbs.py"
    spec = importlib.util.spec_from_file_location("migrate_documents_to_kbs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migrate_script = _load_script()


async def _document(db, run_id, title, chunks, *, persona=None, owner=TEST_OWNER):
    bound = db.for_owner(owner)
    # Only if absent: two documents on one run is the case that produces two KBs, and
    # `create_run` refuses a duplicate name.
    if not await bound.get_run(run_id):
        await bound.create_run(run_id=run_id, topic="t", cast=[{"name": "Ada"}], name=run_id)
    return await bound.add_document(
        run_id=run_id, title=title, chunks=chunks, text="\n\n".join(chunks),
        persona_name=persona,
    )


async def _store_vector(db, run_id, doc_id, ordinal, text, *, persona=None, owner=TEST_OWNER):
    """One vector in the shared index, exactly as `store_chunk_vectors` writes it."""
    import os

    bound = db.for_owner(owner)
    meta = {
        "owner_sub": owner,
        "run_id": run_id,
        "document_id": doc_id,
        "ordinal": ordinal,
        "text": text,
    }
    meta.update({"persona_name": persona} if persona else {"cast_wide": True})
    bound._vectors_client().put_vectors(
        vectorBucketName=os.environ["VECTOR_BUCKET"],
        indexName=bound._vector_index(),
        vectors=[{
            "key": bound._vector_key(doc_id, ordinal),
            "data": {"float32": unit_vector(1.0)},
            "metadata": meta,
        }],
    )


async def _kb_vectors(db, kb_id):
    """What actually landed in a KB's index."""
    import os

    index = vecmod.kb_index_name(kb_id, db.table_prefix)
    page = db._vectors_client().list_vectors(
        vectorBucketName=os.environ["VECTOR_BUCKET"],
        indexName=index,
        returnData=True,
        returnMetadata=True,
    )
    return page.get("vectors", [])


# --------------------------------------------------------------------------- #
# The item that is not a document
# --------------------------------------------------------------------------- #


async def test_the_embedding_marker_does_not_become_a_knowledge_base(db):
    """§4.3. The marker has no run_id and no owner_sub; a scan that treated every item as
    a document would mint a KB for it."""
    doc = await _document(db, "run-a", "policy.md", ["egress inspection rules"])
    await _store_vector(db, "run-a", doc, 0, "egress inspection rules")
    # The marker is written by `store_chunk_vectors`; write it directly so the test does
    # not depend on having embedded anything.
    await db._call(
        db._table("documents").put_item,
        Item={"pk": "EMBEDDING", "sk": "META", "model": "m", "created_at": 1},
    )

    documents, others = await migrate_script.scan_documents(db)

    assert [str(d.get("id")) for d in documents] == [doc]
    assert [(o["pk"], o["sk"]) for o in others] == [("EMBEDDING", "META")]


async def test_an_unrecognised_non_document_stops_the_migration(db):
    """A future non-document item must be a LOUD failure, not a silent skip.

    The alternative is a migration that quietly ignores something nobody has looked at,
    which is how an item ends up mangled on the second run instead of the first.
    """
    await db._call(
        db._table("documents").put_item,
        Item={"pk": "SOMETHING", "sk": "NEW", "whatever": 1},
    )
    with pytest.raises(SystemExit, match="neither a document nor a known marker"):
        await migrate_script.migrate(db, apply=False, bind=False)


# --------------------------------------------------------------------------- #
# The mapping
# --------------------------------------------------------------------------- #


async def test_cast_wide_and_persona_documents_go_to_DIFFERENT_kbs(db):
    """§4.4. Collapsing them would silently make a persona-scoped document cast-wide —
    a disclosure to the rest of the cast, not a tidiness problem."""
    shared = await _document(db, "run-b", "shared.md", ["for everyone"])
    private = await _document(db, "run-b", "ada.md", ["hers alone"], persona="Ada")
    await _store_vector(db, "run-b", shared, 0, "for everyone")
    await _store_vector(db, "run-b", private, 0, "hers alone", persona="Ada")

    await migrate_script.migrate(db, apply=True, bind=False)

    kbs = {kb["name"]: kb for kb in await db.list_knowledge_bases(owner_sub=TEST_OWNER)}
    assert set(kbs) == {"migrated-run-b", "migrated-run-b-ada"}

    cast_wide = await _kb_vectors(db, kbs["migrated-run-b"]["id"])
    persona = await _kb_vectors(db, kbs["migrated-run-b-ada"]["id"])
    assert [v["metadata"]["document_id"] for v in cast_wide] == [shared]
    assert [v["metadata"]["document_id"] for v in persona] == [private]


async def test_two_tenants_documents_never_land_in_one_kb(db):
    """The owner is part of the grouping key, and a KB has exactly one owner.

    **The same run id for both tenants**, which is the case that actually exercises this.
    An earlier version of this test used two different run ids, so dropping `owner` from
    the grouping key left it passing — the mutant survived the very test written for it.
    Two owners genuinely can share a run id: a run is keyed `USER#{sub}` / `RUN#{id}`, so
    the id is only unique within a partition, while the DOCUMENTS table is partitioned by
    `RUN#{run_id}` alone. Same run id therefore means one shared documents partition,
    which is exactly where a careless grouping would merge two tenants' corpora.
    """
    run_id = "run-collision"
    mine = await _document(db, run_id, "mine.md", ["my material"])
    theirs = await _document(db, run_id, "theirs.md", ["their material"], owner=OTHER_USER)
    await _store_vector(db, run_id, mine, 0, "my material")
    await _store_vector(db, run_id, theirs, 0, "their material", owner=OTHER_USER)

    await migrate_script.migrate(db, apply=True, bind=False)

    my_kbs = await db.list_knowledge_bases(owner_sub=TEST_OWNER)
    their_kbs = await db.list_knowledge_bases(owner_sub=OTHER_USER)
    assert {kb["owner_sub"] for kb in my_kbs} == {TEST_OWNER}
    assert {kb["owner_sub"] for kb in their_kbs} == {OTHER_USER}
    # And no KB holds both documents.
    for kb in my_kbs + their_kbs:
        docs = {v["metadata"]["document_id"] for v in await _kb_vectors(db, kb["id"])}
        assert docs in ({mine}, {theirs}), docs


async def test_the_title_is_written_into_the_vector_metadata(db):
    """§8.2. A grantee cannot read the owner's document rows, so the title has to be on
    the vector or a shared passage cites as a hex id."""
    doc = await _document(db, "run-t", "egress-policy.pdf", ["the passage"])
    await _store_vector(db, "run-t", doc, 3, "the passage")

    await migrate_script.migrate(db, apply=True, bind=False)

    kb = (await db.list_knowledge_bases(owner_sub=TEST_OWNER))[0]
    stored = await _kb_vectors(db, kb["id"])
    assert stored[0]["metadata"]["title"] == "egress-policy.pdf"


async def test_documents_with_no_vectors_are_skipped_not_given_an_empty_kb(db):
    """Real case: the corpora were embedded per run and some runs never got that far.
    An empty KB is indistinguishable from a broken one later."""
    await _document(db, "run-empty", "never-embedded.md", ["text with no vector"])

    await migrate_script.migrate(db, apply=True, bind=False)

    assert await db.list_knowledge_bases(owner_sub=TEST_OWNER) == []


# --------------------------------------------------------------------------- #
# A dry run writes nothing
# --------------------------------------------------------------------------- #


async def test_a_dry_run_creates_no_kb_and_copies_no_vector(db):
    doc = await _document(db, "run-dry", "policy.md", ["egress"])
    await _store_vector(db, "run-dry", doc, 0, "egress")

    await migrate_script.migrate(db, apply=False, bind=False)

    assert await db.list_knowledge_bases(owner_sub=TEST_OWNER) == []


# --------------------------------------------------------------------------- #
# Re-runnable
# --------------------------------------------------------------------------- #


async def test_running_twice_reuses_the_kb_rather_than_duplicating_it(db):
    """A migration that half-completes will be re-run. Deterministic names are how this
    is idempotent without keeping a ledger."""
    doc = await _document(db, "run-twice", "policy.md", ["egress"])
    await _store_vector(db, "run-twice", doc, 0, "egress")

    await migrate_script.migrate(db, apply=True, bind=False)
    first = await db.list_knowledge_bases(owner_sub=TEST_OWNER)
    await migrate_script.migrate(db, apply=True, bind=False)
    second = await db.list_knowledge_bases(owner_sub=TEST_OWNER)

    assert len(first) == 1
    assert [kb["id"] for kb in second] == [kb["id"] for kb in first]
    # And the vectors are overwritten by key, not duplicated: the key is
    # `document_id:ordinal`, so a re-run is a PutVectors over the same keys.
    assert len(await _kb_vectors(db, first[0]["id"])) == 1


# --------------------------------------------------------------------------- #
# Bindings, which are opt-in
# --------------------------------------------------------------------------- #


async def test_bindings_are_not_written_unless_asked(db):
    doc = await _document(db, "run-nobind", "policy.md", ["egress"])
    await _store_vector(db, "run-nobind", doc, 0, "egress")

    await migrate_script.migrate(db, apply=True, bind=False)

    run = await db.for_owner(TEST_OWNER).get_run("run-nobind")
    assert json.loads(run.get("config_json") or "{}").get("knowledge_bases") in (None, [])


async def test_bind_writes_a_run_level_binding_for_a_cast_wide_kb(db):
    doc = await _document(db, "run-bind", "policy.md", ["egress"])
    await _store_vector(db, "run-bind", doc, 0, "egress")

    await migrate_script.migrate(db, apply=True, bind=True)

    kb = (await db.list_knowledge_bases(owner_sub=TEST_OWNER))[0]
    run = await db.for_owner(TEST_OWNER).get_run("run-bind")
    assert json.loads(run["config_json"])["knowledge_bases"] == [kb["id"]]


async def test_bind_writes_a_PERSONA_level_binding_for_a_persona_kb(db):
    """The level has to match the scope, or a persona-scoped collection becomes
    cast-wide at the binding even though the mapping got it right."""
    doc = await _document(db, "run-bindp", "ada.md", ["hers"], persona="Ada")
    await _store_vector(db, "run-bindp", doc, 0, "hers", persona="Ada")

    await migrate_script.migrate(db, apply=True, bind=True)

    kb = (await db.list_knowledge_bases(owner_sub=TEST_OWNER))[0]
    run = await db.for_owner(TEST_OWNER).get_run("run-bindp")
    cast = json.loads(run["cast_json"])
    assert cast[0]["name"] == "Ada"
    assert cast[0]["knowledge_bases"] == [kb["id"]]
    # And NOT at run level.
    assert json.loads(run.get("config_json") or "{}").get("knowledge_bases") in (None, [])


async def test_binding_a_persona_that_is_not_in_the_cast_is_refused(db):
    """A document whose `persona_name` does not match any cast member. Binding it
    cast-wide instead would be a silent disclosure, so it stops."""
    bound = db.for_owner(TEST_OWNER)
    await bound.create_run(run_id="run-ghost", topic="t", cast=[{"name": "Ada"}], name="run-ghost")
    doc = await bound.add_document(
        run_id="run-ghost", title="ghost.md", chunks=["x"], text="x",
        persona_name="Someone Who Left",
    )
    await _store_vector(db, "run-ghost", doc, 0, "x", persona="Someone Who Left")

    with pytest.raises(SystemExit, match="no persona named"):
        await migrate_script.migrate(db, apply=True, bind=True)


async def test_runs_sharing_a_name_PREFIX_get_different_kbs(db):
    """Found by the dry run against the real account, not by review.

    The KB name is the idempotence key, and the first version truncated the run id to
    eight characters. The measurement corpora are named `eval-178…`, so four separate runs
    printed as `migrated-eval-178` — the second would have "reused" the first's KB and
    four distinct corpora would have been merged into one. With `--bind` it would have
    bound material into conversations it does not belong to.
    """
    first = await _document(db, "eval-1789012-alpha", "a.md", ["alpha material"])
    second = await _document(db, "eval-1789012-beta", "b.md", ["beta material"])
    await _store_vector(db, "eval-1789012-alpha", first, 0, "alpha material")
    await _store_vector(db, "eval-1789012-beta", second, 0, "beta material")

    await migrate_script.migrate(db, apply=True, bind=False)

    kbs = await db.list_knowledge_bases(owner_sub=TEST_OWNER)
    assert len(kbs) == 2, [kb["name"] for kb in kbs]
    corpora = []
    for kb in kbs:
        stored = await _kb_vectors(db, kb["id"])
        corpora.append(tuple(sorted(v["metadata"]["document_id"] for v in stored)))
    assert sorted(corpora) == sorted([(first,), (second,)])


async def test_persona_kbs_on_prefix_sharing_runs_also_differ(db):
    """The same collision at persona level, which the same truncation caused."""
    a = await _document(db, "eval-1789012-alpha", "a.md", ["alpha"], persona="Ada")
    b = await _document(db, "eval-1789012-beta", "b.md", ["beta"], persona="Ada")
    await _store_vector(db, "eval-1789012-alpha", a, 0, "alpha", persona="Ada")
    await _store_vector(db, "eval-1789012-beta", b, 0, "beta", persona="Ada")

    await migrate_script.migrate(db, apply=True, bind=False)

    kbs = await db.list_knowledge_bases(owner_sub=TEST_OWNER)
    assert len({kb["name"] for kb in kbs}) == 2, [kb["name"] for kb in kbs]


async def test_documents_already_in_a_kb_are_skipped_not_refused(db):
    """A `KB#` document appeared when knowledge bases gained their own documents.

    Worth recording how it surfaced: the unrecognised-item check is a `SystemExit`, so
    such an item ABORTED the whole migration rather than being quietly mishandled. That
    is the check doing its job — refusing to guess about an item shape nobody had told
    it about — and the reason it is loud rather than a log line.
    """
    bound = db.for_owner(TEST_OWNER)
    kb = await bound.create_knowledge_base("hand-made", owner_sub=TEST_OWNER)
    await bound.add_kb_document(kb["id"], title="already-here.md", text="kb material")

    run_doc = await _document(db, "run-mixed", "run.md", ["run material"])
    await _store_vector(db, "run-mixed", run_doc, 0, "run material")

    documents, others = await migrate_script.scan_documents(db)
    assert [str(d.get("id")) for d in documents] == [run_doc], "a KB document was migrated"
    assert all(migrate_script.is_known_non_document(o) for o in others)

    # And the migration completes rather than exiting.
    await migrate_script.migrate(db, apply=True, bind=False)
    names = {k["name"] for k in await db.list_knowledge_bases(owner_sub=TEST_OWNER)}
    assert names == {"hand-made", "migrated-run-mixed"}
