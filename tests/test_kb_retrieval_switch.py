# SPDX-License-Identifier: Apache-2.0
"""
Phase 6 step 5 — the switch: `retrieve_for_turn` searches bound knowledge bases
alongside the run's own documents.

`docs/PHASE6-KB-DESIGN.md` §8.1 records why the run slice is NOT replaced, which is a
correction to §6's build order: one vector index per *run* would cap the install at
10,000 conversations, which §8b rejects outright. So retrieval reads two sources and
merges them, and the ceiling moves onto knowledge bases where it is unreachable.

**The negative cases come first, and that ordering is the point.** This step moves a
tenancy boundary: what a turn may read stops being a metadata filter (`_slice_filter`,
which pins `owner_sub`) and starts also being a *set of indexes chosen by an
authorisation decision*. A suite that only proves the fan-out returns passages is the
shape of a suite that would pass with the grant check removed entirely.

The `FakeVectors` harness is reused from `test_kb_retrieval.py` deliberately: it ranks by
real cosine distance and it RECORDS EVERY FILTER, because the first version of it ignored
`filter` and let "a grantee can read a shared KB" pass even with an `owner_sub` filter
added back. A fake that shares a wrong premise with the code is worse than no fake.
"""

import json
from unittest.mock import patch

import pytest

from matrix_studio.retrieval import retrieve_for_turn
from matrix_studio.storage import vectors as vecmod
from tests.support import TEST_OWNER, unit_vector
from tests.test_kb_retrieval import FakeVectors, _cosine_distance  # noqa: F401

pytestmark = pytest.mark.asyncio

OTHER_USER = "sub-someone-else-9999"
MODEL = "test-model"


@pytest.fixture
def fake(db, monkeypatch):
    """Route `query_vectors` to the fake, leaving every other call on the real client."""
    f = FakeVectors()
    real = db._vectors_client()

    class Routed:
        def __getattr__(self, name):
            if name == "query_vectors":
                return f.query_vectors
            return getattr(real, name)

    monkeypatch.setattr(db, "_vectors_client", lambda: Routed())
    return f


def _embedding(*axis):
    """A stand-in embedder returning one fixed unit vector."""
    vector = unit_vector(*axis)

    async def fake_embed(model=None, input=None, **kwargs):
        return type(
            "R",
            (),
            {"data": [{"embedding": list(vector)}], "usage": type("U", (), {"prompt_tokens": 1})()},
        )()

    return fake_embed


async def _run(db, run_id="run-1", *, config=None, cast=None, groups=None):
    await db.create_run(
        run_id=run_id,
        topic="egress inspection at the border",
        cast=cast or [{"name": "Ada"}, {"name": "Dan"}],
        name=run_id,
        config=config or {},
        groups=groups,
    )
    return await db.get_run(run_id)


async def _kb(db, fake, name, chunks, *, owner=TEST_OWNER):
    """A KB whose faked index holds one vector per chunk, each on its own axis."""
    kb = await db.create_knowledge_base(name, owner_sub=owner)
    index = vecmod.kb_index_name(kb["id"], db.table_prefix)
    for doc_id, ordinal, text, axis, title in chunks:
        fake.add(
            index, doc_id, ordinal, text, unit_vector(*axis),
            kb_id=kb["id"], owner_sub=owner, title=title,
        )
    return kb


async def _run_slice_chunk(db, fake, run_id, doc_id, ordinal, text, axis, *, persona=None):
    """One vector in the SHARED index, scoped exactly as `store_chunk_vectors` writes it.

    A cast-wide chunk carries `cast_wide: True` and **no** `persona_name` — absent
    metadata cannot be matched by an S3 Vectors filter, which is why the flag exists.
    Writing `persona_name: None` here instead made three tests fail, correctly: the code
    was right and the fixture was lying about the shape of a real vector.
    """
    scope = {"persona_name": persona} if persona else {"cast_wide": True}
    fake.add(
        db._vector_index(), doc_id, ordinal, text, unit_vector(*axis),
        owner_sub=TEST_OWNER, run_id=run_id, **scope,
    )


async def _retrieve(db, run_id="run-1", persona="Ada", *, axis=(1.0,), k=5):
    with patch("litellm.aembedding", side_effect=_embedding(*axis)), \
         patch("litellm.completion_cost", return_value=0.0):
        return await retrieve_for_turn(
            db, run_id, persona,
            topic="egress inspection at the border",
            conversation=[{"content": "what does the policy say about egress inspection"}],
            k=k, max_chars=4000, mode="vector", embedding_model=MODEL,
        )


# --------------------------------------------------------------------------- #
# The boundary: a binding is not permission
# --------------------------------------------------------------------------- #


async def test_a_bound_kb_the_caller_cannot_read_contributes_nothing(db, fake):
    """The central negative. A binding says "search this"; a grant says "you may"."""
    kb = await _kb(
        db, fake, "someone-elses-corpus",
        [("d-secret", 0, "the secret egress policy", (1.0,), "secret.md")],
        owner=OTHER_USER,
    )
    await _run(db, config={"knowledge_bases": [kb["id"]]})

    passages, _query, _floor, failures = await _retrieve(db)

    assert passages == [], "a KB with no grant was searched"
    # And not reported as a FAILURE either: it was refused, not broken. Conflating the
    # two would send an operator looking for an outage.
    assert failures == []
    index = vecmod.kb_index_name(kb["id"], db.table_prefix)
    assert index not in fake.calls, "the unauthorised index was queried at all"


async def test_a_grant_makes_the_same_binding_work(db, fake):
    """The positive half of the pair, so the test above is not passing vacuously."""
    kb = await _kb(
        db, fake, "shared-corpus",
        [("d-shared", 0, "the shared egress policy", (1.0,), "shared.md")],
        owner=OTHER_USER,
    )
    await db.grant_kb(kb["id"], user=TEST_OWNER, granted_by=OTHER_USER)
    await _run(db, config={"knowledge_bases": [kb["id"]]})

    passages, _query, _floor, failures = await _retrieve(db)

    assert [p.document_id for p in passages] == ["d-shared"]
    assert failures == []


async def test_a_revoked_grant_stops_working_at_QUERY_time(db, fake):
    """§8b's requirement, and half of the phase's "done when".

    Not "at binding time": the run already exists with the binding in its config, and the
    revocation must take effect on the very next turn without touching the run.
    """
    kb = await _kb(
        db, fake, "revocable",
        [("d-rev", 0, "material about egress inspection", (1.0,), "rev.md")],
        owner=OTHER_USER,
    )
    await db.grant_kb(kb["id"], user=TEST_OWNER, granted_by=OTHER_USER)
    await _run(db, config={"knowledge_bases": [kb["id"]]})

    before, _q, _f, _fail = await _retrieve(db)
    assert [p.document_id for p in before] == ["d-rev"], "setup failed"

    await db.revoke_kb(kb["id"], user=TEST_OWNER)

    after, _q, _f, _fail = await _retrieve(db)
    assert after == [], "a revoked grant still returned passages"


async def test_an_authorised_but_UNBOUND_kb_is_not_searched(db, fake):
    """Permission is not participation. A KB the user owns but did not bind to this
    conversation must not leak into it — that is what stops a shared corpus appearing
    in conversations nobody intended."""
    await _kb(
        db, fake, "my-other-corpus",
        [("d-other", 0, "egress inspection notes", (1.0,), "other.md")],
    )
    await _run(db, config={})  # no bindings at all

    passages, _q, _f, failures = await _retrieve(db)
    assert passages == []
    assert failures == []
    assert fake.calls == [] or all("kb-" not in c for c in fake.calls)


async def test_resolution_failure_searches_nothing_rather_than_everything(db, fake, monkeypatch):
    """Fails CLOSED. An error deciding permission must not become "search all of them",
    and must not end the turn either — the run slice still answers."""
    kb = await _kb(
        db, fake, "fine-corpus",
        [("d-kb", 0, "kb material on egress", (1.0,), "kb.md")],
    )
    await _run(db, config={"knowledge_bases": [kb["id"]]})
    await _run_slice_chunk(db, fake, "run-1", "d-own", 0, "the run's own egress notes", (1.0,))

    async def boom(*a, **k):
        raise RuntimeError("DynamoDB is having a day")

    monkeypatch.setattr(db, "searchable_kbs", boom)

    passages, _q, _f, failures = await _retrieve(db)

    assert [p.document_id for p in passages] == ["d-own"], "the run slice was lost too"
    assert failures == []
    index = vecmod.kb_index_name(kb["id"], db.table_prefix)
    assert index not in fake.calls


# --------------------------------------------------------------------------- #
# The run slice is untouched — this phase is additive
# --------------------------------------------------------------------------- #


async def test_a_run_with_no_bindings_behaves_exactly_as_before(db, fake):
    """§8.1's whole argument for keeping the run slice: nothing existing changes."""
    await _run(db, config={})
    await _run_slice_chunk(db, fake, "run-1", "d-own", 0, "egress inspection notes", (1.0,))

    passages, _q, _f, failures = await _retrieve(db)

    assert [p.document_id for p in passages] == ["d-own"]
    assert failures == []
    # And the slice filter is still applied, with the owner in it.
    slice_filters = [f for f in fake.filters if f and "run_id" in json.dumps(f)]
    assert slice_filters, "the run slice was queried without its filter"
    assert "owner_sub" in json.dumps(slice_filters[0])


async def test_another_runs_documents_are_still_invisible(db, fake):
    """The pre-existing boundary, re-asserted at the new call site. `_slice_filter`
    keeps `run_id`, so the fan-out must not have become a way around it."""
    await _run(db, run_id="run-1", config={})
    await _run(db, run_id="run-2", config={})
    await _run_slice_chunk(db, fake, "run-2", "d-elsewhere", 0, "egress notes", (1.0,))

    passages, _q, _f, _fail = await _retrieve(db, run_id="run-1")
    assert passages == []


# --------------------------------------------------------------------------- #
# Merging the two sources
# --------------------------------------------------------------------------- #


async def test_a_kb_passage_can_outrank_the_runs_own(db, fake):
    """The merge is a sort over both sources, not "run first, then KBs".

    Sound only because the metric is cosine: a distance is between the query and one
    vector, so a number from a KB index is comparable to one from the shared index.
    """
    kb = await _kb(
        db, fake, "sharp",
        [("d-kb", 0, "exactly on point", (1.0, 0.0), "kb.md")],
    )
    await _run(db, config={"knowledge_bases": [kb["id"]]})
    # Off-axis, so it is genuinely the worse match rather than asserted to be.
    await _run_slice_chunk(db, fake, "run-1", "d-own", 0, "loosely related", (0.3, 0.95))

    passages, _q, _f, _fail = await _retrieve(db, axis=(1.0, 0.0))

    assert [p.document_id for p in passages] == ["d-kb", "d-own"]
    assert passages[0].score < passages[1].score


async def test_the_trim_happens_after_the_merge_not_per_source(db, fake):
    """Taking the best k of each source first would drop a KB passage that outranks a
    run passage — and the symptom would be a plausible-looking result set."""
    kb = await _kb(
        db, fake, "three",
        [
            ("d-kb", 0, "best", (1.0, 0.0, 0.0), "kb.md"),
            ("d-kb", 1, "second", (0.95, 0.31, 0.0), "kb.md"),
        ],
    )
    await _run(db, config={"knowledge_bases": [kb["id"]]})
    await _run_slice_chunk(db, fake, "run-1", "d-own", 0, "third", (0.6, 0.8, 0.0))

    passages, _q, _f, _fail = await _retrieve(db, axis=(1.0, 0.0, 0.0), k=2)

    assert len(passages) == 2
    assert [(p.document_id, p.ordinal) for p in passages] == [("d-kb", 0), ("d-kb", 1)]


async def test_a_failed_kb_index_is_reported_and_the_turn_still_answers(db, fake):
    """Partial results are not free: the merged top-k comes from a smaller pool, so a
    passage that would have ranked first is absent. Hence RETURNED, not just logged."""
    kb_ok = await _kb(db, fake, "ok", [("d-ok", 0, "fine", (1.0,), "ok.md")])
    kb_bad = await _kb(db, fake, "bad", [("d-bad", 0, "unreachable", (1.0,), "bad.md")])
    fake.broken.add(vecmod.kb_index_name(kb_bad["id"], db.table_prefix))
    await _run(db, config={"knowledge_bases": [kb_ok["id"], kb_bad["id"]]})

    passages, _q, _f, failures = await _retrieve(db)

    assert [p.document_id for p in passages] == ["d-ok"]
    assert failures == [kb_bad["id"]]


# --------------------------------------------------------------------------- #
# Which KBs a given speaker sees
# --------------------------------------------------------------------------- #


async def test_a_run_level_binding_is_visible_to_every_persona(db, fake):
    kb = await _kb(db, fake, "cast-wide", [("d-all", 0, "for everyone", (1.0,), "all.md")])
    await _run(db, config={"knowledge_bases": [kb["id"]]})

    for persona in ("Ada", "Dan"):
        passages, _q, _f, _fail = await _retrieve(db, persona=persona)
        assert [p.document_id for p in passages] == ["d-all"], persona


async def test_a_persona_binding_is_visible_to_that_persona_ALONE(db, fake):
    """Generalises Phase 5's `persona_name = ? OR persona_name IS NULL` exactly, and
    the negative half is the one that matters: Dan must not see Ada's collection."""
    kb = await _kb(db, fake, "adas-own", [("d-ada", 0, "hers alone", (1.0,), "ada.md")])
    await _run(
        db,
        config={},
        cast=[{"name": "Ada", "knowledge_bases": [kb["id"]]}, {"name": "Dan"}],
    )

    hers, _q, _f, _fail = await _retrieve(db, persona="Ada")
    assert [p.document_id for p in hers] == ["d-ada"]

    his, _q, _f, _fail = await _retrieve(db, persona="Dan")
    assert his == [], "a persona-scoped KB leaked to the rest of the cast"


# --------------------------------------------------------------------------- #
# Titles, which a grantee cannot look up
# --------------------------------------------------------------------------- #


async def test_a_shared_passage_carries_its_own_title(db, fake):
    """§8.2. A KB's document rows live in the OWNER's partition, and a grantee's
    credentials are pinned to their own with `dynamodb:LeadingKeys` — so the title is
    unreachable by design and must travel in the vector's metadata.

    Without it a citation renders as `4f2a91c07b3e #0`, which is unreadable in a
    transcript and makes the second-hand citation ledger match on ids no human can check.
    """
    kb = await _kb(
        db, fake, "shared-titles",
        [("d-shared", 3, "the passage text", (1.0,), "egress-policy.pdf")],
        owner=OTHER_USER,
    )
    await db.grant_kb(kb["id"], user=TEST_OWNER, granted_by=OTHER_USER)
    await _run(db, config={"knowledge_bases": [kb["id"]]})

    passages, _q, _f, _fail = await _retrieve(db)

    assert passages[0].title == "egress-policy.pdf"
    assert passages[0].citation == "egress-policy.pdf #3"


# --------------------------------------------------------------------------- #
# Groups, which the turn loop has no token for
# --------------------------------------------------------------------------- #


async def test_a_group_granted_kb_resolves_from_the_groups_on_the_run(db, fake):
    """§8.3. A turn runs in a Step Functions state with no JWT, so the creator's verified
    groups are captured on the run row at creation. Without this a group-granted KB
    would list in the API and retrieve nothing during the run."""
    kb = await _kb(
        db, fake, "team-corpus",
        [("d-team", 0, "team material", (1.0,), "team.md")],
        owner=OTHER_USER,
    )
    await db.grant_kb(kb["id"], group="platform", granted_by=OTHER_USER)
    await _run(db, config={"knowledge_bases": [kb["id"]]}, groups=["platform"])

    passages, _q, _f, _fail = await _retrieve(db)
    assert [p.document_id for p in passages] == ["d-team"]


async def test_without_that_group_the_same_binding_yields_nothing(db, fake):
    kb = await _kb(
        db, fake, "team-corpus-2",
        [("d-team", 0, "team material", (1.0,), "team.md")],
        owner=OTHER_USER,
    )
    await db.grant_kb(kb["id"], group="platform", granted_by=OTHER_USER)
    await _run(db, config={"knowledge_bases": [kb["id"]]}, groups=["some-other-team"])

    passages, _q, _f, _fail = await _retrieve(db)
    assert passages == []


async def test_malformed_recorded_groups_read_as_no_groups(db, fake, monkeypatch):
    """Fail closed. A corrupt `groups_json` must not raise (ending the turn) and must
    not be read optimistically."""
    from matrix_studio.retrieval import _recorded_groups

    assert _recorded_groups({"groups_json": "not json"}) == []
    assert _recorded_groups({"groups_json": json.dumps({"not": "a list"})}) == []
    assert _recorded_groups({"groups_json": json.dumps([None, "", "  ", "real"])}) == ["real"]
    assert _recorded_groups({}) == []


async def test_the_creators_groups_are_recorded_on_the_run(db):
    """The capture itself, since everything above depends on it."""
    run = await _run(db, groups=["platform", "sre"])
    assert json.loads(run["groups_json"]) == ["platform", "sre"]

    none = await _run(db, run_id="run-nogroups", groups=None)
    assert none["groups_json"] is None


async def test_a_passage_in_both_sources_is_returned_ONCE(db, fake):
    """De-duplication across the two sources, by chunk id.

    A document attached to the run AND held in a bound KB is reachable twice — which is
    exactly what the Phase 6 migration produces, since it copies a run's vectors into a
    KB and leaves the shared index in place. Without de-duplication the same text is
    spent twice against `max_chars` and cited twice in one turn as though it were two
    independent pieces of evidence.
    """
    kb = await _kb(db, fake, "copy-of-the-run", [("d-same", 0, "the same passage", (1.0,), "same.md")])
    await _run(db, config={"knowledge_bases": [kb["id"]]})
    await _run_slice_chunk(db, fake, "run-1", "d-same", 0, "the same passage", (1.0,))

    passages, _q, _f, _fail = await _retrieve(db)

    assert len(passages) == 1, f"the same passage came back {len(passages)} times"
    assert passages[0].document_id == "d-same"


async def test_de_duplication_keeps_the_better_ranked_copy(db, fake):
    """The lower cosine distance wins, so a duplicate can never cost ranking."""
    kb = await _kb(db, fake, "sharper-copy", [("d-same", 0, "exact", (1.0, 0.0), "same.md")])
    await _run(db, config={"knowledge_bases": [kb["id"]]})
    # Same chunk identity, worse vector — a stale copy in the shared index.
    await _run_slice_chunk(db, fake, "run-1", "d-same", 0, "exact", (0.6, 0.8))

    passages, _q, _f, _fail = await _retrieve(db, axis=(1.0, 0.0))

    assert len(passages) == 1
    assert passages[0].score == pytest.approx(0.0, abs=1e-6)
