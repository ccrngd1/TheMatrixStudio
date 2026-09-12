# Phase 6 design: knowledge bases, grants, and the tenancy exception

Companion to `AWS-SERVERLESS-ARCHITECTURE.md` §5.3/§8b, which give the entity model and
the index-granularity decision. This is the level below: the places where §8b leaves a
choice open, the one place it changes a security invariant, and what the migration
actually costs now that there is live data.

Written before the implementation, as `PHASE5-ORCHESTRATION-DESIGN.md` was. That is not
ceremony — Phase 5's own doc turned out to be **wrong** about the stop check being "exact
at `turn_budget=1`", and finding that in writing first is why the correction was cheap.

Status: **implemented and verified against the live account, 2026-09-12.** §8 records three corrections found while building it, one of which contradicts §6's build order.

---

## 1. What changes, and what deliberately does not

Today one `documents` row conflates three things: the **content**, the **collection** it
belongs to, and the **grant** saying who may read it. It carries `run_id` and
`persona_name`, so a document *is* its own grant, scoped to one conversation. That is
why sharing, reuse and cast-wide visibility all require duplication.

| Entity | Holds | Where |
|---|---|---|
| `document` | Extracted text; chunked and indexed **once, ever** | S3 body + `documents` metadata |
| `knowledge_base` | A named collection. The unit of binding **and** of sharing | `knowledge_bases`, `KB#{kb_id}` / `META` |
| `kb_grant` | Which principals may read a KB | `kb_grants`, `KB#{kb_id}` / `PRINCIPAL#…` |
| `binding` | Which KBs a run or a persona may search | On the run's config |

A document belongs to **exactly one** KB, per §8b — multi-KB membership means either
duplicating chunks or rewriting a `kb_ids` array across every chunk on each membership
change. "This document belongs in two collections" is a KB of one document, bound twice.

**What does not change:** the engine, the turn loop, the state machine, `retrieve_for_turn`'s
contract with its caller, or the shape of a `RetrievedPassage`. Retrieval scoping is
already funnelled through one method (`_slice_filter`) precisely so that this phase could
replace it without touching anything above.

---

## 2. The tenancy exception — the one thing here that is a security change

§3's isolation rests on every partition key being `USER#{sub}`, so credentials pin with
`dynamodb:LeadingKeys` and a request physically cannot read another tenant's partition.
**A shared KB cannot work that way**: the whole point is that a user reads a collection
someone else owns. Access becomes an authorisation *decision* rather than a partition
constraint.

§8b states this rather than hiding it, which is right. But it means this phase introduces
the first documented exception to the isolation model, and exceptions to isolation
invariants are where breaches live. So the exception gets designed, not just accepted.

### 2.1 One chokepoint, and it fails closed

`_slice_filter` is today's single chokepoint — every vector read goes through it, so a
future read cannot forget a clause. That discipline transfers:

```python
async def searchable_kbs(run, persona_name, owner_sub) -> List[str]
```

Every retrieval path resolves its KB list here and nowhere else. Two properties, both
tested and both mutation-tested:

- **Fail closed.** A grant lookup that errors returns `[]` — no KBs, no passages, and the
  prompt says so honestly. It must never fall back to "everything bound", which is the
  natural shape of a `try/except` written for availability. This is the inverse of the
  retrieval fallback rule elsewhere in the codebase: a *retrieval* failure degrades to
  lexical, but an *authorisation* failure denies.
- **Intersection, not union.** The effective scope is
  `(run.knowledge_bases ∪ persona.knowledge_bases) ∩ {KBs this caller may read}`.
  Binding is not permission — §8b: "a grant says *may* read, a binding says *does* read
  in this conversation". A stale binding to a revoked KB must yield nothing.

### 2.2 Grants are keyed for a cheap check, and the owner needs no grant

`kb_grants`: pk `KB#{kb_id}`, sk `PRINCIPAL#USER#{sub}` or `PRINCIPAL#GROUP#{name}`.

The owner's own read needs **no grant row**: `knowledge_bases`'s `META` item carries
`owner_sub`, and owner ⇒ permitted. Without that shortcut every private KB would need a
grant to its own creator, which is a row that can be forgotten and a failure that looks
like a bug in sharing.

For a non-owner the check is one `BatchGetItem` over
`{PRINCIPAL#USER#sub} ∪ {PRINCIPAL#GROUP#g for g in groups}` — one round trip per turn
regardless of group count, because it is a single batch.

### 2.3 Group membership comes from the verified token, and that has a stated window

Groups arrive in the JWT as `cognito:groups`, already verified by the API Gateway
authorizer — the same source `identity.py` uses for `sub`. No Cognito API call per query.

**The honest consequence:** a user removed from a group keeps that group's access until
their token expires. That window is the token lifetime, not indefinite.

This is *not* the leak §8b names. §8b's requirement is that the **grant** is re-checked at
query time, so revoking a grant takes effect on the next turn — and it does, because the
grant is read per query. Group membership is a second, smaller staleness window, and the
alternatives are worse: a Cognito `AdminListGroupsForUser` per turn adds an API call to
the hot path and a new failure mode to the authorisation decision.

**Decision:** trust the token's groups; document the window; revisit if a deployment needs
immediate group revocation, in which case the answer is a shorter token lifetime rather
than a per-turn lookup.

### 2.4 Validation happens twice, on purpose

At run creation, a binding to a KB the caller cannot read is a **422** — fail fast, while
there is a human to read the error. At query time it is re-checked and silently yields
nothing. Both, because the first is good feedback and only the second is a boundary.

---

## 3. One vector index per KB

§8b recommends this over a single index with a `kb_id` filter, because IAM can grant on
individual vector indexes and that recovers the boundary §2 just gave up in DynamoDB.
The costs are real but small: one `QueryVectors` per bound KB, fanned out in parallel.

### 3.1 Merging is correct here and would not have been under BM25

Cosine distance is pairwise between the query and each vector, so results from two
indexes are directly comparable and a merge is a sort. Under BM25 the statistics are
per-index and the scores would not share a scale. Vector retrieval is what makes fan-out
cheap *and* correct.

### 3.2 The similarity floor applies **after** the merge

`apply_similarity_floor` runs once, on the merged list, not per index. Per-index would
apply a global threshold to a local candidate set and change the answer depending on how
documents happened to be grouped into KBs — which is exactly the kind of "it depends how
you organised your files" behaviour a floor exists to avoid.

Same for `k`: each index is queried for `k * 2` (as today) and the merged list is trimmed
to `k` once.

### 3.3 Three things are immutable at index creation, so there is one factory

Dimension, distance metric, and non-filterable metadata keys — all fixed forever, all
already decided (1024 / cosine / `text`). A KB index is therefore created by **one
function with one call site**, never by a `create_index` call written at the point of
need. A KB created with the wrong dimension cannot be repaired, only rebuilt.

### 3.4 The embedding-model marker must become per-KB

`embedding_model()` reads a **global singleton** — `pk="EMBEDDING", sk="META"` in the
`documents` table — recording which model produced the stored vectors, so a same-width
model swap can be refused. A different *width* is refused by the service for free, since
an index's dimension is fixed at creation; a different model at the same width is
accepted, and every distance afterwards is arithmetic nonsense.

**The marker was write-only when this document was first drafted, and that has since been
fixed** (`store_chunk_vectors` now reads it and refuses a mismatch). The original draft
here described the guard as though it existed, which is precisely how the next person
decides not to write one — worth recording as an error in this document rather than
quietly correcting.

Under index-per-KB the global scope becomes wrong. It is *correct* today because there is
one shared index, so all tenants' vectors genuinely share a space and mixing models
corrupts everyone's distances. With per-KB indexes, two KBs may legitimately have been
indexed at different times with different models, and only a query crossing them is a
problem. **Decision:** the model moves onto the KB's `META` item, and the mismatch check
runs per KB at query time.

The migration must carry it: each new per-KB `META` records the model its copied vectors
were actually made with, read from the existing global marker. If that marker is absent —
a corpus embedded before it existed — the migration **refuses rather than defaults**,
because writing a guess asserts a fact nobody knows and defeats the check.

This also removes an oddity: that marker is the one item in the `documents` table that is
not under a user partition, so it already sat outside `LeadingKeys`.

---

## 4. The migration, which is cheaper and safer than expected

### 4.1 What is actually there

48 documents and one marker item, in the deployed account:

| Origin | Count | Owner |
|---|---|---|
| `eval-*` runs (recall measurement corpora) | 46 | `local-single-user` |
| `verify-storage` (Phase 2 verification) | 2 | a real Cognito sub |
| `EMBEDDING`/`META` marker — **not a document** | 1 | none |

**All of it is my own test data.** No user documents exist. That changes what the
migration is *for*: not rescuing data, but **rehearsing the script on disposable data
before it ever touches anything that matters**. That is a better position than either
migrating precious data or skipping the script.

### 4.2 Vectors are copied, not re-embedded

Verified against the real service: `ListVectors(returnData=True)` returns the full
1024-float vector plus its metadata. So migration is `ListVectors` → `PutVectors` into the
new per-KB index.

Two consequences, and the second matters more:

- **Zero Bedrock cost.** No re-embedding.
- **Recall is identical by construction.** The vectors are bit-identical, so there is no
  "did quality survive the migration" question to answer — a question that, given what
  the `distance_to_cosine` bug taught this project, would otherwise need measuring rather
  than assuming.

### 4.3 The `documents` table holds a non-document, and a Scan will find it

The `EMBEDDING`/`META` marker has no `run_id` and no `owner_sub`. A migration that scans
the table and treats every item as a document would mangle it — or, worse, create a KB
for it. The script filters on `pk` beginning `RUN#`, and **asserts** that every skipped
item is one it recognises, so a future non-document item is a loud failure rather than a
silent skip.

### 4.4 The mapping preserves persona scoping

Today a document is scoped to a run and optionally to a persona. The faithful mapping,
which also exercises both binding levels:

- One KB per `(run, persona)` for persona-scoped documents → bound at **persona** level.
- One KB per run for cast-wide documents (`persona_name IS NULL`) → bound at **run** level.

For the real data that is one KB per `eval-*` run (all 46 are cast-wide) plus one
persona KB for the two `Ada` documents — so both paths are exercised by the migration
itself.

### 4.5 The old shared index is left in place

After migrating, `matrix-studio-chunks` still holds every vector. It is not deleted:
deleting an index in a live account is irreversible and the copies want verifying first.
It goes in `BACKLOG.md` alongside the orphaned `thread_messages` table, to be removed on
an explicit go-ahead.

---

## 5. What must not regress, and how that is checked

**Retrieval quality.** §4.2 makes the vectors identical, so the risk is not the data but
the *fan-out and merge*: querying two indexes and merging could rank differently from one
index with a filter. `scripts/measure_retrieval_recall.py` now reproduces recall for
~$0.25, so the check is to split the measurement corpus across two KBs and confirm the
numbers hold. Cheap, and the metric bug is the reason to do it rather than reason about
it.

**Tenancy.** `scripts/verify_tenant_isolation.py` must keep passing unchanged — Phase 6
adds a sharing path, it does not relax the existing one. Plus new negative cases, which
per §8b deserve their own suite: a revoked grant, a grant to a group the user has left, a
binding to a KB the user never had, and a KB owned by another user with no grant at all.

**The turn loop.** `scripts/verify_turn_loop.py`'s 35 checks must keep passing, since
document ingest happens in the machine's prepare state.

---

## 6. Build order

Each step leaves the system working, and the first three touch no existing behaviour.

1. **Storage for KBs and grants** — `knowledge_bases` and `kb_grants` into `_TABLES`,
   CRUD, and `searchable_kbs` with its fail-closed and intersection properties. Tested
   against `moto`, nothing wired.
2. **The per-KB index factory** and the model marker moving onto the KB.
3. **Retrieval fan-out** — `vector_search` over a list of indexes, merge, floor once.
   Behind the existing single-index path until step 5.
4. **Bindings** — run and persona level in the config model, validated at creation with a
   422, re-checked at query time.
5. **The switch** — `searchable_kbs` becomes the scoping used by `retrieve_for_turn`;
   `_slice_filter`'s `run_id` clause is replaced by the KB index selection.
6. **The migration script**, run on the 48 documents.
7. **Re-measure recall** across two KBs; re-run both verify scripts.
8. **Deploy**, then verify grants and revocation against the real account.

*Done when* (from the plan, unchanged): one document, uploaded once, is searchable by two
personas in two different conversations; and a revoked grant stops working **at query
time**, not just at binding time.

---

## 7. Decisions taken here, for the record

| Decision | Alternative rejected | Why |
|---|---|---|
| Groups from the JWT | `AdminListGroupsForUser` per query | An API call on the hot path and a new failure mode in an authorisation decision; the staleness window is bounded by token TTL and is not the leak §8b names |
| Owner needs no grant row | A grant to every creator | A row that can be forgotten, failing in a way that looks like a sharing bug |
| Floor applied post-merge | Per index | A global threshold against a local candidate set makes results depend on how files were grouped |
| Copy vectors | Re-embed | Free, and makes recall identical by construction rather than a thing to measure |
| One KB per run for cast-wide docs | One KB per run, full stop | Would silently drop persona scoping, which exists and is used |
| Old shared index retained | Delete after migrating | Irreversible in a live account; belongs behind an explicit go-ahead |

---

## 8. Corrections found while implementing steps 5–8

Three of them, and the first contradicts §6's build order. Recorded here rather than
quietly implemented differently, because §6 was written before the code was read and a
build order that turns out to be wrong is worth more as a correction than as a deletion.

### 8.1 The run slice STAYS. `_slice_filter`'s `run_id` clause is not retired

§6 step 5 says the `run_id` clause "is replaced by the KB index selection". Taken
literally that is a **ceiling violation**, and the reasoning was already written down —
in `copy_documents_to_run`'s docstring and in §8b of the architecture:

> It does NOT transfer to index-per-RUN, which is the only mapping available before
> Phase 6 introduces KBs — that would cap the whole install at 10,000 conversations,
> and the stated target is "something a large company installs".

S3 Vectors allows 10,000 indexes per bucket. §4.4's migration mapping — one KB per run —
is fine as a **one-off for 46 runs**, but as the standing ingest path it makes every new
conversation with an attachment consume an index, which is exactly the mapping §8b
rejects. Nothing in §4.4 said otherwise; the gap is that the design covered the migration
and never covered what happens when a *new* run attaches a file.

So retrieval reads **two sources and merges them**:

| Source | Index | Scoped by | What it holds |
|---|---|---|---|
| Run slice | the shared `chunks` index | `_slice_filter`: `owner_sub` + `run_id` + `persona_name` | This conversation's own attachments — ad-hoc, deleted with it |
| Bound KBs | one index per KB | `searchable_for_turn`: bindings ∩ grants | Deliberate, reusable, shareable collections |

Merging the two is sound for the same reason merging two KB indexes is (see
`vector_search_kbs`): both are cosine distances against the same query in the same
1024-dimensional space from the same model, so the numbers share a scale and the merge is
a sort. The floor still applies **once, after** the merge (§3.2).

This also means **no existing behaviour changes**. A run with no bindings retrieves
exactly what it retrieves today, through the same filter — which is a far better position
from which to add sharing than a switch that moves every document at once.

The ceiling is not gone, it is moved to where §8b wanted it: 10,000 *knowledge bases*,
which is a number a company does not reach, rather than 10,000 *conversations*, which one
team reaches in a year.

### 8.2 A shared KB's passage titles must travel with the vector

`vector_search_kbs` takes a `titles` map, and its caller was going to build that map from
`list_documents(run_id)`. For a **shared** KB that cannot work, and not for a fiddly
reason:

A KB's documents are rows under **the KB owner's** partition. A grantee's scoped
credentials are pinned with `dynamodb:LeadingKeys` to their own partition, so a reader of
a shared KB **physically cannot read the document row** that holds the title. The title is
not merely inconvenient to fetch; it is unreachable by design, and by the same mechanism
that makes the isolation model work.

A citation renders as `"title #ordinal"`, so without the title every shared passage would
cite as a twelve-character hex id — unreadable in a transcript, and it would make the
second-hand citation ledger (`firsthand_citations`) match on ids no human can check.

So `store_kb_vectors` writes `title` into the vector metadata, and `vector_search_kbs`
prefers it over the map. It is stored **filterable** (the metadata budget is 40 KB per
vector, of which 2 KB filterable; a title is tens of bytes), so no index configuration
changes and the immutable non-filterable key list stays `["text"]`.

### 8.3 The turn loop has no token, so group membership is captured on the run

§2.3 settled group membership as "from the verified token, staleness bounded by token
lifetime". That holds for the API. It does **not** hold in the turn loop: a turn runs in a
Step Functions state, invoked by the state machine, with no JWT anywhere — the run row's
`owner_sub` is all the identity there is.

Three options, and the trade is visible:

| | Freshness | Cost |
|---|---|---|
| `AdminListGroupsForUser` per slice | Fresh every turn | A Cognito call and a new failure mode inside an authorisation decision, plus IAM |
| Capture the creator's groups on the run | Stale for the run's lifetime | One attribute |
| Pass no groups in the turn loop | n/a | Group-granted KBs silently unsearchable — a feature that half-works |

**Decision: capture them on the run row at creation**, from the verified token, and state
the window honestly: a group-granted KB stays searchable by a run started while the user
was a member, for as long as that run lives. The *grant* is still re-read every turn,
which is what §8b actually requires; only membership is stale.

The third option is rejected outright: retrieval that silently ignores a legitimate
binding is the failure mode this phase exists to avoid.

Why not the first, given it is genuinely fresher: it puts an external API call inside the
authorisation path of every turn, and §2.3 already rejected that reasoning for the
per-query case. The per-slice case is cheaper but the new failure mode is the same one.
Recorded in `BACKLOG.md` with a revisit trigger — the first deployment that actually uses
group grants and needs immediate revocation.
