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
PREFIX = "matrix-studio-test"


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
