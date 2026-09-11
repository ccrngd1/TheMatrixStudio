# Phase 6 design: knowledge bases, grants, and the tenancy exception

Companion to `AWS-SERVERLESS-ARCHITECTURE.md` §5.3/§8b, which give the entity model and
the index-granularity decision. This is the level below: the places where §8b leaves a
choice open, the one place it changes a security invariant, and what the migration
actually costs now that there is live data.

Written before the implementation, as `PHASE5-ORCHESTRATION-DESIGN.md` was. That is not
ceremony — Phase 5's own doc turned out to be **wrong** about the stop check being "exact
at `turn_budget=1`", and finding that in writing first is why the correction was cheap.

Status: **design settled, awaiting go-ahead to implement.**

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

Today `embedding_model()` reads a **global singleton** — `pk="EMBEDDING", sk="META"` in
the `documents` table — recording which model produced the stored vectors. It exists to
catch a same-width model swap, whose vectors are not comparable even though the service
would accept them.

Under index-per-KB a global marker is wrong: two KBs may legitimately have been indexed
at different times with different models, and a query embedded with model B against a KB
indexed with model A returns confident nonsense. **Decision:** the model moves onto the
KB's `META` item, and the mismatch check runs per KB at query time.

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
