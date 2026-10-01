# SPDX-License-Identifier: Apache-2.0
"""The Research state: where a corpus is allowed to go, and what happens when it cannot.

`docs/PERSONA-RESEARCH.md` §11 step 4. `test_research.py` covers the searcher; this covers the
three things the searcher must not decide for itself.

**Ownership is checked twice, and the second time is the one that matters.** `allocate_targets`
resolves targets at creation, but they are stored in `config_json` — which is built from a request
body — so between allocation and use there is a JSON blob the caller controls. Phase 6 is explicit
that write permission is ownership ALONE ("a grant says *may read*"), so `test_a_target_the_caller_
does_not_own_is_refused` is the test that keeps research from writing into somebody else's
collection by way of a crafted config.

**Research must never fail a run** (§5.2). Every failure here is asserted to produce a RECORD and
not an exception: no provider, a provider that raises, a KB that vanished, a store that will not
take the record. A conversation the operator asked for is not lost to a rate limit.

**The corpus has to be EMBEDDED.** A chunk with no vector is invisible to a k-NN query, and the only
other caller of `embed_pending_kb_chunks` is the upload route — so a document that did not arrive
through it would never be embedded by anything. That failure would read as "research found nothing
useful", a judgement about quality, while the corpus sat in the collection unvectorised. It is the
exact shape of the three features that shipped inert in this project, so it gets a test of its own.
"""

import json

import pytest

from matrix_studio import research as rs
from matrix_studio import research_state as st
from tests.support import TEST_OWNER

#: The owner the `db` fixture is BOUND to. Using anything else here would write the run row
#: under one partition and read it back from another, and every test would report "run does not
#: exist" — a failure that looks like the code and is the fixture.
OWNER = TEST_OWNER

#: A different user, for the ownership refusals.
STRANGER = "sub-stranger-9999"


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def test_absent_research_config_is_off():
    """Every run created before this feature existed, and every run that does not ask."""
    assert st.settings_from(None).enabled is False
    assert st.settings_from({}).enabled is False
    assert st.settings_from({"research": {}}).enabled is False


def test_research_true_is_accepted_as_enabled():
    """A hand-written definition saying `research: true` means it. Refusing that would be
    pedantry about JSON rather than about research."""
    assert st.settings_from({"research": True}).enabled is True


def test_a_malformed_research_config_reads_as_off_rather_than_raising():
    """This is parsed on the turn-loop side, where the config was already accepted.

    Raising would fail a run over a field that only ever ADDS to it.
    """
    assert st.settings_from({"research": "yes please"}).enabled is False
    assert st.settings_from({"research": ["a", "b"]}).enabled is False


def test_both_tiers_default_on_and_can_be_switched_off_independently():
    s = st.settings_from({"research": {"enabled": True, "personas": False}})
    assert s.shared is True and s.personas is False


def test_a_negative_or_unparseable_count_falls_back_to_the_measured_default():
    """`None` means "use the value in research.py, next to the measurement that chose it"."""
    s = st.settings_from({"research": {"enabled": True, "results_per_query": -3,
                                       "fetch_per_query": "lots"}})
    assert s.results_per_query is None and s.fetch_per_query is None


def test_targets_are_read_but_never_trusted():
    """They are parsed, because the state needs them. `_verified_target` is what checks them."""
    s = st.settings_from({"research": {"enabled": True,
                                       "targets": {"shared": "kb-1",
                                                   "personas": {"Casey": "kb-2"}}}})
    assert s.target_for(None) == "kb-1"
    assert s.target_for("Casey") == "kb-2"
    assert s.target_for("Jordan") is None


# --------------------------------------------------------------------------- #
# Allocation: always a NEW collection per scope, bound alongside what was there
# --------------------------------------------------------------------------- #


def _request(**config):
    return {
        "topic": "Whether a specialty plan may be renewed without a new exam",
        "cast": [
            {"name": "Casey", "structured": {"viewpoints": ["The statute governs"]}},
            {"name": "Jordan", "structured": {"viewpoints": ["Continuity of care matters"]}},
        ],
        "config": {"research": {"enabled": True}, **config},
    }


#: The pass most allocation tests allocate for. A run id, as `create_run` passes.
FOR = st.for_run("r1")


async def _allocate(db, request, *, research_for=FOR, **kw):
    return await st.allocate_targets(
        db, request, owner_sub=OWNER, label=kw.pop("label", "run"), research_for=research_for, **kw,
    )


async def test_allocation_is_a_no_op_when_research_is_off(db):
    """A run without research must be byte-identical to one created before the feature."""
    request = {"topic": "t", "cast": [{"name": "Casey"}], "config": {"max_messages": 4}}
    out = await _allocate(db, dict(request))
    assert out == request


async def test_a_bound_kb_the_caller_owns_is_NOT_the_target_a_new_one_is_bound_beside_it(db):
    """The 2026-09-30 incident, at the allocation step.

    This used to reuse an owned bound collection as the target. A run created from another run's
    setup inherits that run's bindings, so reuse pointed both runs' research at the same curated
    collection — and the second pass replaced the first's batch inside it. Now the curated one
    stays bound, exactly where the operator put it, and research gets a collection of its own.
    """
    existing = await db.create_knowledge_base("Casey's statutes", owner_sub=OWNER)
    out = await _allocate(db, _request(knowledge_bases=[existing["id"]]))

    shared = out["config"]["research"]["targets"]["shared"]
    assert shared != existing["id"]
    # Kept, and kept FIRST: the binding the operator made is untouched, research is appended.
    assert out["config"]["knowledge_bases"] == [existing["id"], shared]


async def test_a_persona_s_curated_binding_is_kept_and_their_research_goes_beside_it(db):
    """The same at a persona's scope, which is where six of the seven incident collections were."""
    hers = await db.create_knowledge_base("Casey's own", owner_sub=OWNER)
    request = _request()
    request["cast"][0]["knowledge_bases"] = [hers["id"]]

    out = await _allocate(db, request)

    target = out["config"]["research"]["targets"]["personas"]["Casey"]
    assert target != hers["id"]
    casey = next(m for m in out["cast"] if m["name"] == "Casey")
    assert casey["knowledge_bases"] == [hers["id"], target]


async def test_a_new_kb_is_created_MARKED_and_BOUND(db):
    """The id is pre-allocated into the bindings before anything is searched, and the row says
    which pass it belongs to — the one fact the Research state checks before writing.

    A run's bindings live in `config_json`, written once, so "research then associate the KB"
    would be a read-modify-write of a JSON blob from a state the machine can retry.
    """
    out = await _allocate(db, _request(), label="renewal")

    shared = out["config"]["research"]["targets"]["shared"]
    assert out["config"]["knowledge_bases"] == [shared]
    row = await db.get_knowledge_base(shared)
    assert row["owner_sub"] == OWNER
    assert row["research_for"] == FOR
    # Named for the run AND the scope, so a list of seven of them says which is whose.
    assert "renewal" in row["name"] and "shared" in row["name"]
    casey = out["config"]["research"]["targets"]["personas"]["Casey"]
    assert "Casey" in (await db.get_knowledge_base(casey))["name"]


async def test_allocation_refuses_to_run_without_knowing_its_pass(db):
    """An unmarked collection is one the Research state will refuse to write into, so allocating
    one would make a run that searches, pays, and stores nothing."""
    with pytest.raises(ValueError):
        await st.allocate_targets(db, _request(), owner_sub=OWNER, label="run", research_for="")


async def test_each_persona_gets_their_OWN_target_not_the_cast_wide_one(db):
    """§2.2: a private corpus holds the persona's stance AND the opposition's case.

    Bound at the persona's own scope only. `bindings.bound_kbs` returns the union, which is right
    for a TURN — a persona may search the cast-wide collection — and wrong here: it would put one
    persona's research, including the case against them, into the collection every other persona
    reads.
    """
    out = await _allocate(db, _request())

    targets = out["config"]["research"]["targets"]
    casey, jordan, shared = (
        targets["personas"]["Casey"], targets["personas"]["Jordan"], targets["shared"],
    )
    assert len({casey, jordan, shared}) == 3, "the three scopes must not share a collection"
    by_name = {m["name"]: m for m in out["cast"]}
    assert by_name["Casey"]["knowledge_bases"] == [casey]
    assert by_name["Jordan"]["knowledge_bases"] == [jordan]


async def test_a_persona_with_no_viewpoint_gets_no_collection(db):
    """They have no stance to research and no opposition to find. Creating one would leave an
    empty collection bound to them for ever."""
    request = _request()
    request["cast"].append({"name": "Silent"})
    out = await _allocate(db, request)

    assert "Silent" not in out["config"]["research"]["targets"]["personas"]
    assert not [m for m in out["cast"] if m["name"] == "Silent"][0].get("knowledge_bases")


async def test_a_kb_the_caller_does_not_own_is_left_read_only_and_a_new_one_is_added(db):
    """A cast-wide binding may be somebody else's collection, shared read-only.

    Writing into it would be a side effect on data another user curated, arriving from a run
    they cannot see. So research creates its own and binds that IN ADDITION.
    """
    theirs = await db.create_knowledge_base("Somebody else's", owner_sub=STRANGER)
    out = await _allocate(db, _request(knowledge_bases=[theirs["id"]]))

    shared = out["config"]["research"]["targets"]["shared"]
    assert shared != theirs["id"]
    # Still bound — the run may READ it. It is simply not where research writes.
    assert out["config"]["knowledge_bases"] == [theirs["id"], shared]


async def test_a_caller_supplied_targets_block_is_overwritten_not_merged(db):
    """`targets` names collections to WRITE into, and it arrives from a request body.

    The API model does not declare the field, so it is normally dropped before reaching here —
    this asserts the server-side half, which is what holds if that model ever changes.
    """
    out = await _allocate(
        db, _request(research={"enabled": True, "targets": {"shared": "kb-somebody-elses"}}),
    )
    assert out["config"]["research"]["targets"]["shared"] != "kb-somebody-elses"


async def test_the_vector_index_is_ensured_with_PRIVILEGED_credentials(db, monkeypatch):
    """The tenant role holds `GetIndex` and deliberately not `CreateIndex`.

    So the API must create the index at allocation time; the worker running the Research state
    physically cannot. Getting this wrong is not a traceback — it is a collection that exists,
    accepts documents, and can never hold a vector for any of them.
    """
    seen = []

    async def ensure(store, kb_id):
        seen.append((store, kb_id))
        return True

    monkeypatch.setattr("matrix_studio.storage.vectors.ensure_index_for_kb", ensure)
    sentinel = object()
    curated = await db.create_knowledge_base("curated", owner_sub=OWNER)
    out = await _allocate(db, _request(knowledge_bases=[curated["id"]]), privileged=sentinel)

    targets = out["config"]["research"]["targets"]
    every = {targets["shared"], *targets["personas"].values()}
    # Exactly the new ones. A bound collection is read, never written, so its index is the
    # upload route's business and not this one's.
    assert {kb for _s, kb in seen} == every
    assert curated["id"] not in {kb for _s, kb in seen}
    assert all(store is sentinel for store, _kb in seen), (
        "the index must be created with the unscoped store, not the tenant-bound one"
    )


# --------------------------------------------------------------------------- #
# The state: skipping, and why each skip exists
# --------------------------------------------------------------------------- #


async def _run_row(db, run_id="r1", config=None, **kwargs):
    await db.create_run(
        run_id=run_id, topic="a topic",
        cast=[{"name": "Casey", "structured": {"viewpoints": ["v"]}}],
        name=run_id, config=config or {}, owner_sub=OWNER, **kwargs,
    )
    return run_id


async def test_a_run_without_research_is_skipped_without_touching_anything(db):
    run_id = await _run_row(db, config={"max_messages": 4})
    record = await st.run_research(db, run_id, owner_sub=OWNER)

    assert record["status"] == st.SKIPPED
    row = await db.get_run(run_id)
    # Nothing recorded: a run that did not ask has nothing to account for, and writing a row
    # for it would put a research field on every conversation in the system.
    assert not row.get("research_json")


async def test_a_missing_run_is_reported_rather_than_raising(db):
    record = await st.run_research(db, "no-such-run", owner_sub=OWNER)
    assert record["status"] == st.SKIPPED
    assert "does not exist" in record["error"]


@pytest.mark.parametrize("mode", ["branch", "resume"])
async def test_a_branch_or_resume_inherits_research_rather_than_repeating_it(db, mode):
    """Researching again would ingest a second batch into collections the parent filled.

    A branch exists to vary ONE thing from a run that happened; re-searching would vary the
    evidence too, and the comparison would stop being about the branch point.
    """
    run_id = await _run_row(db, config={"research": {"enabled": True}})
    record = await st.run_research(db, run_id, owner_sub=OWNER, mode=mode)

    assert record["status"] == st.SKIPPED
    assert mode in record["error"]
    # Recorded this time: the run DID ask, so an operator looking for its corpus deserves the
    # reason there is none.
    assert json.loads((await db.get_run(run_id))["research_json"])["status"] == st.SKIPPED


async def test_an_ensemble_member_never_researches_for_itself(db):
    """§6: an ensemble researches ONCE, before its members exist.

    Live search per member would give replicates different inputs, and
    ENSEMBLE-CONVERSATIONS.md §2 rests on the opposite — same config, same brief, so divergence
    is evidence about the BRIEF. Independent searches would make it unattributable.
    """
    run_id = await _run_row(
        db, config={"research": {"enabled": True}}, ensemble_id="e1", ensemble_cell="base",
    )
    record = await st.run_research(db, run_id, owner_sub=OWNER)

    assert record["status"] == st.SKIPPED
    assert "replicates stay replicates" in record["error"]


async def test_no_search_provider_is_UNAVAILABLE_not_FAILED(db, monkeypatch):
    """Nothing went wrong; the deployment has no key. Worth distinguishing because the fix is
    a configuration change rather than a retry."""
    from matrix_studio import websearch

    def refuse(preferred=None, **kw):
        raise websearch.SearchUnavailable("no key")

    monkeypatch.setattr(websearch, "select", refuse)
    run_id = await _run_row(db, config={"research": {"enabled": True}})

    record = await st.run_research(db, run_id, owner_sub=OWNER)
    assert record["status"] == st.UNAVAILABLE
    assert json.loads((await db.get_run(run_id))["research_json"])["status"] == st.UNAVAILABLE


# --------------------------------------------------------------------------- #
# The state: doing the work
# --------------------------------------------------------------------------- #


class _Provider:
    name = "fake"
    supplies_text = True

    def __init__(self, hits=None, raises=None):
        self._hits, self._raises = hits or [], raises

    async def search(self, query, count=5):
        if self._raises:
            raise self._raises
        return self._hits


class _Hit:
    def __init__(self, url, title="t", text="x" * 400):
        self.url, self.title, self.text = url, title, text


def _wire(monkeypatch, provider, *, documents=1):
    """Replace the searcher's three seams: the provider, the model, and the tierer."""
    from matrix_studio import websearch

    monkeypatch.setattr(websearch, "select", lambda preferred=None, **kw: provider)

    async def acompletion(messages, model=None, temperature=0.0, max_tokens=None):
        # Two queries, which both prompts accept; the tiering prompt gets a tier per document.
        if "Classify each source" in messages[0]["content"]:
            body = json.dumps([
                {"index": i, "authority": "controlling", "reason": "a statute"}
                for i in range(documents)
            ])
        else:
            body = json.dumps({"queries": [{"query": "state practice act", "intent": "background"}]})
        return {"content": body, "cost_usd": 0.002, "tokens_in": 5, "tokens_out": 5,
                "finish_reason": "stop"}

    monkeypatch.setattr("matrix_studio.analysis._acompletion", acompletion)


async def test_a_successful_pass_ingests_embeds_and_records(db, monkeypatch):
    kb = await db.create_knowledge_base("target", owner_sub=OWNER, research_for=FOR)
    config = {"research": {"enabled": True, "personas": False,
                           "targets": {"shared": kb["id"], "personas": {}}}}
    run_id = await _run_row(db, config=config)
    _wire(monkeypatch, _Provider([_Hit("https://example.gov/statute")]))

    embedded = []

    async def embed(store, kb_id, **kw):
        embedded.append(kb_id)
        return {"embedded": 3, "cost_usd": 0.0001, "model": "titan"}

    monkeypatch.setattr("matrix_studio.retrieval.embed_pending_kb_chunks", embed)

    record = await st.run_research(db, run_id, owner_sub=OWNER)

    assert record["status"] == st.RESEARCHED
    assert record["provider"] == "fake"
    scope = record["scopes"][0]
    assert scope["scope"] == "shared"
    assert scope["kb_id"] == kb["id"]
    assert scope["written"] >= 1
    # THE assertion that keeps the corpus retrievable. A chunk with no vector is invisible to
    # a k-NN query and nothing else in the system would ever embed it.
    assert embedded == [kb["id"]]
    assert scope["embedded"] == 3

    stored = await db.list_kb_documents(kb["id"])
    assert stored and all(d["origin"] == "researched" for d in stored)
    assert all(d["research_batch"] == record["batch"] for d in stored)


async def test_the_pass_is_recorded_on_the_run_row(db, monkeypatch):
    """§5.2: the record is what lets an operator tell "found nothing" from "failed"."""
    kb = await db.create_knowledge_base("target", owner_sub=OWNER, research_for=FOR)
    run_id = await _run_row(db, config={
        "research": {"enabled": True, "personas": False,
                     "targets": {"shared": kb["id"], "personas": {}}},
    })
    _wire(monkeypatch, _Provider([_Hit("https://example.gov/s")]))
    monkeypatch.setattr(
        "matrix_studio.retrieval.embed_pending_kb_chunks",
        lambda *a, **k: _done({"embedded": 1, "cost_usd": 0.0}),
    )

    record = await st.run_research(db, run_id, owner_sub=OWNER)
    on_row = json.loads((await db.get_run(run_id))["research_json"])
    assert on_row == record


def _done(value):
    async def _coro():
        return value

    return _coro()


async def test_finding_nothing_is_a_SUCCESS_not_a_failure(db, monkeypatch):
    """A brief whose authorities are not on the open web is a real answer, and §4's documented
    negative is the artefact that says so. "Nobody looked" and "we looked and there is nothing"
    are different facts, and only the second is reusable."""
    kb = await db.create_knowledge_base("target", owner_sub=OWNER, research_for=FOR)
    run_id = await _run_row(db, config={
        "research": {"enabled": True, "personas": False,
                     "targets": {"shared": kb["id"], "personas": {}}},
    })
    # A provider that returns nothing at all.
    _wire(monkeypatch, _Provider([]), documents=0)
    monkeypatch.setattr(
        "matrix_studio.retrieval.embed_pending_kb_chunks",
        lambda *a, **k: _done({"embedded": 1, "cost_usd": 0.0}),
    )

    record = await st.run_research(db, run_id, owner_sub=OWNER)
    assert record["status"] == st.FOUND_NOTHING
    assert record["scopes"][0]["documents"] == 0
    # The negative IS written, which is the artefact. So the status cannot be derived from
    # rows written — every pass writes at least this one — and is derived from sources found.
    assert record["scopes"][0]["negative"] is True
    assert record["scopes"][0]["written"] >= 1


async def test_a_provider_that_raises_does_not_fail_the_run(db, monkeypatch):
    """One failed query loses that query. A structural failure loses the pass, not the run."""
    kb = await db.create_knowledge_base("target", owner_sub=OWNER, research_for=FOR)
    run_id = await _run_row(db, config={
        "research": {"enabled": True, "personas": False,
                     "targets": {"shared": kb["id"], "personas": {}}},
    })
    _wire(monkeypatch, _Provider(raises=RuntimeError("429 slow down")), documents=0)
    monkeypatch.setattr(
        "matrix_studio.retrieval.embed_pending_kb_chunks",
        lambda *a, **k: _done({"embedded": 0, "cost_usd": 0.0}),
    )

    record = await st.run_research(db, run_id, owner_sub=OWNER)
    # `gather` absorbs a search failure into `unreadable`, so the pass completes and the
    # negative says a source refused us rather than implying there was none.
    assert record["status"] in (st.RESEARCHED, st.FOUND_NOTHING)
    assert record["scopes"][0]["unreadable"] >= 1


async def test_a_target_the_caller_does_not_own_is_refused(db, monkeypatch):
    """A read grant is not a write grant, and `targets` travels in a caller-supplied blob.

    Without this check, naming another user's KB in `config.research.targets` would write
    research into their collection — a side effect on curated data from a run they cannot see.
    """
    theirs = await db.create_knowledge_base("not yours", owner_sub=STRANGER)
    run_id = await _run_row(db, config={
        "research": {"enabled": True, "personas": False,
                     "targets": {"shared": theirs["id"], "personas": {}}},
    })
    _wire(monkeypatch, _Provider([_Hit("https://example.gov/s")]))

    record = await st.run_research(db, run_id, owner_sub=OWNER)

    assert "not owned by this run's owner" in record["scopes"][0]["refused"]
    assert await db.list_kb_documents(theirs["id"]) == []


async def test_a_target_that_has_been_deleted_is_refused_with_the_reason(db, monkeypatch):
    run_id = await _run_row(db, config={
        "research": {"enabled": True, "personas": False,
                     "targets": {"shared": "kb-gone", "personas": {}}},
    })
    _wire(monkeypatch, _Provider([_Hit("https://example.gov/s")]))

    record = await st.run_research(db, run_id, owner_sub=OWNER)
    assert "no longer exists" in record["scopes"][0]["refused"]


async def test_a_scope_with_no_allocated_target_says_so(db, monkeypatch):
    """Rather than inventing a collection at search time. A run whose target was never
    allocated has a configuration problem, and silently creating one would hide it."""
    run_id = await _run_row(db, config={"research": {"enabled": True, "personas": False}})
    _wire(monkeypatch, _Provider([_Hit("https://example.gov/s")]))

    record = await st.run_research(db, run_id, owner_sub=OWNER)
    assert "nowhere to put" in record["scopes"][0]["refused"]


async def test_an_embedding_failure_keeps_the_documents_and_says_they_are_not_retrievable(
    db, monkeypatch,
):
    """The documents are stored and a model mismatch is fixed by configuration rather than by
    re-running the search. So the pass keeps what it found and says plainly that no turn can
    see it yet — which is the difference between a reported problem and an inert feature."""
    kb = await db.create_knowledge_base("target", owner_sub=OWNER, research_for=FOR)
    run_id = await _run_row(db, config={
        "research": {"enabled": True, "personas": False,
                     "targets": {"shared": kb["id"], "personas": {}}},
    })
    _wire(monkeypatch, _Provider([_Hit("https://example.gov/s")]))
    monkeypatch.setattr(
        "matrix_studio.retrieval.embed_pending_kb_chunks",
        lambda *a, **k: _done({"embedded": 0, "error": "index built with another model"}),
    )

    record = await st.run_research(db, run_id, owner_sub=OWNER)
    assert "another model" in record["scopes"][0]["embed_error"]
    assert await db.list_kb_documents(kb["id"]), "the documents must survive"


async def test_research_spend_is_metered(db, monkeypatch):
    """Without this, research is UNMETERED. A dozen model calls per run before turn 1, charged
    nowhere, while the cap's whole job is to refuse the NEXT thing."""
    kb = await db.create_knowledge_base("target", owner_sub=OWNER, research_for=FOR)
    run_id = await _run_row(db, config={
        "research": {"enabled": True, "personas": False,
                     "targets": {"shared": kb["id"], "personas": {}}},
    })
    _wire(monkeypatch, _Provider([_Hit("https://example.gov/s")]))
    monkeypatch.setattr(
        "matrix_studio.retrieval.embed_pending_kb_chunks",
        lambda *a, **k: _done({"embedded": 1, "cost_usd": 0.0}),
    )
    charged = []
    monkeypatch.setattr(
        db, "add_user_spend",
        lambda cost, owner_sub=None: _done(charged.append((cost, owner_sub))),
    )

    record = await st.run_research(db, run_id, owner_sub=OWNER)
    assert charged and charged[0][1] == OWNER
    assert charged[0][0] == pytest.approx(record["cost_usd"])


# --------------------------------------------------------------------------- #
# Re-running replaces, and the record stays small
# --------------------------------------------------------------------------- #


async def test_a_retried_research_step_replaces_its_own_attempt_and_creates_no_collection(
    db, monkeypatch,
):
    """A retried Lambda must be idempotent: the same collections, one batch, nothing new.

    Collections are created once, by the API at allocation, and the Research state never creates
    one — so a retry can only write into what this run was allocated, and write-then-replace means
    its batch supersedes the failed attempt's inside them rather than adding a second copy.
    Driven through the state machine's own entry point, twice, as a Lambda retry would.
    """
    from matrix_studio import step_handlers

    request = await _allocate(db, _searchable_request())
    config, cast = request["config"], request["cast"]
    await db.create_run(run_id="r1", topic="t", cast=cast, name="r1", config=config,
                        owner_sub=OWNER)
    _wire(monkeypatch, _Provider([_Hit("https://example.gov/s")]))
    monkeypatch.setattr(
        "matrix_studio.retrieval.embed_pending_kb_chunks",
        lambda *a, **k: _done({"embedded": 1, "cost_usd": 0.0}),
    )

    async def bound(_owner):
        return db

    monkeypatch.setattr(step_handlers, "_bound", bound)
    event = {"run_id": "r1", "owner_sub": OWNER, "mode": "fresh"}
    collections_before = {k["id"] for k in await db.list_knowledge_bases(owner_sub=OWNER)}

    await step_handlers._research(event)
    first = json.loads((await db.get_run("r1"))["research_json"])
    await step_handlers._research(event)
    second = json.loads((await db.get_run("r1"))["research_json"])

    assert {k["id"] for k in await db.list_knowledge_bases(owner_sub=OWNER)} == collections_before
    assert [sc["kb_id"] for sc in first["scopes"]] == [sc["kb_id"] for sc in second["scopes"]]
    assert first["batch"] != second["batch"]
    for scope in second["scopes"]:
        docs = await db.list_kb_documents(scope["kb_id"])
        assert docs and {d["research_batch"] for d in docs} == {second["batch"]}, (
            "the retry's batch must replace the failed attempt's, not sit beside it"
        )


async def test_a_research_collection_somebody_uploaded_into_is_left_alone(db, monkeypatch):
    """A research collection is a normal collection, and an operator may add their own document
    to it. From then on it is partly curated, and the replace step must not run against it."""
    kb = await db.create_knowledge_base("target", owner_sub=OWNER, research_for=FOR)
    mine = await db.add_kb_document(kb["id"], title="hand-written", text="y" * 400,
                                    char_count=400)
    run_id = await _run_row(db, config={
        "research": {"enabled": True, "personas": False,
                     "targets": {"shared": kb["id"], "personas": {}}},
    })
    _wire(monkeypatch, _Provider([_Hit("https://example.gov/s")]))

    record = await st.run_research(db, run_id, owner_sub=OWNER)

    assert "somebody uploaded" in record["scopes"][0]["refused"]
    assert [d["id"] for d in await db.list_kb_documents(kb["id"])] == [mine]


async def test_the_record_holds_counts_rather_than_contents(db, monkeypatch):
    """Bounded by the size of the cast, so there is no truncation step to get wrong.

    The corpus is reviewable where it belongs — the KB view lists a collection's documents
    (§5.3) — and duplicating passages onto the run row would put a 400 KB item limit between a
    run and a large research pass.
    """
    kb = await db.create_knowledge_base("target", owner_sub=OWNER, research_for=FOR)
    run_id = await _run_row(db, config={
        "research": {"enabled": True, "personas": False,
                     "targets": {"shared": kb["id"], "personas": {}}},
    })
    _wire(monkeypatch, _Provider([_Hit("https://example.gov/s", text="z" * 20000)]))
    monkeypatch.setattr(
        "matrix_studio.retrieval.embed_pending_kb_chunks",
        lambda *a, **k: _done({"embedded": 1, "cost_usd": 0.0}),
    )

    record = await st.run_research(db, run_id, owner_sub=OWNER)
    blob = json.dumps(record)
    assert "z" * 100 not in blob, "no passage text may reach the run row"
    assert len(blob) < 4000, len(blob)


async def test_a_long_error_is_truncated(db, monkeypatch):
    """Long enough to name the provider and the status code, short enough that a stack of them
    cannot threaten the item size limit."""
    from matrix_studio import websearch

    def refuse(preferred=None, **kw):
        raise websearch.SearchUnavailable("x" * 5000)

    monkeypatch.setattr(websearch, "select", refuse)
    run_id = await _run_row(db, config={"research": {"enabled": True}})

    record = await st.run_research(db, run_id, owner_sub=OWNER)
    assert len(record["error"]) == st.MAX_ERROR_CHARS


async def test_a_record_for_a_vanished_run_is_dropped_rather_than_creating_a_phantom(db):
    """DynamoDB's `UpdateItem` UPSERTS. An unconditional write to a missing run manufactures
    one — with a status and no topic, no cast and no `created_at` — which then appears in the
    caller's history."""
    assert await db.set_run_research("no-such-run", {"status": "researched"}) is False
    assert await db.get_run("no-such-run") is None


# --------------------------------------------------------------------------- #
# The brief
# --------------------------------------------------------------------------- #


def test_the_brief_includes_attached_document_text():
    """Measured: without it, a persona's queries drifted off the subject entirely — a
    licensed-plan brief produced "CMS guidance on…", which is human healthcare, because the
    viewpoint alone never said what industry this was."""
    brief = rs.brief_for("Renewing a plan", [
        {"name": "Casey", "document_texts": [{"text": "The specialty plan protocol says…"}]},
    ])
    assert "Renewing a plan" in brief
    assert "specialty plan protocol" in brief


def test_the_brief_caps_each_attachment():
    """The brief is prompt input for query generation, not a corpus. A forty-page attachment
    would crowd out the topic itself."""
    brief = rs.brief_for("t", [{"document_texts": [{"text": "x" * 99999}]}])
    assert len(brief) <= rs.BRIEF_DOCUMENT_CHARS + 10


def test_the_brief_survives_a_malformed_cast():
    """A definition written by hand, or a persona with no attachments. Neither is a reason to
    lose the topic."""
    assert rs.brief_for("t", ["not a dict", {"document_texts": None}]) == "t"


# --------------------------------------------------------------------------- #
# Ensembles: research once, or not at all — never per member
# --------------------------------------------------------------------------- #


async def test_an_ensemble_member_does_not_allocate_its_own_collections(db, monkeypatch):
    """§6, and this is a defect the state's skip alone does NOT cover.

    `create_ensemble` calls `create_run` per member. Allocating there would create a fresh set
    of collections for every member — a ten-member fan-out leaves thirty empty ones bound for
    ever, because each member's Research state then correctly skips the search. The guard is in
    `create_run`, keyed on `ensemble_id`, and it is permanent rather than a placeholder for
    step 6: once the parent allocates, a member inheriting the parent's config already has the
    targets it needs.
    """
    from matrix_studio.api.manager import RunManager

    store = type("S", (), {"for_owner": lambda _self, _sub: db})()
    manager = RunManager(store)
    allocated = []
    monkeypatch.setattr(
        st, "allocate_targets",
        lambda *a, **k: _done(allocated.append(1)),
    )
    monkeypatch.setattr(
        "matrix_studio.orchestration.turn_loop_arn", lambda: "arn:aws:states:::sm/x",
    )

    async def start(*a, **k):
        return {"executionArn": "x"}

    monkeypatch.setattr("matrix_studio.orchestration.start_execution", start)

    await manager.create_run(
        {"topic": "t", "name": "member-1", "cast": [{"name": "Casey"}],
         "config": {"research": {"enabled": True}}},
        owner_sub=OWNER, run_id="m1", ensemble_id="e1", ensemble_cell="base",
    )
    assert allocated == [], "a member allocated its own research collections"


async def test_a_single_run_DOES_allocate(db, monkeypatch):
    """The positive half, so the test above is not passing because allocation never runs."""
    from matrix_studio.api.manager import RunManager

    store = type("S", (), {"for_owner": lambda _self, _sub: db})()
    manager = RunManager(store)
    allocated = []
    monkeypatch.setattr(
        st, "allocate_targets",
        lambda _db, request, **k: _done(allocated.append(1) or request),
    )
    monkeypatch.setattr(
        "matrix_studio.orchestration.turn_loop_arn", lambda: "arn:aws:states:::sm/x",
    )

    async def start(*a, **k):
        return {"executionArn": "x"}

    monkeypatch.setattr("matrix_studio.orchestration.start_execution", start)

    await manager.create_run(
        {"topic": "t", "name": "solo", "cast": [{"name": "Casey"}],
         "config": {"research": {"enabled": True}}},
        owner_sub=OWNER, run_id="s1",
    )
    assert allocated == [1]


# --------------------------------------------------------------------------- #
# Making displacement visible (§5.1)
# --------------------------------------------------------------------------- #
#
# The floor reserves a slot per COLLECTION, not per kind of thing in one. So once research
# writes into a curated collection, the operator's own document competes with the searcher's
# finds — and on run 602ddffe a persona's hand-picked source material lost all three slots to
# researched passages. By the floor's own accounting nothing went wrong: the collection
# contributed.
#
# The decision (private/docs/BACKLOG.md) was to accept the behaviour and make it VISIBLE rather than
# add a third floor — floors that multiply leave every slot reserved and stop ranking deciding
# anything. So `origin` has to survive from the document row to the turn's record, and these
# assert the two hops that carry it.


async def test_origin_travels_with_the_vector_like_authority(db):
    """A grantee cannot read the KB owner's document rows, so a query-time lookup would work
    for the owner and silently return nothing for everyone a collection is shared with —
    which is the same reason `title` and `authority` ride along."""
    kb = await db.create_knowledge_base("mixed", owner_sub=OWNER)
    await db.add_kb_document(kb["id"], title="found", text="x" * 400, char_count=400,
                             origin="researched", authority="controlling")
    await db.add_kb_document(kb["id"], title="mine", text="y" * 400, char_count=400)

    pending = await db.kb_chunks_missing_vectors(kb["id"])
    by_title = {c["title"]: c for c in pending}
    assert by_title["found"]["origin"] == "researched"
    assert by_title["found"]["authority"] == "controlling"
    # Absent rather than "" on a curated document: "not researched" and "judged to be nothing
    # in particular" are different, and only one of them is true here.
    assert not by_title["mine"]["origin"]


def test_a_passage_reports_whether_a_human_chose_it():
    from matrix_studio.retrieval import RetrievedPassage

    found = RetrievedPassage(
        chunk_id=1, document_id="d", title="t", ordinal=0, content="x", score=0.1,
        origin="researched", authority="controlling",
    )
    mine = RetrievedPassage(
        chunk_id=2, document_id="d2", title="t2", ordinal=0, content="y", score=0.2,
    )
    assert found.is_researched is True
    # The default matters: every lexical row and every run-scoped passage arrives without
    # these fields, and this function serves every retrieval mode.
    assert mine.is_researched is False
    assert mine.origin == "" and mine.authority == ""


# --------------------------------------------------------------------------- #
# The standing query (§9.5)
# --------------------------------------------------------------------------- #


def test_the_standing_query_is_built_from_shift_conditions_and_NOT_positions():
    """A query from the position would pull material SUPPORTING it — the confirmation engine §2.2
    exists to prevent. The shift conditions are by construction the evidence that could move them."""
    from matrix_studio.retrieval import standing_query_text

    structured = {"viewpoints": [
        {"position": "Plan renewal is fine without an exam",
         "evidence_that_shifts": ["a state statute defining specialty plans as regulated-only"]},
        {"position": "Volume matters", "evidence_that_shifts": ["a board enforcement action"]},
    ]}
    text = standing_query_text(structured)
    assert "statute defining specialty plans" in text
    assert "board enforcement action" in text
    assert "fine without an exam" not in text
    assert "Volume matters" not in text


def test_no_structured_block_means_no_standing_query():
    from matrix_studio.retrieval import standing_query_text

    assert standing_query_text(None) == ""
    assert standing_query_text({"viewpoints": [{"position": "x"}]}) == ""


def test_the_flag_defaults_off():
    """It changes what reaches a prompt and arrived with its own pre-registered measurement."""
    from matrix_studio.state import RetrievalConfig

    assert RetrievalConfig().standing_query is False
    assert RetrievalConfig.from_config(
        {"retrieval": {"enabled": True, "standing_query": True}}
    ).standing_query is True


# --------------------------------------------------------------------------- #
# Consultants: a library each, found before turn 1 and stored in their own KB
# --------------------------------------------------------------------------- #


def _with_consultant(**research):
    request = _request()
    request["config"]["research"].update(research)
    request["config"]["experts"] = [{"name": "Ada", "expertise": "the practice act"}]
    return request


async def test_a_consultant_gets_its_OWN_collection_bound_to_it(db):
    out = await _allocate(db, _with_consultant())
    targets = out["config"]["research"]["targets"]
    ada = targets["consultants"]["Ada"]
    assert ada not in {targets["shared"], *targets["personas"].values()}
    assert out["config"]["experts"][0]["knowledge_bases"] == [ada]
    # Not in the cast-wide bindings: every persona would otherwise hold the consultant's library.
    assert ada not in out["config"]["knowledge_bases"]


async def test_consultant_research_can_be_switched_off(db):
    out = await _allocate(db, _with_consultant(consultants=False))
    assert out["config"]["research"]["targets"]["consultants"] == {}
    assert "knowledge_bases" not in out["config"]["experts"][0]


async def test_a_consultant_target_never_resolves_to_a_persona_of_the_same_name():
    s = st.Settings(enabled=True, targets={"personas": {"Ada": "kb-p"}, "consultants": {"Ada": "kb-c"}})
    assert s.target_for("Ada") == "kb-p" and s.target_for("Ada", consultant=True) == "kb-c"
    assert st.Settings(enabled=True, targets={"personas": {"Ada": "kb-p"}}).target_for(
        "Ada", consultant=True) is None


async def test_the_pass_fills_the_consultant_s_library(db, monkeypatch):
    kb = await db.create_knowledge_base("Ada's library", owner_sub=OWNER, research_for=FOR)
    config = {
        "research": {"enabled": True, "shared": False, "personas": False,
                     "targets": {"consultants": {"Ada": kb["id"]}}},
        "experts": [{"name": "Ada", "expertise": "the practice act", "knowledge_bases": [kb["id"]]}],
    }
    run_id = await _run_row(db, config=config)
    _wire(monkeypatch, _Provider([_Hit("https://example.gov/act")]))
    seen = []

    async def embed(store, kb_id, **kw):
        seen.append(kb_id)
        return {"embedded": 1}

    monkeypatch.setattr("matrix_studio.retrieval.embed_pending_kb_chunks", embed)
    record = await st.run_research(db, run_id, owner_sub=OWNER)

    [scope] = record["scopes"]
    assert scope["scope"] == "Ada" and scope["consultant"] is True and scope["kb_id"] == kb["id"]
    assert seen == [kb["id"]]
    docs = await db.list_kb_documents(kb["id"])
    assert any(d["source_path"] == "https://example.gov/act" for d in docs)


async def test_the_consultant_query_prompt_names_the_expertise_and_forbids_sides():
    prompts = []

    async def call(messages, model=None, temperature=0.0, max_tokens=None):
        prompts.append(messages[0]["content"])
        return {"content": json.dumps({"queries": ["a", "b", "c", "d"]}), "cost_usd": 0.001,
                "finish_reason": "stop"}

    queries, _ = await rs.consultant_queries("Ada", "the practice act", brief="renewal", call=call)
    assert [q.text for q in queries] == ["a", "b", "c"] and {q.intent for q in queries} == {"reference"}
    assert "the practice act" in prompts[0] and "no side" in prompts[0]


def test_the_forecast_counts_a_collection_per_consultant():
    from matrix_studio import forecast

    request = _with_consultant()
    assert forecast.research_collections(request) == 4
    request["config"]["research"]["consultants"] = False
    assert forecast.research_collections(request) == 3


# --------------------------------------------------------------------------- #
# Research writes only into collections created for the pass (2026-09-30)
# --------------------------------------------------------------------------- #
#
# Observed on the deployed system: a run created from an earlier run's setup, with research on,
# wrote its research INTO the seven curated collections that setup was bound to, and a later run
# from the same setup REPLACED the earlier run's researched documents there. Curated passages were
# crowded out of retrieval, every other run bound to those collections — no-research baselines
# included — read web material it never asked for, and one run's sources were swapped for
# another's. These pin the rule that prevents it: a pass creates its own collections, binds them
# beside the curated ones, and writes nowhere else.


def _wired(monkeypatch, url="https://example.gov/statute"):
    _wire(monkeypatch, _Provider([_Hit(url)]))
    monkeypatch.setattr(
        "matrix_studio.retrieval.embed_pending_kb_chunks",
        lambda *a, **k: _done({"embedded": 1, "cost_usd": 0.0}),
    )


async def _curated(db, name, *, earlier_research=False):
    """A hand-curated collection: two uploaded documents and, optionally, a researched one an
    earlier pass left behind — the shape the deployed collections are in today."""
    kb = await db.create_knowledge_base(name, owner_sub=OWNER)
    for title in ("Source material", "Meeting notes"):
        await db.add_kb_document(kb["id"], title=f"{title} — {name}", text="c" * 400,
                                 char_count=400)
    if earlier_research:
        await db.add_kb_document(kb["id"], title="found earlier", text="e" * 400,
                                 char_count=400, origin="researched", authority="commentary",
                                 research_batch="earlier-batch")
    return kb["id"]


async def _snapshot(db, kb_ids):
    return {kb: sorted(d["id"] for d in await db.list_kb_documents(kb)) for kb in kb_ids}


def _searchable_request(**config):
    """`_request`, with viewpoints the query planner can actually read (position + shift)."""
    request = _request(**config)
    for member, position in zip(request["cast"], ("The statute governs", "Continuity matters")):
        member["structured"] = {"viewpoints": [
            {"position": position, "evidence_that_shifts": ["a board ruling on renewals"]},
        ]}
    return request


def _setup(curated_shared, curated_casey):
    """The setup both runs are created from: curated bindings at the run AND persona level."""
    request = _searchable_request(knowledge_bases=[curated_shared])
    request["cast"][0]["knowledge_bases"] = [curated_casey]
    return request


async def _create_and_research(db, request, run_id):
    """`create_run` then the Research state, as the deployed path does them."""
    request = await st.allocate_targets(
        db, json.loads(json.dumps(request)), owner_sub=OWNER, label=run_id,
        research_for=st.for_run(run_id),
    )
    await db.create_run(run_id=run_id, topic=request["topic"], cast=request["cast"],
                        name=run_id, config=request["config"], owner_sub=OWNER)
    record = await st.run_research(db, run_id, owner_sub=OWNER)
    return request, record


async def test_research_on_a_run_bound_to_curated_collections_leaves_them_untouched(
    db, monkeypatch,
):
    """Not one document added, replaced or deleted in a collection the run merely inherited —
    including the researched document an earlier pass left there, which the old replace step
    would have deleted as "the predecessor"."""
    shared = await _curated(db, "proposal", earlier_research=True)
    casey = await _curated(db, "casey-own", earlier_research=True)
    before = await _snapshot(db, [shared, casey])
    _wired(monkeypatch)

    request, record = await _create_and_research(db, _setup(shared, casey), "run-a")

    assert await _snapshot(db, [shared, casey]) == before
    written = {sc["kb_id"] for sc in record["scopes"] if sc.get("written")}
    assert written and not written & {shared, casey}
    # Bound ALONGSIDE, not instead: the curated collections are still read by every turn.
    targets = request["config"]["research"]["targets"]
    assert request["config"]["knowledge_bases"] == [shared, targets["shared"]]
    by_name = {m["name"]: m for m in request["cast"]}
    assert by_name["Casey"]["knowledge_bases"] == [casey, targets["personas"]["Casey"]]
    for kb_id in written:
        docs = await db.list_kb_documents(kb_id)
        assert docs and all(d["origin"] == "researched" for d in docs)
        assert (await db.get_knowledge_base(kb_id))["research_for"] == st.for_run("run-a")


async def test_two_runs_from_the_same_setup_do_not_touch_each_others_research(db, monkeypatch):
    """The incident's second half: the later run REPLACED the earlier run's research.

    Run B is built the worst way a copy can be: from A's stored config, so it binds A's research
    collections as well as the curated ones and even carries A's targets block. Allocation
    overwrites the targets, so B researches into its own; A's research is exactly as it was.
    """
    shared, casey = await _curated(db, "proposal"), await _curated(db, "casey-own")
    _wired(monkeypatch)
    a_request, a_record = await _create_and_research(db, _setup(shared, casey), "run-a")
    a_research = [sc["kb_id"] for sc in a_record["scopes"]]
    a_before = await _snapshot(db, a_research + [shared, casey])

    copied = {"topic": a_request["topic"], "cast": a_request["cast"],
              "config": a_request["config"]}
    _wired(monkeypatch, url="https://example.gov/a-different-page")
    b_request, b_record = await _create_and_research(db, copied, "run-b")
    b_research = [sc["kb_id"] for sc in b_record["scopes"]]

    assert len(b_research) == 3 and not set(a_research) & set(b_research)
    assert await _snapshot(db, a_research + [shared, casey]) == a_before
    for kb_id in b_research:
        batches = {d["research_batch"] for d in await db.list_kb_documents(kb_id)}
        assert batches == {b_record["batch"]}
    # B READS A's research here, because this copy bound it explicitly — and only reads it.
    b_bound = list(b_request["config"]["knowledge_bases"])
    for member in b_request["cast"]:
        b_bound += member.get("knowledge_bases") or []
    assert set(a_research) <= set(b_bound)


async def test_a_stored_target_from_another_run_is_refused_not_written(db, monkeypatch):
    """The state's own check, without allocation in front of it: a run row whose targets name
    another run's research collections — a crafted config, or a copy that bypassed allocation."""
    shared, casey = await _curated(db, "proposal"), await _curated(db, "casey-own")
    _wired(monkeypatch)
    a_request, a_record = await _create_and_research(db, _setup(shared, casey), "run-a")
    a_research = [sc["kb_id"] for sc in a_record["scopes"]]
    a_before = await _snapshot(db, a_research)

    await db.create_run(run_id="run-b", topic="t", cast=a_request["cast"], name="run-b",
                        config=a_request["config"], owner_sub=OWNER)
    record = await st.run_research(db, "run-b", owner_sub=OWNER)

    assert record["scopes"]
    assert all("not created for this research pass" in sc["refused"] for sc in record["scopes"])
    assert await _snapshot(db, a_research) == a_before


async def test_a_run_created_before_the_fix_cannot_write_into_its_curated_targets(
    db, monkeypatch,
):
    """Runs created before 2026-10-01 carry targets that ARE their curated collections. If such a
    run's Research state ever executes again — an execution in flight across the deploy — it must
    refuse rather than resume writing into them."""
    shared = await _curated(db, "proposal", earlier_research=True)
    before = await _snapshot(db, [shared])
    run_id = await _run_row(db, run_id="legacy", config={
        "knowledge_bases": [shared],
        "research": {"enabled": True, "personas": False,
                     "targets": {"shared": shared, "personas": {}}},
    })
    _wired(monkeypatch)

    record = await st.run_research(db, run_id, owner_sub=OWNER)

    assert "not created for this research pass" in record["scopes"][0]["refused"]
    assert await _snapshot(db, [shared]) == before


async def test_an_ensemble_researches_once_into_fresh_collections_every_member_shares(
    db, monkeypatch,
):
    """§6 with the fix: one pass, into collections marked for the ENSEMBLE, bound beside the
    curated ones — and every member reads that one set. Nothing per member, nothing curated."""
    from matrix_studio import ensemble_spec
    from matrix_studio.api.manager import RunManager

    shared, casey = await _curated(db, "proposal"), await _curated(db, "casey-own")
    before = await _snapshot(db, [shared, casey])
    _wired(monkeypatch)
    monkeypatch.setattr(
        "matrix_studio.orchestration.turn_loop_arn", lambda: "arn:aws:states:::sm/x",
    )

    async def start(*a, **k):
        return {"executionArn": "x"}

    async def no_index(*a, **k):
        return True

    monkeypatch.setattr("matrix_studio.orchestration.start_execution", start)
    monkeypatch.setattr("matrix_studio.storage.vectors.ensure_index_for_kb", no_index)
    store = type("S", (), {"for_owner": lambda _self, _sub: db})()
    collections_before = {k["id"] for k in await db.list_knowledge_bases(owner_sub=OWNER)}

    request = {**_setup(shared, casey), "name": "renewal-ens"}
    out = await RunManager(store).create_ensemble(
        request, ensemble_spec.replicates(n=3), owner_sub=OWNER,
    )

    eid = out["ensemble_id"]
    after = {k["id"] for k in await db.list_knowledge_bases(owner_sub=OWNER)}
    created = after - collections_before
    # 1 shared + 2 personas with viewpoints — once for the ensemble, not once per member.
    assert len(created) == 3
    for kb_id in created:
        assert (await db.get_knowledge_base(kb_id))["research_for"] == st.for_ensemble(eid)
        assert await db.list_kb_documents(kb_id), "the one pass wrote nothing"
    assert await _snapshot(db, [shared, casey]) == before

    rows = [await db.get_run(m["run_id"]) for m in out["members"]]
    assert len(rows) == 3 and all(rows)
    configs = [json.loads(r["config_json"]) for r in rows]
    casts = [json.loads(r["cast_json"]) for r in rows]
    assert all(c["knowledge_bases"] == configs[0]["knowledge_bases"] for c in configs)
    assert configs[0]["knowledge_bases"][0] == shared
    assert set(configs[0]["knowledge_bases"][1:]) <= created
    member_casey = [next(m for m in c if m["name"] == "Casey")["knowledge_bases"] for c in casts]
    assert all(b == member_casey[0] for b in member_casey)
    assert member_casey[0][0] == casey and set(member_casey[0][1:]) <= created


async def test_a_copied_setup_leaves_the_earlier_run_s_research_behind(db, monkeypatch):
    """What a copy of a setup must not carry: the collections research added.

    And for a run from before the fix, whose targets WERE its curated collections, those are kept
    — they hold the operator's own documents and are the bindings they chose.
    """
    shared, casey = await _curated(db, "proposal"), await _curated(db, "casey-own")
    _wired(monkeypatch)
    a_request, a_record = await _create_and_research(db, _setup(shared, casey), "run-a")

    dropped = await st.research_collections_of(db, a_request["config"])
    assert set(dropped) == {sc["kb_id"] for sc in a_record["scopes"]}
    assert shared not in dropped and casey not in dropped

    legacy = {"research": {"enabled": True, "targets": {"shared": shared,
                                                       "personas": {"Casey": casey}}}}
    assert await st.research_collections_of(db, legacy) == []
