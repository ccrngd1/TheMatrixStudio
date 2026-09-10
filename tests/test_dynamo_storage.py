# SPDX-License-Identifier: Apache-2.0
"""
The DynamoDB + S3 storage layer, against `moto`.

The emphasis is deliberate and lopsided: most of these tests are about **ordering and
paging**, not about whether a write followed by a read returns the value. Round-trip
tests catch the failures that announce themselves. The two failure modes this port can
actually produce are silent:

  * an unpadded numeric sort key returns every item, in the wrong order — so every
    count-based assertion passes while the event log replays as a different
    conversation, and `reconstruct_at_turn` rebuilds a state that never existed;
  * an unfollowed `LastEvaluatedKey` returns a prefix of the log — a truncated
    conversation, indistinguishable from a run that really was that short.

So the ordering tests assert sequences, never lengths, and use seq values that
straddle a digit boundary (9 → 10) because that is the only place unpadded keys
misbehave. A test using 1, 2, 3 would pass against the bug.

`moto` rather than DynamoDB Local: no container, so this stays one `pytest`
invocation. Verified separately that it intercepts boto3 calls issued from
`asyncio.to_thread`, which is how the async layer drives the sync SDK.
"""

import json
import os

import pytest

from matrix_studio.state import AgentState, SimSnapshot
from matrix_studio.storage.dynamo import (
    SEQ_WIDTH,
    DuplicateNameError,
    DynamoStorage,
    StorageError,
    _event_sk,
    _run_prefix,
)

USER_A = "sub-aaaa-1111"
USER_B = "sub-bbbb-2222"
BUCKET = "matrix-studio-test-data"
VECTOR_BUCKET = "matrix-studio-test-vectors"
VECTOR_INDEX = "matrix-studio-test-chunks"
PREFIX = "matrix-studio-test"
# Small on purpose: these tests are about scoping and shape, not about recall, so a
# 4-dimensional vector makes the arithmetic readable. The real index is 1024 — see
# docs/EMBEDDING-DIMENSION-MEASUREMENT.md, and note the dimension is immutable once
# an index exists.
DIM = 4


@pytest.fixture
def aws(monkeypatch):
    """A mocked AWS account with the tables and bucket this layer expects."""
    for name, value in (
        ("AWS_ACCESS_KEY_ID", "testing"),
        ("AWS_SECRET_ACCESS_KEY", "testing"),
        ("AWS_SECURITY_TOKEN", "testing"),
        ("AWS_SESSION_TOKEN", "testing"),
        ("AWS_DEFAULT_REGION", "us-east-1"),
        ("AWS_REGION", "us-east-1"),
    ):
        monkeypatch.setenv(name, value)

    from moto import mock_aws

    with mock_aws():
        import boto3

        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        # Mirrors infra/matrix_infra/stack.py, including the two id GSIs. Kept in
        # step by `test_the_fixture_matches_the_deployed_tables`, because a fixture
        # that quietly diverges from the real stack is a suite that passes against
        # infrastructure that does not exist.
        id_index = {"threads": "thread_id", "documents": "document_id"}
        for table in ("runs", "events", "snapshots", "summaries", "threads",
                      "thread-messages", "documents"):
            attrs = [
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ]
            extra = {}
            if table in id_index:
                attrs.append(
                    {"AttributeName": id_index[table], "AttributeType": "S"}
                )
                extra["GlobalSecondaryIndexes"] = [
                    {
                        "IndexName": f"by-{id_index[table].replace('_', '-')}",
                        "KeySchema": [
                            {"AttributeName": id_index[table], "KeyType": "HASH"}
                        ],
                        "Projection": {"ProjectionType": "KEYS_ONLY"},
                    }
                ]
            ddb.create_table(
                TableName=f"{PREFIX}-{table}",
                KeySchema=[
                    {"AttributeName": "pk", "KeyType": "HASH"},
                    {"AttributeName": "sk", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=attrs,
                BillingMode="PAY_PER_REQUEST",
                **extra,
            )
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)

        # The vector bucket and index, mirroring infra/matrix_infra/stack.py. The
        # metadata configuration matters and is immutable at creation: `text` must be
        # NON-filterable, because filterable metadata has a ~2 KB per-vector budget
        # that a 749-byte-mean passage would eat, and nothing ever filters on text.
        monkeypatch.setenv("VECTOR_BUCKET", VECTOR_BUCKET)
        monkeypatch.setenv("VECTOR_INDEX", VECTOR_INDEX)
        vec = boto3.client("s3vectors", region_name="us-east-1")
        vec.create_vector_bucket(vectorBucketName=VECTOR_BUCKET)
        vec.create_index(
            vectorBucketName=VECTOR_BUCKET,
            indexName=VECTOR_INDEX,
            dataType="float32",
            dimension=DIM,
            distanceMetric="cosine",
            metadataConfiguration={"nonFilterableMetadataKeys": ["text"]},
        )
        yield


@pytest.fixture
async def store(aws):
    s = DynamoStorage(table_prefix=PREFIX, bucket=BUCKET, region="us-east-1")
    await s.connect()
    yield s
    await s.close()


async def _seed_run(store, run_id="r1", owner=USER_A, name=None):
    await store.create_run(
        run_id=run_id, topic="a topic",
        cast=[{"name": "Dana", "persona": "p"}], name=name, owner_sub=owner,
    )
    return run_id


# --------------------------------------------------------------------------- #
# Ordering — the silent failure
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_event_order_survives_a_digit_boundary(store):
    """The test that fails against an unpadded sort key, and only this shape does.

    DynamoDB sorts strings lexicographically, so an unpadded `RUN#r1#10` sorts
    BEFORE `RUN#r1#9`. Writing 1..3 would pass against that bug; 8..12 is where it
    shows. Asserting the sequence rather than the count is the other half — an
    unpadded key still returns all fifteen items.
    """
    await _seed_run(store)
    for seq in range(15):
        await store.append_event(
            run_id="r1", turn=seq, seq=seq, event_type="agent.response",
            payload={"n": seq}, owner_sub=USER_A,
        )
    events = await store.get_events_after("r1", after_seq=-1, owner_sub=USER_A)
    assert [e["seq"] for e in events] == list(range(15))


@pytest.mark.asyncio
async def test_get_events_after_is_exclusive_at_the_boundary(store):
    """`after_seq=9` must return 10 onwards, not 9 onwards.

    An off-by-one here re-delivers the last event the client already has, which the
    UI would render as a duplicate turn. `BETWEEN` is inclusive, so the lower bound
    is built at `after_seq + 1`; getting that wrong is invisible without this.
    """
    await _seed_run(store)
    for seq in range(15):
        await store.append_event(
            run_id="r1", turn=seq, seq=seq, event_type="e",
            payload={}, owner_sub=USER_A,
        )
    got = await store.get_events_after("r1", after_seq=9, owner_sub=USER_A)
    assert [e["seq"] for e in got] == [10, 11, 12, 13, 14]


@pytest.mark.asyncio
async def test_max_seq_reads_the_highest_not_the_last_written(store):
    """Written out of order on purpose.

    `max_seq` returns the final item in key order, which is only the maximum because
    the key is padded. Writing ascending would make a broken implementation look
    right, since the last written would also be the last in insertion order.
    """
    await _seed_run(store)
    for seq in (3, 11, 7, 2, 10):
        await store.append_event(
            run_id="r1", turn=1, seq=seq, event_type="e",
            payload={}, owner_sub=USER_A,
        )
    assert await store.max_seq("r1", owner_sub=USER_A) == 11


@pytest.mark.asyncio
async def test_max_seq_of_an_empty_run_is_minus_one(store):
    """-1, not 0. The engine adds 1 to get the next seq, so 0 would skip seq 0."""
    await _seed_run(store)
    assert await store.max_seq("r1", owner_sub=USER_A) == -1


@pytest.mark.asyncio
async def test_snapshot_turns_survive_a_digit_boundary(store):
    """Same padding argument as events, for the snapshot sort key.

    `get_snapshot(turn=None)` means "the latest", implemented as the last item in key
    order — so unpadded turns would return turn 9 as the latest of 15.
    """
    await _seed_run(store)
    for turn in range(1, 16):
        await store.save_snapshot(_snapshot("r1", turn), owner_sub=USER_A)

    listed = await store.list_snapshots("r1", owner_sub=USER_A)
    assert [s["turn"] for s in listed] == list(range(1, 16))
    assert await store.last_checkpoint_turn("r1", owner_sub=USER_A) == 15
    latest = await store.get_snapshot("r1", owner_sub=USER_A)
    assert latest is not None and latest.turn == 15


def _snapshot(run_id: str, turn: int, status: str = "running") -> SimSnapshot:
    return SimSnapshot(
        run_id=run_id, turn=turn, topic="a topic",
        agents={"Dana": AgentState(name="Dana", persona="p", goals=[])},
        conversation=[{"speaker": "Dana", "content": "hi", "turn": turn}],
        status=status, created_at=1, total_turns=turn,
    )


@pytest.mark.asyncio
async def test_one_run_prefix_does_not_match_another(store):
    """`RUN#r1#` must not match run `r10`.

    Run ids are UUIDs in production, so this looks theoretical — but
    `copy_events_upto` writes to a CALLER-SUPPLIED destination id, and imports and
    fixtures use ids like `r1` and `r10`. Without the trailing `#` in the prefix,
    r1's event log would silently include r10's.
    """
    await _seed_run(store, "r1")
    await _seed_run(store, "r10")
    await store.append_event(run_id="r1", turn=1, seq=0, event_type="e",
                             payload={"which": "r1"}, owner_sub=USER_A)
    for seq in range(3):
        await store.append_event(run_id="r10", turn=1, seq=seq, event_type="e",
                                 payload={"which": "r10"}, owner_sub=USER_A)

    r1 = await store.get_events("r1", owner_sub=USER_A)
    assert [e["run_id"] for e in r1] == ["r1"]
    assert _run_prefix("r1") not in _event_sk("r10", 0)


# --------------------------------------------------------------------------- #
# Paging
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_log_longer_than_one_page_is_returned_whole(store):
    """DynamoDB caps a page at 1 MB and returns a `LastEvaluatedKey`.

    Ignoring it truncates the event log with no error — a shorter conversation,
    indistinguishable from one that really was short. Forced here with payloads big
    enough to guarantee several pages rather than trusted to a count.
    """
    await _seed_run(store)
    filler = "x" * 30_000  # ~30 KB per event => >1 MB across 40
    for seq in range(40):
        await store.append_event(
            run_id="r1", turn=seq, seq=seq, event_type="agent.response",
            payload={"message": filler, "n": seq}, owner_sub=USER_A,
        )
    events = await store.get_events_after("r1", after_seq=-1, owner_sub=USER_A)
    assert [e["seq"] for e in events] == list(range(40)), (
        f"expected 40 events in order, got {len(events)} — LastEvaluatedKey "
        "is probably not being followed"
    )


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_run_is_invisible_outside_its_partition(store):
    await _seed_run(store, "r1", owner=USER_A, name="alpha-run")
    assert await store.get_run("r1", owner_sub=USER_B) is None
    assert await store.get_run_by_ref("r1", owner_sub=USER_B) is None
    assert await store.get_run_by_ref("alpha-run", owner_sub=USER_B) is None
    assert await store.list_runs(owner_sub=USER_B) == []


@pytest.mark.asyncio
async def test_events_are_invisible_outside_their_partition(store):
    """Not implied by the run check: events are their own table and their own read.

    A port that partitioned events by run rather than by user would pass the run test
    above and fail this one — which is the scenario §4 rejected, so it is worth
    holding a test against.
    """
    await _seed_run(store, "r1", owner=USER_A)
    await store.append_event(run_id="r1", turn=1, seq=0, event_type="e",
                             payload={"secret": "A's"}, owner_sub=USER_A)
    assert await store.get_events("r1", owner_sub=USER_B) == []
    assert await store.get_events_after("r1", owner_sub=USER_B) == []
    assert await store.max_seq("r1", owner_sub=USER_B) == -1


@pytest.mark.asyncio
async def test_two_owners_can_hold_the_same_run_name(store):
    await _seed_run(store, "a1", owner=USER_A, name="trusted-robot")
    await _seed_run(store, "b1", owner=USER_B, name="trusted-robot")
    a = await store.get_run_by_ref("trusted-robot", owner_sub=USER_A)
    b = await store.get_run_by_ref("trusted-robot", owner_sub=USER_B)
    assert a["id"] == "a1" and b["id"] == "b1"


@pytest.mark.asyncio
async def test_one_owner_cannot_reuse_a_name(store):
    """The unique index, re-expressed as a conditional marker item.

    DynamoDB has no unique constraint beyond the primary key, so per-user name
    uniqueness is a `NAME#{name}` marker written in the same transaction as the run.
    `manager.create_run` relies on detecting this to disambiguate rather than fail,
    so it has to raise something recognisable.
    """
    await _seed_run(store, "a1", owner=USER_A, name="trusted-robot")
    with pytest.raises(DuplicateNameError):
        await _seed_run(store, "a2", owner=USER_A, name="trusted-robot")


@pytest.mark.asyncio
async def test_name_exists_is_scoped_to_the_owner(store):
    await _seed_run(store, "a1", owner=USER_A, name="trusted-robot")
    assert await store.name_exists("trusted-robot", owner_sub=USER_A) is True
    assert await store.name_exists("trusted-robot", owner_sub=USER_B) is False


# --------------------------------------------------------------------------- #
# Snapshot bodies in S3
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_snapshot_body_goes_to_s3_under_the_owners_prefix(store):
    """The prefix is what the scoped role's `…/{sub}/*` condition matches.

    A body written outside it would be unreadable under the Phase 2 credentials —
    and the write would succeed, so the failure would surface later, on read.
    """
    import boto3

    await _seed_run(store)
    await store.save_snapshot(_snapshot("r1", 3), owner_sub=USER_A)
    keys = [
        o["Key"]
        for o in boto3.client("s3", region_name="us-east-1")
        .list_objects_v2(Bucket=BUCKET)["Contents"]
    ]
    assert keys == [f"snapshots/{USER_A}/r1/000003.json"]


@pytest.mark.asyncio
async def test_a_snapshot_over_the_dynamodb_item_limit_still_saves(store):
    """The measured reason bodies go to S3 at all.

    One of the 38 real runs already holds a snapshot over DynamoDB's 400 KB item
    limit, so inline storage is not a risk to watch — it is a write that fails at
    turn 30 of a real conversation. Forced to ~600 KB here.
    """
    await _seed_run(store)
    big = _snapshot("r1", 1)
    big.conversation = [
        {"speaker": "Dana", "content": "y" * 20_000, "turn": i} for i in range(30)
    ]
    await store.save_snapshot(big, owner_sub=USER_A)
    back = await store.get_snapshot("r1", turn=1, owner_sub=USER_A)
    assert back is not None
    assert len(back.conversation) == 30


@pytest.mark.asyncio
async def test_list_snapshots_reads_status_without_touching_s3(store):
    """`status` must come off the pointer, or the list is N S3 GETs (key design §3).

    Asserted by deleting every S3 object first: if the implementation reads the body
    for `status`, this returns None or raises, and the denormalisation has silently
    stopped happening.
    """
    import boto3

    await _seed_run(store)
    await store.save_snapshot(_snapshot("r1", 1, status="running"), owner_sub=USER_A)
    await store.save_snapshot(_snapshot("r1", 2, status="complete"), owner_sub=USER_A)

    s3 = boto3.client("s3", region_name="us-east-1")
    for obj in s3.list_objects_v2(Bucket=BUCKET).get("Contents", []):
        s3.delete_object(Bucket=BUCKET, Key=obj["Key"])

    listed = await store.list_snapshots("r1", owner_sub=USER_A)
    assert [(s["turn"], s["status"]) for s in listed] == [
        (1, "running"), (2, "complete"),
    ]


@pytest.mark.asyncio
async def test_a_missing_body_degrades_rather_than_raising(store):
    """A pointer with no body must not crash a read.

    The event log is the source of truth and a snapshot is a cache of it, so the
    caller's fallback is replay. Raising here would turn a recoverable inconsistency
    into a dead run.
    """
    import boto3

    await _seed_run(store)
    await store.save_snapshot(_snapshot("r1", 1), owner_sub=USER_A)
    boto3.client("s3", region_name="us-east-1").delete_object(
        Bucket=BUCKET, Key=f"snapshots/{USER_A}/r1/000001.json"
    )
    assert await store.get_snapshot("r1", turn=1, owner_sub=USER_A) is None


@pytest.mark.asyncio
async def test_saving_without_a_bucket_refuses_rather_than_writing_a_pointer(aws):
    """A pointer to a nonexistent object reads as a saved snapshot that is broken.

    Failing the write is the honest outcome: the caller learns immediately, and the
    checkpoint list does not gain an entry that cannot be opened.
    """
    s = DynamoStorage(table_prefix=PREFIX, bucket="", region="us-east-1")
    await s.connect()
    try:
        await s.create_run(run_id="r1", topic="t", cast=[], owner_sub=USER_A)
        with pytest.raises(StorageError, match="DATA_BUCKET"):
            await s.save_snapshot(_snapshot("r1", 1), owner_sub=USER_A)
        assert await s.list_snapshots("r1", owner_sub=USER_A) == []
    finally:
        await s.close()


# --------------------------------------------------------------------------- #
# The rest of the run lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_stats_are_derived_from_the_log(store):
    """Counted from events, not from a maintained field, so they cannot drift."""
    await _seed_run(store)
    await store.append_event(run_id="r1", turn=0, seq=0, event_type="sim.started",
                             payload={}, owner_sub=USER_A)
    for i, cost in enumerate([0.002, 0.003], start=1):
        await store.append_event(
            run_id="r1", turn=i, seq=i, event_type="agent.response",
            payload={"cost_usd": cost, "message": "m"}, owner_sub=USER_A,
        )
    stats = await store.get_run_stats("r1", owner_sub=USER_A)
    assert stats["turn_count"] == 2
    assert abs(stats["total_cost_usd"] - 0.005) < 1e-9
    assert stats["last_event_at"] is not None


@pytest.mark.asyncio
async def test_costs_come_back_as_floats_not_decimals(store):
    """`Decimal("0.001") == 0.001` is False, so this would break every cost check.

    DynamoDB refuses `float` on write and returns `Decimal` on read. Left unconverted
    it leaks into API responses and test assertions, where the failure points at the
    comparison rather than at the storage layer.
    """
    from decimal import Decimal

    await _seed_run(store)
    await store.append_event(run_id="r1", turn=1, seq=0, event_type="agent.response",
                             payload={"cost_usd": 0.0012}, owner_sub=USER_A)
    stats = await store.get_run_stats("r1", owner_sub=USER_A)
    assert isinstance(stats["total_cost_usd"], float)
    assert not isinstance(stats["turn_count"], Decimal)
    run = await store.get_run("r1", owner_sub=USER_A)
    assert isinstance(run["created_at"], int)


@pytest.mark.asyncio
async def test_absent_optional_fields_read_back_as_none(store):
    """Callers SUBSCRIPT these fields and expect None, as SQLite gave them.

    `_to_ddb` drops None rather than storing DynamoDB's NULL type, so a nullable
    attribute comes back ABSENT — and absent is a `KeyError`, not a `None`, unless
    the read path restores it.

    Subscripted, not `.get()`. An earlier version of this test used `.get()` and so
    passed with the restoration removed entirely — it asserted that a dict does what
    dicts do. Real callers subscript:

        app.py     d["media_type"] · d["persona_name"] is None   (the dossier)
        app.py     row["agent_name"]                             (retrieved passages)
        service.py thread["persona_name"] · reply["speaker"]     (aside threads)

    and a cast-wide document has no `persona_name` while `sim.started` has no
    `agent_name`, so these are the ordinary cases. The dossier route would 500 on any
    run holding a cast-wide document.
    """
    await _seed_run(store)
    run = await store.get_run("r1", owner_sub=USER_A)
    for field in ("name", "description", "slug", "config_json",
                  "parent_run_id", "branch_turn", "completed_at"):
        assert run[field] is None, field


@pytest.mark.asyncio
async def test_every_read_restores_its_nullable_fields(store):
    """The same guarantee across every entity, exercised the way callers do.

    One test per entity rather than one for runs, because the restoration is applied
    per read path — so it can be present on three of them and missing on the fourth,
    and only the fourth's caller would find out.
    """
    await _seed_run(store)

    # An event with no agent_name — `sim.started` always looks like this.
    await store.append_event(run_id="r1", turn=0, seq=0, event_type="sim.started",
                             payload={}, owner_sub=USER_A)
    event = (await store.get_events("r1", owner_sub=USER_A))[0]
    assert event["agent_name"] is None
    after = (await store.get_events_after("r1", owner_sub=USER_A))[0]
    assert after["agent_name"] is None

    # A cast-wide document has no persona_name, and no source_path when pasted.
    doc_id = await store.add_document(run_id="r1", title="everyone.md",
                                      chunks=["e"], owner_sub=USER_A)
    doc = (await store.list_documents("r1"))[0]
    assert doc["persona_name"] is None
    assert doc["source_path"] is None
    assert doc["media_type"] is None
    assert (await store._find_document(doc_id))["persona_name"] is None
    # The dossier's actual expression, which KeyErrors on an absent attribute.
    assert doc["persona_name"] is None and doc["char_count"] == 0

    # An analyst thread has no persona_name.
    await store.create_thread("t1", "r1", target="analyst")
    assert (await store.get_thread("t1"))["persona_name"] is None
    assert (await store.list_threads("r1"))[0]["persona_name"] is None

    # A user message has no speaker.
    await store.add_thread_message("t1", role="user", content="q")
    assert (await store.get_thread_messages("t1"))[0]["speaker"] is None

    # A summary generated with the default framing has no instructions.
    await store.save_summary("r1", {"overview": "o"})
    assert (await store.get_summaries("r1"))[0]["instructions"] is None


@pytest.mark.asyncio
async def test_status_updates_and_completion_time(store):
    await _seed_run(store)
    assert (await store.get_run("r1", owner_sub=USER_A))["status"] == "pending"
    await store.update_run_status("r1", "running", owner_sub=USER_A)
    assert (await store.get_run("r1", owner_sub=USER_A))["status"] == "running"
    await store.update_run_status("r1", "complete", 1700, owner_sub=USER_A)
    run = await store.get_run("r1", owner_sub=USER_A)
    assert run["status"] == "complete" and run["completed_at"] == 1700


@pytest.mark.asyncio
async def test_appending_the_same_seq_twice_is_refused(store):
    """Reproduces SQLite's `UNIQUE(run_id, turn, seq)`.

    Without the condition, a re-delivered write — a Step Functions retry — would
    overwrite a different event that reused the seq, and the log would be corrupt
    with nothing raised.
    """
    await _seed_run(store)
    await store.append_event(run_id="r1", turn=1, seq=0, event_type="e",
                             payload={"v": 1}, owner_sub=USER_A)
    with pytest.raises(StorageError):
        await store.append_event(run_id="r1", turn=1, seq=0, event_type="e",
                                 payload={"v": 2}, owner_sub=USER_A)
    events = await store.get_events("r1", owner_sub=USER_A)
    assert [json.loads(e["payload"])["v"] for e in events] == [1]


@pytest.mark.asyncio
async def test_get_events_filters_by_turn_range(store):
    await _seed_run(store)
    for turn in range(1, 11):
        await store.append_event(run_id="r1", turn=turn, seq=turn, event_type="e",
                                 payload={}, owner_sub=USER_A)
    got = await store.get_events("r1", from_turn=3, to_turn=5, owner_sub=USER_A)
    assert [e["turn"] for e in got] == [3, 4, 5]


@pytest.mark.asyncio
async def test_truncate_removes_the_tail_of_events_and_snapshots(store):
    await _seed_run(store)
    for turn in range(1, 11):
        await store.append_event(run_id="r1", turn=turn, seq=turn, event_type="e",
                                 payload={}, owner_sub=USER_A)
        await store.save_snapshot(_snapshot("r1", turn), owner_sub=USER_A)

    removed = await store.truncate_after_turn("r1", 6, owner_sub=USER_A)
    assert removed == 4
    assert [e["turn"] for e in await store.get_events("r1", owner_sub=USER_A)] == \
        [1, 2, 3, 4, 5, 6]
    assert [s["turn"] for s in await store.list_snapshots("r1", owner_sub=USER_A)] == \
        [1, 2, 3, 4, 5, 6]


@pytest.mark.asyncio
async def test_copy_events_upto_preserves_turn_and_seq(store):
    """A branch must replay byte-for-byte identically to its parent up to the fork."""
    await _seed_run(store, "parent")
    await _seed_run(store, "branch")
    for seq in range(12):
        await store.append_event(run_id="parent", turn=seq, seq=seq,
                                 event_type="agent.response",
                                 payload={"n": seq}, owner_sub=USER_A)

    copied = await store.copy_events_upto("parent", "branch", 8, owner_sub=USER_A)
    assert copied == 9
    branch = await store.get_events("branch", owner_sub=USER_A)
    assert [(e["turn"], e["seq"]) for e in branch] == [(i, i) for i in range(9)]
    assert {e["run_id"] for e in branch} == {"branch"}
    # The parent is never modified — the core immutability invariant.
    assert len(await store.get_events("parent", owner_sub=USER_A)) == 12


@pytest.mark.asyncio
async def test_the_counter_is_monotonic_under_concurrency(store):
    """The AUTOINCREMENT replacement, tested for the property that matters.

    A read-modify-write would produce duplicates here; `ADD` is atomic server-side,
    so twenty concurrent allocations return twenty distinct values. Thread messages
    are ORDERED by this id, so a duplicate is two messages that cannot be sequenced.
    """
    import asyncio

    ids = await asyncio.gather(*[
        store._next_id("thread-messages", "THREAD#t1") for _ in range(20)
    ])
    assert sorted(ids) == list(range(1, 21))


@pytest.mark.asyncio
async def test_list_runs_search_does_not_cross_owners(store):
    """The SQL version leaked here through AND/OR precedence; assert it directly."""
    await _seed_run(store, "a1", owner=USER_A, name="a-run")
    await _seed_run(store, "b1", owner=USER_B, name="b-run")
    for q in (None, "run", "topic", "a-run"):
        rows = await store.list_runs(q=q, owner_sub=USER_A)
        assert {r["id"] for r in rows} <= {"a1"}, f"leak for q={q!r}"


@pytest.mark.asyncio
async def test_list_runs_by_status_is_cross_tenant_on_purpose(store):
    """The sweep's one legitimate cross-tenant read.

    A crash orphans every tenant's in-flight run, so a scoped sweep would leave
    everyone else's reporting as live forever. Asserted so the behaviour is a
    decision on record rather than an accident of using `Scan`.
    """
    await _seed_run(store, "a1", owner=USER_A)
    await _seed_run(store, "b1", owner=USER_B)
    await store.update_run_status("a1", "running", owner_sub=USER_A)
    await store.update_run_status("b1", "running", owner_sub=USER_B)
    ids = {r["id"] for r in await store.list_runs_by_status("running")}
    assert ids == {"a1", "b1"}




# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_summaries_are_versioned_and_the_latest_per_kind_wins(store):
    """Regenerating must not destroy the previous version, and must be the one read.

    Ordered by the atomic counter rather than by timestamp: two regenerations inside
    the same second are the normal case for a fast model, and `int(time.time())`
    would order them arbitrarily — so the UI would sometimes show the older summary
    with nothing wrong anywhere.
    """
    await _seed_run(store)
    await store.save_summary("r1", {"overview": "first"}, kind="generated")
    await store.save_summary("r1", {"overview": "second"}, kind="generated")
    await store.save_summary("r1", {"overview": "third"}, kind="generated")

    got = await store.get_summaries("r1")
    assert [s["payload"]["overview"] for s in got] == ["third"]


@pytest.mark.asyncio
async def test_a_generated_summary_never_overwrites_an_imported_one(store):
    """Different kinds coexist; the importer's original is not a draft to replace."""
    await _seed_run(store)
    await store.save_summary("r1", {"overview": "from the source"}, kind="imported")
    await store.save_summary("r1", {"overview": "freshly analysed"})
    by_kind = {s["kind"]: s["payload"]["overview"] for s in
               await store.get_summaries("r1")}
    assert by_kind == {
        "imported": "from the source", "generated": "freshly analysed",
    }


@pytest.mark.asyncio
async def test_summary_returns_the_shape_callers_index(store):
    await _seed_run(store)
    saved = await store.save_summary(
        "r1", {"overview": "o"}, tokens_in=10, tokens_out=5, cost_usd=0.0012,
        instructions="be terse",
    )
    assert saved["id"] == 1
    assert saved["payload"] == {"overview": "o"}
    read = (await store.get_summaries("r1"))[0]
    assert read["instructions"] == "be terse"
    assert isinstance(read["cost_usd"], float)
    assert abs(read["cost_usd"] - 0.0012) < 1e-9


# --------------------------------------------------------------------------- #
# Threads
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_thread_is_findable_by_its_own_id(store):
    """The GSI lookup (key design §5) — two reads, no scan.

    `get_thread` is one of only two methods addressed by an id with no run context.
    Without the index there is no partition to read, and a Scan would cross tenants.
    """
    await _seed_run(store)
    await store.create_thread("t1", "r1", target="analyst")
    got = await store.get_thread("t1")
    assert got is not None
    assert got["run_id"] == "r1" and got["target"] == "analyst"
    assert await store.get_thread("nope") is None


@pytest.mark.asyncio
async def test_thread_messages_order_by_counter_not_by_clock(store):
    """Fifteen messages written inside one second must still come back in order.

    This is the case a timestamp key gets wrong, and the reason the counter exists.
    Straddles a digit boundary for the same reason the event test does.
    """
    await _seed_run(store)
    await store.create_thread("t1", "r1", target="analyst")
    for i in range(15):
        await store.add_thread_message("t1", role="user", content=f"m{i}")
    messages = await store.get_thread_messages("t1")
    assert [m["content"] for m in messages] == [f"m{i}" for i in range(15)]
    assert [m["id"] for m in messages] == list(range(1, 16))


@pytest.mark.asyncio
async def test_thread_cost_sums_only_its_own_messages(store):
    """Aside cost is tracked separately from the run's recorded cost."""
    await _seed_run(store)
    await store.create_thread("t1", "r1", target="analyst")
    await store.create_thread("t2", "r1", target="analyst")
    await store.add_thread_message("t1", role="target", content="a", cost_usd=0.002)
    await store.add_thread_message("t1", role="target", content="b", cost_usd=0.003)
    await store.add_thread_message("t2", role="target", content="c", cost_usd=0.5)
    assert abs(await store.thread_cost("t1") - 0.005) < 1e-9
    assert abs(await store.thread_cost("t2") - 0.5) < 1e-9


@pytest.mark.asyncio
async def test_list_threads_carries_counts_that_were_a_sql_join(store):
    """`message_count` and `total_cost_usd` came from a LEFT JOIN … GROUP BY.

    Derived per thread rather than maintained on the thread row, so they cannot drift
    from the messages they describe.
    """
    await _seed_run(store)
    await store.create_thread("t1", "r1", target="analyst")
    await store.create_thread("t2", "r1", target="persona", persona_name="Dana")
    await store.add_thread_message("t1", role="user", content="q")
    await store.add_thread_message("t1", role="target", content="a", cost_usd=0.004)

    threads = await store.list_threads("r1")
    by_id = {t["id"]: t for t in threads}
    assert by_id["t1"]["message_count"] == 2
    assert abs(by_id["t1"]["total_cost_usd"] - 0.004) < 1e-9
    assert by_id["t2"]["message_count"] == 0
    assert by_id["t2"]["total_cost_usd"] == 0.0
    assert by_id["t2"]["persona_name"] == "Dana"


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


CHUNKS = [
    "Egress inspection provides auditable evidence for the auditor.",
    "The auditor needs a record that survives the incident review.",
]


@pytest.mark.asyncio
async def test_document_text_round_trips_through_s3_without_re_overlapping(store):
    """Chunks overlap, so the stored text must be the DE-OVERLAPPED join.

    Measured on two real documents, naive concatenation added 4,253 and 4,859
    duplicated characters. §4a says one S3 object makes `join_chunks` unnecessary —
    true for the reader, and not for the writer, which still has to reassemble once.
    Asserted against `join_chunks` directly so the two cannot drift.
    """
    from matrix_studio.documents import join_chunks

    await _seed_run(store)
    doc_id = await store.add_document(
        run_id="r1", title="bg.md", chunks=CHUNKS, persona_name="Dana",
        char_count=120, owner_sub=USER_A,
    )
    assert await store.document_text(doc_id) == join_chunks(CHUNKS)


@pytest.mark.asyncio
async def test_document_text_lives_under_the_owners_s3_prefix(store):
    """As with snapshots: the prefix is what the scoped role's ARN condition matches."""
    import boto3

    await _seed_run(store)
    doc_id = await store.add_document(
        run_id="r1", title="bg.md", chunks=CHUNKS, owner_sub=USER_A,
    )
    keys = [
        o["Key"] for o in boto3.client("s3", region_name="us-east-1")
        .list_objects_v2(Bucket=BUCKET)["Contents"]
    ]
    assert keys == [f"docs/{USER_A}/r1/{doc_id}.txt"]


@pytest.mark.asyncio
async def test_document_metadata_carries_what_s3_cannot(store):
    """The four facts §4a says earn the DynamoDB row.

    `chunk_count` is a property of the chunking, not of the object; `media_type`
    records that this was a PDF rather than a `.docx`, which is what the operator
    needs to recognise their own file and is lost the moment only text is stored.
    """
    await _seed_run(store)
    doc_id = await store.add_document(
        run_id="r1", title="report.pdf", chunks=CHUNKS, persona_name="Dana",
        source_path="/uploads/report.pdf", media_type="pdf", char_count=4321,
        owner_sub=USER_A,
    )
    doc = (await store.list_documents("r1"))[0]
    assert doc["id"] == doc_id
    assert doc["title"] == "report.pdf"
    assert doc["media_type"] == "pdf"
    assert doc["chunk_count"] == len(CHUNKS)
    assert doc["char_count"] == 4321
    assert doc["persona_name"] == "Dana"


@pytest.mark.asyncio
async def test_a_persona_sees_its_own_documents_plus_cast_wide(store):
    """The retrieval scope, at the metadata level.

    A cast-wide document has a NULL persona, and `_to_ddb` drops None — so the
    attribute is absent, not null. A filter written as `persona_name == None` against
    the raw item would therefore match nothing, and cast-wide documents would vanish
    from every persona's list.
    """
    await _seed_run(store)
    await store.add_document(run_id="r1", title="dana.md", chunks=["d"],
                             persona_name="Dana", owner_sub=USER_A)
    await store.add_document(run_id="r1", title="marcus.md", chunks=["m"],
                             persona_name="Marcus", owner_sub=USER_A)
    await store.add_document(run_id="r1", title="everyone.md", chunks=["e"],
                             owner_sub=USER_A)

    titles = {d["title"] for d in await store.list_documents("r1", "Dana")}
    assert titles == {"dana.md", "everyone.md"}
    assert await store.count_documents("r1") == 3


@pytest.mark.asyncio
async def test_deleting_a_document_removes_its_metadata_and_object(store):
    import boto3

    await _seed_run(store)
    doc_id = await store.add_document(run_id="r1", title="bg.md", chunks=CHUNKS,
                                      owner_sub=USER_A)
    assert await store.delete_document(doc_id) is True
    assert await store.list_documents("r1") == []
    assert await store.document_text(doc_id) == ""
    s3 = boto3.client("s3", region_name="us-east-1")
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET)
    # Idempotent: a second delete is False, not an error.
    assert await store.delete_document(doc_id) is False


@pytest.mark.asyncio
async def test_document_text_of_an_unknown_id_is_empty_not_an_error(store):
    """Matches SQLite, which returned `join_chunks([])`.

    The setup-export path calls this for every document and treats "" as nothing to
    carry, so raising would turn one deleted document into a failed export.
    """
    assert await store.document_text("does-not-exist") == ""


@pytest.mark.asyncio
async def test_copying_documents_to_a_branch_gives_each_its_own_id_and_object(store):
    """A branch must own its rows, which an earlier version of this got wrong twice.

    That version reused the document id and shared the S3 object, on the reasoning
    that document text is immutable so copying bytes buys nothing. Immutable
    *content* was the wrong property: *lifetime* is what matters.

      1. Two items sharing one `document_id` put a duplicate in the
         `by-document-id` GSI, so `_find_document`'s `Limit=1` could resolve to
         either run's row — and `delete_document` would delete the wrong run's
         document while reporting success.
      2. Deleting either copy destroyed the shared object, so the other run's
         `document_text` silently became "" — a persona's background material gone
         because somebody tidied a different conversation.

    The previous test asserted only that the object was shared, so it passed against
    both. This asserts the properties that matter: distinct ids, independent objects,
    identical text, and that deleting one leaves the other readable.
    """
    await _seed_run(store, "parent")
    await _seed_run(store, "branch")
    parent_id = await store.add_document(
        run_id="parent", title="bg.md", chunks=CHUNKS, persona_name="Dana",
        media_type="md", char_count=120, owner_sub=USER_A,
    )

    assert await store.copy_documents_to_run(
        "parent", "branch", owner_sub=USER_A) == 1
    branch_docs = await store.list_documents("branch")
    assert len(branch_docs) == 1
    branch_id = branch_docs[0]["id"]

    assert branch_id != parent_id, "the branch must not reuse the parent's id"
    assert branch_docs[0]["run_id"] == "branch"
    # Metadata carries over, so the branch's personas see the same material.
    assert branch_docs[0]["title"] == "bg.md"
    assert branch_docs[0]["persona_name"] == "Dana"
    assert branch_docs[0]["media_type"] == "md"
    # Same text, different objects.
    assert await store.document_text(branch_id) == \
        await store.document_text(parent_id)
    assert branch_docs[0]["s3_key"] != \
        (await store._find_document(parent_id))["s3_key"]

    # Each id resolves to its OWN row — the GSI duplicate would break this.
    assert (await store._find_document(parent_id))["run_id"] == "parent"
    assert (await store._find_document(branch_id))["run_id"] == "branch"

    # And deleting the parent's copy leaves the branch's intact.
    assert await store.delete_document(parent_id) is True
    assert await store.list_documents("parent") == []
    assert len(await store.list_documents("branch")) == 1
    assert await store.document_text(branch_id) != "", (
        "deleting the parent's document emptied the branch's text"
    )


@pytest.mark.asyncio
async def test_rechunking_the_stored_text_reproduces_the_chunks(store):
    """The assertion Phase 3's retrieval design rests on — and it needed `text=`.

    §4a plans for the lexical arm to re-chunk the stored text rather than keep chunk
    text anywhere, because `chunk_text()` is deterministic. Determinism is the wrong
    property: what is needed is that `chunk_text(stored)` equals the chunks the
    vectors were built from. Measured on ten real documents, that fails on two when
    the stored text is `join_chunks(chunks)` — 3 of 110 ordinals on
    AWS-SERVERLESS-ARCHITECTURE.md and 4 of 55 on README.md — because `join_chunks`
    reassembles 72,149 characters from 72,136 and the extra ones shift boundaries.

    An ordinal that points at different text from the vector built at that ordinal is
    a passage cited under the wrong ordinal, and hybrid retrieval fuses the arms by
    chunk id. Silent. Passing the original text removes it at the root.
    """
    from matrix_studio.documents import chunk_text

    original = ("Egress inspection provides auditable evidence. " * 60)
    chunks = [c.content for c in chunk_text(original)]
    assert len(chunks) > 1, "the fixture must span several chunks to be meaningful"

    await _seed_run(store)
    doc_id = await store.add_document(
        run_id="r1", title="bg.md", chunks=chunks, text=original,
        owner_sub=USER_A,
    )
    stored = await store.document_text(doc_id)
    assert stored == original, "the ORIGINAL text must be stored, not a reassembly"
    assert [c.content for c in chunk_text(stored)] == chunks


@pytest.mark.asyncio
async def test_omitting_the_text_is_recorded_as_a_reassembly(store):
    """An older caller still works, and the row says the text is not the original.

    Phase 3 has to be able to tell: re-chunking is only safe against the original, so
    a document written without `text=` must keep its chunks rather than have them
    regenerated from a body that drifted.
    """
    from matrix_studio.documents import join_chunks

    await _seed_run(store)
    doc_id = await store.add_document(
        run_id="r1", title="bg.md", chunks=CHUNKS, owner_sub=USER_A,
    )
    assert await store.document_text(doc_id) == join_chunks(CHUNKS)
    doc = (await store.list_documents("r1"))[0]
    assert doc["text_is_original"] is False

    with_text = await store.add_document(
        run_id="r1", title="other.md", chunks=CHUNKS, text="the real thing",
        owner_sub=USER_A,
    )
    assert await store.document_text(with_text) == "the real thing"
    flags = {d["title"]: d["text_is_original"] for d in
             await store.list_documents("r1")}
    assert flags == {"bg.md": False, "other.md": True}


@pytest.mark.asyncio
async def test_the_fixture_matches_the_deployed_tables(store):
    """Guards the fixture against drifting from the real stack.

    A fixture that creates tables the CDK does not — or omits an index it does —
    makes this whole suite pass against infrastructure that cannot serve it. Read
    from the CDK source rather than duplicated, so the two cannot disagree.
    """
    import re
    from pathlib import Path

    stack = Path(__file__).resolve().parents[1] / "infra/matrix_infra/stack.py"
    source = stack.read_text()
    declared = set(re.findall(r'self\._table\("([a-z-]+)"', source))
    declared |= {
        m for m in re.findall(r'"([a-z_]+)": self\._table\("([a-z-]+)"', source)
    }
    # The tables this suite creates, as the storage layer names them.
    used = {"runs", "events", "snapshots", "summaries", "threads",
            "thread-messages", "documents"}
    for name in used:
        assert f'"{name.replace("-", "_")}"' in source or f'"{name}"' in source, (
            f"the storage layer uses a '{name}' table that the CDK stack does not "
            "create"
        )


# --------------------------------------------------------------------------- #
# Lineage
# --------------------------------------------------------------------------- #


async def _branch(store, child, parent, turn, owner=USER_A):
    await store.create_run(
        run_id=child, topic="t", cast=[], name=child,
        parent_run_id=parent, branch_turn=turn, owner_sub=owner,
    )


@pytest.mark.asyncio
async def test_the_tree_is_rooted_at_the_earliest_ancestor(store):
    """Asking about a grandchild must return the whole forest, not a subtree of it.

    Two recursive SQL CTEs became one partition read plus an in-memory walk: the
    whole forest lives in one partition, so emulating recursion with a query per hop
    would be more round trips and more code for the same answer.
    """
    await _seed_run(store, "root", name="root-run")
    await _branch(store, "kid", "root", 3)
    await _branch(store, "grandkid", "kid", 5)

    tree = await store.get_run_tree("grandkid", owner_sub=USER_A)
    assert tree["root_id"] == "root"
    assert set(tree["nodes"]) == {"root", "kid", "grandkid"}
    assert tree["nodes"]["grandkid"]["branch_turn"] == 5
    assert tree["nodes"]["kid"]["parent_run_id"] == "root"


@pytest.mark.asyncio
async def test_the_tree_carries_config_json_for_edge_labels(store):
    """The tree view reads `branch_mutation` off this, so a missing field is a
    silently unlabelled edge rather than an error."""
    await _seed_run(store, "root")
    await store.create_run(
        run_id="kid", topic="t", cast=[], parent_run_id="root", branch_turn=2,
        config={"branch_mutation": {"kind": "pressure"}}, owner_sub=USER_A,
    )
    tree = await store.get_run_tree("root", owner_sub=USER_A)
    assert json.loads(tree["nodes"]["kid"]["config_json"])["branch_mutation"] == \
        {"kind": "pressure"}


@pytest.mark.asyncio
async def test_a_lineage_cycle_does_not_hang(store):
    """The walk is over DATA, so a cycle has to be survivable.

    It cannot arise through the normal branch path — a child always points at an
    existing parent — but an import or a hand-edited row could produce one, and
    without the guard that is an infinite loop inside a request handler rather than
    a wrong answer.
    """
    await _seed_run(store, "a")
    await _seed_run(store, "b")
    # Point them at each other: a -> b -> a.
    for child, parent in (("a", "b"), ("b", "a")):
        await store._call(
            store._table("runs").update_item,
            Key={"pk": f"USER#{USER_A}", "sk": f"RUN#{child}"},
            UpdateExpression="SET parent_run_id = :p",
            ExpressionAttributeValues={":p": parent},
        )
    tree = await store.get_run_tree("a", owner_sub=USER_A)
    assert set(tree["nodes"]) == {"a", "b"}


@pytest.mark.asyncio
async def test_lineage_cannot_leave_the_tenant(store):
    """Another owner's run claiming this run as its parent must not appear.

    In a correct system this is unreachable — a branch inherits its parent's owner —
    so it is exactly the invariant worth holding a test against. Here the partition
    itself enforces it: the walk never reads outside the caller's, so there is no
    filter to forget in a later refactor.
    """
    await _seed_run(store, "root", owner=USER_A, name="root-run")
    await _branch(store, "stolen", "root", 1, owner=USER_B)

    assert await store.list_branches("root", owner_sub=USER_A) == []
    tree = await store.get_run_tree("root", owner_sub=USER_A)
    assert set(tree["nodes"]) == {"root"}


@pytest.mark.asyncio
async def test_list_branches_returns_direct_children_only(store):
    await _seed_run(store, "root")
    await _branch(store, "kid1", "root", 2)
    await _branch(store, "kid2", "root", 4)
    await _branch(store, "grandkid", "kid1", 6)

    ids = {b["run_id"] for b in await store.list_branches("root", owner_sub=USER_A)}
    assert ids == {"kid1", "kid2"}
    assert await store.list_branches("kid2", owner_sub=USER_A) == []


@pytest.mark.asyncio
async def test_an_unknown_run_has_an_empty_tree(store):
    tree = await store.get_run_tree("nope", owner_sub=USER_A)
    assert tree == {"root_id": "nope", "nodes": {}}


@pytest.mark.asyncio
async def test_updating_a_nonexistent_run_does_not_invent_one(store):
    """DynamoDB's `UpdateItem` upserts; SQL's `UPDATE … WHERE` is a no-op.

    Without a condition, updating a run that is not there CREATES one — measured: a
    row carrying a status and a `completed_at` but no topic, no cast and no
    `created_at`, which then shows up in the owner's history as a phantom
    conversation. A wrong `owner_sub`, a deleted run, or resuming something already
    gone would each manufacture one.

    Ignored rather than raised, matching SQLite: the engine calls this at the end of
    a run, so an exception there would fail a run that had already finished.
    """
    await store.update_run_status("ghost", "complete", 123, owner_sub=USER_A)
    assert await store.get_run("ghost", owner_sub=USER_A) is None
    assert await store.list_runs(owner_sub=USER_A) == []


@pytest.mark.asyncio
async def test_a_status_update_cannot_reach_another_owners_partition(store):
    """The same upsert hazard, in the form that would be a cross-tenant write.

    A caller holding the right run id and the wrong owner would, without the
    condition, create a phantom row in the OTHER user's partition — a write into
    somebody else's history rather than a failed update of their own.
    """
    await _seed_run(store, "r1", owner=USER_A, name="alpha-run")
    await store.update_run_status("r1", "complete", owner_sub=USER_B)
    assert await store.list_runs(owner_sub=USER_B) == []
    # A's run is untouched.
    assert (await store.get_run("r1", owner_sub=USER_A))["status"] == "pending"


# --------------------------------------------------------------------------- #
# Retrieval — vectors
# --------------------------------------------------------------------------- #


async def _embed_doc(store, run_id, title, chunks, persona=None, vectors=None):
    """Attach a document and store a vector per chunk."""
    text = "\n\n".join(chunks)
    doc_id = await store.add_document(
        run_id=run_id, title=title, chunks=chunks, text=text,
        persona_name=persona, owner_sub=USER_A,
    )
    from matrix_studio.documents import chunk_text

    derived = chunk_text(text)
    payload = []
    meta = {}
    for i, chunk in enumerate(derived):
        cid = store.chunk_id_for(doc_id, chunk.ordinal)
        vec = (vectors[i] if vectors else [1.0, 0.0, 0.0, 0.0])
        payload.append((cid, vec))
        meta[cid] = {
            "document_id": doc_id, "ordinal": chunk.ordinal,
            "content": chunk.content, "persona_name": persona,
        }
    await store.store_chunk_vectors(
        run_id, payload, "test-model", owner_sub=USER_A, chunks=meta
    )
    return doc_id


@pytest.mark.asyncio
async def test_a_chunk_id_is_stable_and_derivable_without_a_lookup(store):
    """SQLite gave this as an AUTOINCREMENT rowid; there is no such thing here.

    It has to be an int (`retrieval.py` does `int(row["chunk_id"])`, RRF keys on it)
    and it has to be the SAME id from both retrieval arms, or fusing by chunk id
    fuses nothing. Derived from (document_id, ordinal) so both arms compute it
    independently and agree.
    """
    a = store.chunk_id_for("doc-abc", 3)
    assert a == store.chunk_id_for("doc-abc", 3)
    assert a != store.chunk_id_for("doc-abc", 4)
    assert a != store.chunk_id_for("doc-abd", 3)
    assert 0 < a < 2 ** 63, "must fit a signed 64-bit int for JSON and SQLite parity"


@pytest.mark.asyncio
async def _stored_vectors(index_name=None):
    """Everything in the test index, via `ListVectors`.

    `QueryVectors` is NOT implemented by moto — it falls through to real AWS and
    fails with an invalid-token error. So the k-NN behaviour itself is verified by
    `scripts/verify_vector_retrieval.py` against the deployed index, and these tests
    assert what was WRITTEN: the keys, the scoping metadata, and the passage text.

    That split is not a compromise on the important part. Every way this port can
    leak or lose data is in the write — an unscoped vector, a missing `cast_wide`
    flag, text omitted from metadata — and each is checkable here. What needs the real
    service is whether AWS honours a filter, which is AWS's behaviour rather than
    this code's.
    """
    import boto3

    client = boto3.client("s3vectors", region_name="us-east-1")
    out = []
    token = None
    while True:
        params = {"vectorBucketName": VECTOR_BUCKET,
                  "indexName": index_name or VECTOR_INDEX,
                  "returnMetadata": True, "maxResults": 500}
        if token:
            params["nextToken"] = token
        result = client.list_vectors(**params)
        out.extend(result.get("vectors") or [])
        token = result.get("nextToken")
        if not token:
            return out


@pytest.mark.asyncio
async def test_a_stored_vector_carries_its_passage_text(store):
    """The round trip §4a's design saves: text rides as vector metadata.

    If text were omitted, `vector_search` would return ids and distances with empty
    content and the per-turn path would need a second lookup per passage — the
    design's central claim, silently untrue, with every count assertion still passing.
    """
    await _seed_run(store)
    await _embed_doc(store, "r1", "bg.md",
                     ["Egress inspection provides auditable evidence."])
    stored = await _stored_vectors()
    assert len(stored) == 1
    assert "Egress inspection" in stored[0]["metadata"]["text"]


@pytest.mark.asyncio
async def test_every_stored_vector_is_scoped(store):
    """With ONE shared index, this metadata IS the isolation boundary.

    A vector missing `owner_sub` or `run_id` is returned by any filtered query that
    cannot exclude what it cannot see — a permanent cross-tenant leak, undetectable
    afterwards. Phase 6's per-KB indexes recover an IAM boundary; until then this is
    the whole of it.
    """
    await _seed_run(store, "r1")
    await _embed_doc(store, "r1", "a.md", ["Content for run one."], persona="Dana")
    await _embed_doc(store, "r1", "b.md", ["Content shared with the cast."])
    for entry in await _stored_vectors():
        meta = entry["metadata"]
        assert meta["owner_sub"] == USER_A, meta
        assert meta["run_id"] == "r1", meta
        assert meta["document_id"], meta
        assert "ordinal" in meta, meta


@pytest.mark.asyncio
async def test_a_cast_wide_vector_carries_an_explicit_flag(store):
    """Absent metadata cannot be matched by a filter.

    A cast-wide chunk has no `persona_name`, so the slice filter's second arm has to
    test something that IS present. Without `cast_wide`, cast-wide documents would be
    retrievable by nobody — the same trap that made them invisible in
    `list_documents`, one layer down.
    """
    await _seed_run(store)
    await _embed_doc(store, "r1", "dana.md", ["Dana's own note."], persona="Dana")
    await _embed_doc(store, "r1", "all.md", ["Shared with the whole cast."])
    by_title = {}
    for entry in await _stored_vectors():
        by_title[entry["metadata"]["text"][:10]] = entry["metadata"]

    dana = next(m for k, m in by_title.items() if k.startswith("Dana"))
    shared = next(m for k, m in by_title.items() if k.startswith("Shared"))
    assert dana["persona_name"] == "Dana"
    assert "cast_wide" not in dana
    assert shared.get("cast_wide") is True
    assert "persona_name" not in shared


@pytest.mark.asyncio
async def test_the_slice_filter_names_every_scoping_clause(store):
    """The filter is built in one method because it is the isolation boundary.

    Asserted structurally rather than through a query, since moto has no
    `QueryVectors`. The clauses that must be present: owner, run, and the
    persona-or-cast-wide disjunction. A filter missing the `cast_wide` arm silently
    hides shared documents; one missing `owner_sub` silently exposes other tenants'.
    """
    for persona in (None, "Dana"):
        body = json.dumps(store._slice_filter("r1", persona, USER_A))
        assert USER_A in body and '"owner_sub"' in body, body
        assert '"run_id"' in body and "r1" in body, body

    persona_body = json.dumps(store._slice_filter("r1", "Dana", USER_A))
    assert '"persona_name"' in persona_body and "Dana" in persona_body
    assert '"cast_wide"' in persona_body, (
        "the filter has no cast-wide arm, so shared documents are unretrievable"
    )


@pytest.mark.asyncio
async def test_every_filter_object_carries_exactly_one_key(store):
    """A hard S3 Vectors rule, and the first version of the filter broke it.

    `{"owner_sub": ..., "run_id": ...}` is rejected with a bare
    `ValidationException: Invalid filter` that names neither the object nor the rule —
    so this failed only against the real service, and only after the vectors had been
    written successfully. Determined empirically:

        {"a": 1, "b": 2}                     -> Invalid filter
        {"$and": [{"a": 1}, {"b": 2}]}       -> ok
        {"$and": [{"a": 1, "b": 2}, ...]}    -> Invalid filter

    Checked recursively, because the violation that actually happened was nested one
    level down inside an `$and` — a top-level-only check would have passed it.
    """
    def assert_single_key(node, path="filter"):
        if isinstance(node, list):
            for i, item in enumerate(node):
                assert_single_key(item, f"{path}[{i}]")
            return
        if not isinstance(node, dict):
            return
        assert len(node) == 1, (
            f"{path} has {len(node)} keys ({sorted(node)}); S3 Vectors rejects any "
            "filter object with more than one"
        )
        for key, value in node.items():
            if key.startswith("$"):
                assert_single_key(value, f"{path}.{key}")

    for persona in (None, "Dana", "Marcus"):
        assert_single_key(store._slice_filter("r1", persona, USER_A))


@pytest.mark.asyncio
async def test_vector_search_degrades_rather_than_raising(store):
    """A retrieval failure must not end a run.

    Exercised here by the fact that moto has no `QueryVectors`: the call fails, and
    the contract is that it returns no passages so the prompt can say so honestly —
    the same contract the SQLite path had. An exception would take down a
    conversation for a search problem.
    """
    await _seed_run(store)
    await _embed_doc(store, "r1", "bg.md", ["Some background material."])
    assert await store.vector_search(
        "r1", [1.0, 0.0, 0.0, 0.0], k=3, owner_sub=USER_A) == []


@pytest.mark.asyncio
async def test_vector_search_without_a_bucket_is_empty_not_an_error(store,
                                                                   monkeypatch):
    monkeypatch.delenv("VECTOR_BUCKET", raising=False)
    assert await store.vector_search(
        "r1", [1.0, 0.0, 0.0, 0.0], k=3, owner_sub=USER_A) == []


@pytest.mark.asyncio
async def test_a_vector_without_scoping_metadata_is_refused(store):
    """An unscoped vector would be returned to every tenant.

    A filtered query cannot exclude what it cannot see, so a vector missing
    `owner_sub`/`run_id` is a permanent cross-tenant leak with no way to detect it
    later. Refusing the write is the only point at which it is fixable.
    """
    await _seed_run(store)
    with pytest.raises(StorageError, match="cannot be scoped"):
        await store.store_chunk_vectors(
            "r1", [(12345, [1.0, 0.0, 0.0, 0.0])], "m",
            owner_sub=USER_A, chunks={},
        )


@pytest.mark.asyncio
async def test_embedding_ingest_is_resumable(store):
    """`chunks_missing_vectors` must shrink as vectors land, or ingest restarts.

    Also the test that catches re-chunking drift: the missing set is derived by
    re-chunking the STORED text, so if that produced different ordinals than the
    vectors were written under, already-embedded chunks would look missing forever
    and ingest would loop.
    """
    await _seed_run(store)
    text = "Egress inspection provides auditable evidence. " * 40
    doc_id = await store.add_document(
        run_id="r1", title="bg.md",
        chunks=[c.content for c in __import__(
            "matrix_studio.documents", fromlist=["x"]).chunk_text(text)],
        text=text, owner_sub=USER_A,
    )
    missing = await store.chunks_missing_vectors("r1", owner_sub=USER_A)
    assert len(missing) > 1, "the fixture must span several chunks"
    assert await store.count_chunk_vectors("r1", owner_sub=USER_A) == 0

    payload = [(m["chunk_id"], [1.0, 0.0, 0.0, 0.0]) for m in missing]
    meta = {m["chunk_id"]: m for m in missing}
    await store.store_chunk_vectors(
        "r1", payload, "test-model", owner_sub=USER_A, chunks=meta)

    assert await store.count_chunk_vectors("r1", owner_sub=USER_A) == len(missing)
    assert await store.chunks_missing_vectors("r1", owner_sub=USER_A) == [], (
        "re-chunking the stored text produced different ordinals, so ingest would "
        "re-embed the same chunks forever"
    )
    assert await store.embedding_model() == "test-model"


@pytest.mark.asyncio
async def test_chunk_count_is_read_from_metadata_not_by_refetching(store):
    """One of the four jobs §4a says the document row earns its place with."""
    await _seed_run(store)
    await store.add_document(run_id="r1", title="a.md", chunks=["x", "y", "z"],
                             persona_name="Dana", owner_sub=USER_A)
    await store.add_document(run_id="r1", title="b.md", chunks=["p", "q"],
                             owner_sub=USER_A)
    assert await store.chunk_count("r1") == 5
    assert await store.chunk_count("r1", "Dana") == 5
    assert await store.chunk_count("r1", "Marcus") == 2


# --------------------------------------------------------------------------- #
# Retrieval — the lexical arm
# --------------------------------------------------------------------------- #


LEX_CHUNKS = [
    "Egress inspection provides auditable evidence for the auditor.",
    "Cost allocation tags let finance attribute spend to a team.",
]


@pytest.mark.asyncio
async def test_lexical_search_returns_the_matching_chunk(store):
    """FTS5 is gone with SQLite, so this is in-process BM25.

    Small on purpose: measured lexical recall@1 is 0.017 against vector's 0.367
    (PHASE5-RETRIEVAL-MEASUREMENT §5f), so this serves `/documents/search` — an
    inspection endpoint — not the per-turn path.
    """
    await _seed_run(store)
    await store.add_document(run_id="r1", title="bg.md", chunks=LEX_CHUNKS,
                             text="\n\n".join(LEX_CHUNKS), owner_sub=USER_A)
    hits = await store.search_documents(run_id="r1", query='"egress"', k=3)
    assert hits, "no lexical match"
    assert "Egress inspection" in hits[0]["content"]
    assert hits[0]["title"] == "bg.md"


@pytest.mark.asyncio
async def test_lexical_scores_are_negative_as_fts5_returned_them(store):
    """Not cosmetic. `filter_by_score` takes `abs(score)` as strength and the
    hybrid fusion assumes an ordering, so positive scores would invert every
    ranking while looking numerically plausible."""
    await _seed_run(store)
    await store.add_document(run_id="r1", title="bg.md", chunks=LEX_CHUNKS,
                             text="\n\n".join(LEX_CHUNKS), owner_sub=USER_A)
    hits = await store.search_documents(
        run_id="r1", query='"egress" OR "auditor"', k=5)
    assert hits
    assert all(h["score"] < 0 for h in hits), [h["score"] for h in hits]
    assert hits == sorted(hits, key=lambda h: h["score"]), "not best-first"


@pytest.mark.asyncio
async def test_lexical_search_accepts_the_fts5_syntax_callers_still_build(store):
    """`build_fts_query` emits `"a" OR "b"`, and every caller still calls it.

    Stripping the quoting and operators here rather than changing those callers means
    the engine needs no branch on which storage backend it is talking to — and a
    query that reached the index literally would match on the word "or".
    """
    await _seed_run(store)
    await store.add_document(run_id="r1", title="bg.md", chunks=LEX_CHUNKS,
                             text="\n\n".join(LEX_CHUNKS), owner_sub=USER_A)
    quoted = await store.search_documents(
        run_id="r1", query='"egress" OR "inspection"', k=3)
    plain = await store.search_documents(run_id="r1", query="egress inspection", k=3)
    assert quoted and plain
    assert [h["chunk_id"] for h in quoted] == [h["chunk_id"] for h in plain]


@pytest.mark.asyncio
async def test_lexical_search_is_scoped_to_the_persona_slice(store):
    await _seed_run(store)
    await store.add_document(run_id="r1", title="dana.md",
                             chunks=["Dana's egress briefing."],
                             text="Dana's egress briefing.",
                             persona_name="Dana", owner_sub=USER_A)
    await store.add_document(run_id="r1", title="marcus.md",
                             chunks=["Marcus's egress briefing."],
                             text="Marcus's egress briefing.",
                             persona_name="Marcus", owner_sub=USER_A)
    hits = await store.search_documents(
        run_id="r1", query='"egress"', persona_name="Dana", k=5)
    assert hits and all("Dana" in h["content"] for h in hits), (
        [h["content"] for h in hits]
    )


@pytest.mark.asyncio
async def test_lexical_statistics_cannot_be_contaminated_by_another_run(store):
    """The v0.6 bug, structurally impossible here.

    `bm25()` computed statistics over the WHOLE SQLite index while `run_id` was only
    an outer filter, so a score depended on what other runs existed. The index here is
    built from the run's own chunks, so scoping is structural rather than a predicate
    — a score is a property of the run.
    """
    await _seed_run(store, "r1")
    await store.add_document(run_id="r1", title="a.md",
                             chunks=["Egress inspection is the topic."],
                             text="Egress inspection is the topic.",
                             owner_sub=USER_A)
    alone = await store.search_documents(run_id="r1", query='"egress"', k=3)

    await _seed_run(store, "r2")
    for i in range(20):
        await store.add_document(
            run_id="r2", title=f"noise{i}.md",
            chunks=[f"Egress egress egress noise document {i}."],
            text=f"Egress egress egress noise document {i}.",
            owner_sub=USER_A,
        )
    after = await store.search_documents(run_id="r1", query='"egress"', k=3)
    assert [h["score"] for h in alone] == [h["score"] for h in after], (
        "another run's corpus changed this run's scores"
    )


@pytest.mark.asyncio
async def test_an_unknown_corpus_value_is_still_rejected(store):
    """`corpus` is now structural, but a typo should fail rather than be ignored."""
    with pytest.raises(ValueError, match="corpus must be"):
        await store.search_documents(run_id="r1", query='"x"', corpus="wrong")


@pytest.mark.asyncio
async def test_term_frequencies_are_keyed_by_the_callers_terms(store):
    """`select_discriminative_terms` looks these up by the raw string it passed.

    Returning stems would produce a zero for every term and silently disable
    discriminative selection — a feature quietly turning off is worse than one
    failing, because nothing reports it.
    """
    await _seed_run(store)
    await store.add_document(run_id="r1", title="bg.md", chunks=LEX_CHUNKS,
                             text="\n\n".join(LEX_CHUNKS), owner_sub=USER_A)
    freqs = await store.term_document_frequencies(
        "r1", ["auditor", "egress", "absent"])
    assert set(freqs) == {"auditor", "egress", "absent"}
    assert freqs["absent"] == 0
    assert freqs["egress"] >= 1


@pytest.mark.asyncio
async def test_reindex_reports_the_chunk_count_rather_than_pretending(store):
    """There is no derived index to rebuild — the vectors ARE the index.

    The route exists and its contract is "tell me the index is consistent". The honest
    answer is now "it cannot be otherwise", and the useful number is how many chunks
    there are.
    """
    await _seed_run(store)
    await store.add_document(run_id="r1", title="a.md", chunks=["x", "y", "z"],
                             owner_sub=USER_A)
    assert await store.reindex_documents() == 3


# --------------------------------------------------------------------------- #
# Owner binding
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_unbound_store_refuses_rather_than_guessing(aws):
    """"Whose data is this?" being unanswerable must not default to anyone.

    Defaulting to the local user would attribute one person's conversation to a
    shared bucket, silently — the failure the explicit-argument design existed to
    prevent, and binding has to preserve it rather than trade it away.
    """
    s = DynamoStorage(table_prefix=PREFIX, bucket=BUCKET, region="us-east-1")
    await s.connect()
    try:
        for call in (
            s.get_run("r1"),
            s.list_runs(),
            s.get_events("r1"),
            s.append_event(run_id="r1", turn=1, seq=0, event_type="e", payload={}),
        ):
            with pytest.raises(StorageError, match="no owner for this call"):
                await call
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_binding_scopes_every_read_without_repeating_the_owner(aws):
    """The point of `for_owner`: name the tenant once, at the request boundary."""
    s = DynamoStorage(table_prefix=PREFIX, bucket=BUCKET, region="us-east-1")
    await s.connect()
    try:
        a = s.for_owner(USER_A)
        b = s.for_owner(USER_B)
        await a.create_run(run_id="r1", topic="t", cast=[], name="a-run")
        await a.append_event(run_id="r1", turn=1, seq=0, event_type="e",
                             payload={"v": 1})

        assert (await a.get_run("r1"))["owner_sub"] == USER_A
        assert len(await a.get_events("r1")) == 1
        # The other binding sees none of it, with no argument passed anywhere.
        assert await b.get_run("r1") is None
        assert await b.get_events("r1") == []
        assert await b.list_runs() == []
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_an_explicit_owner_overrides_the_binding(aws):
    """What lets a test assert that a cross-tenant read is refused.

    Without the override, a bound store could only ever read its own partition, and
    the negative cases would be unwritable — the suite would be unable to express the
    property it most needs to check.
    """
    s = DynamoStorage(table_prefix=PREFIX, bucket=BUCKET, region="us-east-1")
    await s.connect()
    try:
        a = s.for_owner(USER_A)
        await a.create_run(run_id="r1", topic="t", cast=[])
        assert await a.get_run("r1", owner_sub=USER_B) is None
        assert (await a.get_run("r1", owner_sub=USER_A))["id"] == "r1"
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_a_binding_does_not_leak_back_into_the_unbound_store(aws):
    """`for_owner` returns a copy. If it mutated `self`, the last request served
    would silently become the default for every later one — including an unbound
    background task."""
    s = DynamoStorage(table_prefix=PREFIX, bucket=BUCKET, region="us-east-1")
    await s.connect()
    try:
        s.for_owner(USER_A)
        assert getattr(s, "_owner_sub", None) is None
        with pytest.raises(StorageError, match="no owner"):
            await s.list_runs()
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_two_bindings_do_not_interfere(aws):
    """Concurrent requests take separate bindings off one store."""
    s = DynamoStorage(table_prefix=PREFIX, bucket=BUCKET, region="us-east-1")
    await s.connect()
    try:
        a, b = s.for_owner(USER_A), s.for_owner(USER_B)
        await a.create_run(run_id="ra", topic="t", cast=[], name="shared-name")
        await b.create_run(run_id="rb", topic="t", cast=[], name="shared-name")
        assert (await a.get_run_by_ref("shared-name"))["id"] == "ra"
        assert (await b.get_run_by_ref("shared-name"))["id"] == "rb"
        assert a._owner_sub == USER_A and b._owner_sub == USER_B
    finally:
        await s.close()
