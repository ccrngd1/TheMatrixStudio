# Phase 2 key design: the six problems the port has to solve first

Companion to `AWS-SERVERLESS-ARCHITECTURE.md` §4/§4a, which give the table list and
the DynamoDB-vs-S3 rule. This is the level below that: the specific places where the
existing 53 storage methods do something SQLite makes free and DynamoDB does not.

Written before the implementation on purpose. Each of these, decided wrong, is
discovered after the port is written and costs a rewrite of the key schema — and two
of them are silent (wrong ordering, and a stale denormalised field), so they would
not announce themselves.

Status: **design settled, implementation in progress.** Verified premise:
`asyncio.to_thread` + plain boto3 is intercepted by `moto`, including concurrent
writes and sort-key range reads, so the storage layer keeps its `async def`
signatures without an async AWS client.

---

## 1. Numeric sort-key components must be zero-padded

§4 gives `events` the sort key `RUN#{run_id}#{seq}`. Taken literally that is broken:
DynamoDB sorts strings **lexicographically**, so

```
RUN#abc#10   <   RUN#abc#9
```

and `get_events_after(seq)` — a `sk > :marker` range read, the single most-used
access pattern in the app — would silently skip and mis-order events. The event log
is the source of truth that `reconstruct_at_turn` replays, so mis-ordering it
corrupts branch and resume rather than merely displaying something odd.

**Decision:** every numeric sort-key component is zero-padded to a fixed width.

| Key | Format | Width chosen because |
|---|---|---|
| `events.sk` | `RUN#{run_id}#{seq:012d}` | 12 digits ≈ 10¹² events in one run; measured max is 80 |
| `snapshots.sk` | `RUN#{run_id}#{turn:06d}` | 6 digits ≈ 10⁶ turns; the budget is tens |
| `thread_messages.sk` | `MSG#{n:012d}` | same reasoning as events |

Widths are deliberately absurd relative to real values. The cost of a too-wide key
is a few bytes; the cost of a too-narrow one is that ordering breaks at a threshold
nobody is watching for, having worked perfectly for a year.

---

## 2. `get_events` filters by turn, which is not in the sort key

`get_events(run_id, from_turn, to_turn)` is a turn range; the sort key carries
`seq`. Turn and seq are correlated but not interchangeable — several events share a
turn, and the turn boundary is not computable from seq without reading the events.

**Decision:** Query the `RUN#{run_id}#` prefix and filter on the `turn` attribute.
Not a second index: the query is already confined to one run's contiguous items
(measured mean 80 events, max 2.4 KB each ≈ 190 KB), so this reads at most a couple
of pages. A GSI keyed on turn would add write cost to the hottest write path in the
system to save a filter on a small partition.

Same treatment for `truncate_after_turn`, which additionally needs the item keys to
delete — so it queries, filters, then batch-writes deletes.

---

## 3. `list_snapshots` reads a field out of the snapshot body

```python
status = json.loads(row[1]).get("status")
```

Snapshot bodies go to S3 (mean 45 KB, max 2.2 MB, 1 of 619 already over DynamoDB's
400 KB item limit). So this one line, ported naively, becomes **one S3 GET per
checkpoint** to render a list — a 40-turn run would issue 40 GETs of 45 KB each to
display a row of turn numbers.

**Decision:** denormalise `status` onto the DynamoDB pointer item, alongside `turn`,
`created_at` and the S3 key.

This is the same trade §4a already makes for documents (`title`, `chunk_count`), and
it carries the same hazard, stated the same way: **a denormalised field can drift
from the object it describes.** The S3 body is authoritative if they ever disagree.
The mitigation is that `status` is written once, in the same call that writes the
body, and never updated in place — a snapshot is immutable once saved.

---

## 4. `AUTOINCREMENT` has no DynamoDB equivalent, and the ids are load-bearing

`summaries.id` and `thread_messages.id` are SQLite `AUTOINCREMENT` integers, and
they are not internal: `save_summary` and `add_thread_message` **return** `id` to
their callers, and `get_thread_messages` **orders by** it. So they cannot be replaced
with UUIDs without changing the ordering contract, and cannot be dropped without
changing the return shape the tests assert.

Rejected: a timestamp. `int(time.time())` has one-second resolution, and two messages
in the same second would order arbitrarily — a thread where the model replies fast is
exactly when it happens.

**Decision:** an atomic counter item per partition, incremented with
`UpdateItem … ADD seq :1` and `ReturnValues=UPDATED_NEW`. DynamoDB's `ADD` is atomic
and returns the post-increment value, which *is* an autoincrement, and it is
correct under concurrency without a read-modify-write.

Cost: one extra write per inserted summary or thread message. Both are low-frequency
(a summary is once per run; thread messages are human-paced), so this is the right
place to spend a write. **It is deliberately not used for `events`** — the engine
already assigns a monotonic per-run `seq` itself, and putting a counter on the
hottest write path would double its cost to replace a number the caller already has.

---

## 5. Two methods look up by an id with no run or user context

```python
get_thread(thread_id)        # no run_id
document_text(document_id)   # no run_id
delete_document(document_id) # no run_id
```

Under `threads` pk=`RUN#{run_id}` these cannot be a `GetItem` — there is no way to
know the partition. A `Scan` is not an option: it is O(table) and, worse, it reads
across tenants, so a tenancy bug becomes a full-table disclosure rather than a
mistake in one partition.

**Decision:** a GSI on each of `threads` and `documents`, keyed on the id.

This is a **change to the Phase 1 stack**, which shipped without them — cheap now,
and the reason to notice it before writing the port rather than after. The GSIs
project only the keys, so a lookup is GSI query → base-table `GetItem`: two round
trips, but the second one is key-addressed and the first reads one item.

Authorisation is unaffected and still explicit: the caller's `owner_sub` is checked
against the resolved run before anything is returned. The GSI makes the item
*findable*, not *readable* — `api/app.py` already routes both thread endpoints
through `_require_thread_run`, which resolves the run through the scoped
`get_run_by_ref`.

---

## 6. `owner_sub` has to reach the user-partitioned tables, and most signatures lack it

`events` and `snapshots` are partitioned `USER#{sub}` — deliberately, because that
is the only shape `dynamodb:LeadingKeys` can enforce (§4 says so explicitly, and
records that run-partitioning was the earlier, weaker draft). But the methods that
write them do not take a user:

```python
append_event(run_id, turn, seq, event_type, payload, agent_name=None)
save_snapshot(snapshot)
get_snapshot(run_id, turn=None)
max_seq(run_id)
```

Three ways to bridge that, and the choice matters more than it looks:

| Option | Why not / why |
|---|---|
| Partition by `RUN#{run_id}` instead | Rejected in §4 already: it is the one table pair `LeadingKeys` cannot reach, so isolation reverts to "every developer remembers the predicate". |
| `Database` resolves `run_id → owner_sub` internally, with a cache | Tempting — no caller changes. But the lookup item lives outside `USER#{sub}`, so the *scoped credentials cannot read it*, which means the resolution has to happen with wider rights than the request. That reintroduces exactly the ambient authority §3 exists to remove. |
| **Thread `owner_sub` through the signatures** | Chosen. |

**Decision:** `owner_sub` becomes a **required keyword-only argument** on every
method touching a user-partitioned table — the same rule Phase 0.2 established for
the read paths, and for the same reason: a caller that forgets it fails with a
`TypeError` at call time rather than writing into, or reading from, the wrong
partition.

The callers already have it. `api/app.py` resolves every request through
`get_run_by_ref(ref, owner_sub=user)` before doing anything else, and the engine
receives `owner_sub` in its run request (added in Phase 0.2). So this is threading a
value that exists, not inventing one — which is why it is worth the churn across
~25 signatures instead of hiding it behind a cache.

---

## What this implies for the work

- **The CDK stack changes** (item 5): two GSIs, plus nothing else — the tables and
  keys otherwise match §4.
- **~25 signatures gain `owner_sub`** (item 6), and their callers with them. This is
  the bulk of the mechanical work and the reason Phase 2 is the plan's "large" phase.
- **Ordering is the thing to test hardest** (items 1 and 4). A test that writes
  events 1–15 and reads them back must assert the *order*, not just the count:
  unpadded keys return all fifteen, in the wrong sequence, and every count-based
  assertion passes.

## Build order

Build `storage/dynamo.py` complete, test it directly against `moto`, and swap
`storage/__init__.py` only once it passes. Nothing is broken until the swap, and the
swap plus the 41 construction sites is one mechanical step rather than a partially
migrated tree. This is not an abstraction layer — there is no interface and no
runtime switch, just the replacement built before the original is deleted.
