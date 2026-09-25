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
# Allocation: reuse a bound KB, create one only where there is none
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


async def test_allocation_is_a_no_op_when_research_is_off(db):
    """A run without research must be byte-identical to one created before the feature."""
    request = {"topic": "t", "cast": [{"name": "Casey"}], "config": {"max_messages": 4}}
    out = await st.allocate_targets(db, dict(request), owner_sub=OWNER, label="run")
    assert out == request


async def test_a_bound_kb_the_caller_owns_is_reused_rather_than_duplicated(db):
    """§5.1: a persona with a curated collection AND a research collection would be two
    places to look for the same kind of thing, and retrieval would split a budget between
    them for no reason."""
    existing = await db.create_knowledge_base("Casey's statutes", owner_sub=OWNER)
    request = _request(knowledge_bases=[existing["id"]])

    out = await st.allocate_targets(db, request, owner_sub=OWNER, label="run")

    targets = out["config"]["research"]["targets"]
    assert targets["shared"] == existing["id"]
    # No second collection at the cast-wide scope.
    assert out["config"]["knowledge_bases"] == [existing["id"]]


async def test_a_new_kb_is_created_and_BOUND_when_nothing_is_bound(db):
    """The id is pre-allocated into the bindings before anything is searched.

    A run's bindings live in `config_json`, written once, so "research then associate the KB"
    would be a read-modify-write of a JSON blob from a state the machine can retry.
    """
    out = await st.allocate_targets(db, _request(), owner_sub=OWNER, label="renewal")

    shared = out["config"]["research"]["targets"]["shared"]
    assert out["config"]["knowledge_bases"] == [shared]
    row = await db.get_knowledge_base(shared)
    assert row["owner_sub"] == OWNER
    assert "renewal" in row["name"]


async def test_each_persona_gets_their_OWN_target_not_the_cast_wide_one(db):
    """§2.2: a private corpus holds the persona's stance AND the opposition's case.

    Resolved against the persona's own bindings only. `bindings.bound_kbs` returns the union,
    which is right for a TURN — a persona may search the cast-wide collection — and wrong here:
    it would put one persona's research, including the case against them, into the collection
    every other persona reads.
    """
    out = await st.allocate_targets(db, _request(), owner_sub=OWNER, label="run")

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
    out = await st.allocate_targets(db, request, owner_sub=OWNER, label="run")

    assert "Silent" not in out["config"]["research"]["targets"]["personas"]
    assert not [m for m in out["cast"] if m["name"] == "Silent"][0].get("knowledge_bases")


async def test_a_kb_the_caller_does_not_own_is_left_read_only_and_a_new_one_is_added(db):
    """A cast-wide binding may be somebody else's collection, shared read-only.

    Writing into it would be a side effect on data another user curated, arriving from a run
    they cannot see. So research creates its own and binds that IN ADDITION.
    """
    theirs = await db.create_knowledge_base("Somebody else's", owner_sub=STRANGER)
    out = await st.allocate_targets(
        db, _request(knowledge_bases=[theirs["id"]]), owner_sub=OWNER, label="run",
    )

    shared = out["config"]["research"]["targets"]["shared"]
    assert shared != theirs["id"]
    # Still bound — the run may READ it. It is simply not where research writes.
    assert out["config"]["knowledge_bases"] == [theirs["id"], shared]


async def test_a_caller_supplied_targets_block_is_overwritten_not_merged(db):
    """`targets` names collections to WRITE into, and it arrives from a request body.

    The API model does not declare the field, so it is normally dropped before reaching here —
    this asserts the server-side half, which is what holds if that model ever changes.
    """
    out = await st.allocate_targets(
        db,
        _request(research={"enabled": True, "targets": {"shared": "kb-somebody-elses"}}),
        owner_sub=OWNER, label="run",
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
    out = await st.allocate_targets(
        db, _request(), owner_sub=OWNER, label="run", privileged=sentinel,
    )

    targets = out["config"]["research"]["targets"]
    every = {targets["shared"], *targets["personas"].values()}
    assert {kb for _s, kb in seen} == every
    assert all(store is sentinel for store, _kb in seen), (
        "the index must be created with the unscoped store, not the tenant-bound one"
    )


async def test_a_reused_target_also_gets_its_index_ensured(db, monkeypatch):
    """A KB created before the index was made eagerly, or one whose creation half-failed,
    would otherwise be permanently unwritable."""
    existing = await db.create_knowledge_base("curated", owner_sub=OWNER)
    seen = []

    async def ensure(store, kb_id):
        seen.append(kb_id)
        return True

    monkeypatch.setattr("matrix_studio.storage.vectors.ensure_index_for_kb", ensure)
    await st.allocate_targets(
        db, _request(knowledge_bases=[existing["id"]]), owner_sub=OWNER, label="run",
        privileged=object(),
    )
    assert existing["id"] in seen


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
    kb = await db.create_knowledge_base("target", owner_sub=OWNER)
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
    kb = await db.create_knowledge_base("target", owner_sub=OWNER)
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
    kb = await db.create_knowledge_base("target", owner_sub=OWNER)
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
    kb = await db.create_knowledge_base("target", owner_sub=OWNER)
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
    kb = await db.create_knowledge_base("target", owner_sub=OWNER)
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
    kb = await db.create_knowledge_base("target", owner_sub=OWNER)
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


async def test_a_second_pass_replaces_the_first_and_leaves_curation_alone(db, monkeypatch):
    """§5.1: run the same definition twice and the collection would otherwise hold two copies.

    The curated document is the control. An operator who assembled a collection by hand and
    then enabled research must be able to undo the research without rebuilding their own work.
    """
    kb = await db.create_knowledge_base("target", owner_sub=OWNER)
    curated = await db.add_kb_document(
        kb["id"], title="hand-written", text="y" * 400, char_count=400,
    )
    config = {"research": {"enabled": True, "personas": False,
                           "targets": {"shared": kb["id"], "personas": {}}}}
    run_id = await _run_row(db, config=config)
    _wire(monkeypatch, _Provider([_Hit("https://example.gov/s")]))
    monkeypatch.setattr(
        "matrix_studio.retrieval.embed_pending_kb_chunks",
        lambda *a, **k: _done({"embedded": 1, "cost_usd": 0.0}),
    )

    first = await st.run_research(db, run_id, owner_sub=OWNER)
    second = await st.run_research(db, run_id, owner_sub=OWNER)

    assert first["batch"] != second["batch"]
    batches = {d.get("research_batch") for d in await db.list_kb_documents(kb["id"])}
    assert batches == {None, second["batch"]}, "the earlier batch must be gone"
    assert curated in [d["id"] for d in await db.list_kb_documents(kb["id"])]


async def test_the_record_holds_counts_rather_than_contents(db, monkeypatch):
    """Bounded by the size of the cast, so there is no truncation step to get wrong.

    The corpus is reviewable where it belongs — the KB view lists a collection's documents
    (§5.3) — and duplicating passages onto the run row would put a 400 KB item limit between a
    run and a large research pass.
    """
    kb = await db.create_knowledge_base("target", owner_sub=OWNER)
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
# The decision (docs/BACKLOG.md) was to accept the behaviour and make it VISIBLE rather than
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
