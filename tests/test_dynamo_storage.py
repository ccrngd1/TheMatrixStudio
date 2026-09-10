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
        for table in ("runs", "events", "snapshots", "summaries", "threads",
                      "thread-messages", "documents"):
            ddb.create_table(
                TableName=f"{PREFIX}-{table}",
                KeySchema=[
                    {"AttributeName": "pk", "KeyType": "HASH"},
                    {"AttributeName": "sk", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "pk", "AttributeType": "S"},
                    {"AttributeName": "sk", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
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
    """Callers index `run["description"]` and expect None, as SQLite gave them.

    `_to_ddb` drops None rather than storing DynamoDB's NULL type, so the attribute
    is absent — and an absent attribute is a KeyError, not a None, unless the read
    path fills it in.
    """
    await _seed_run(store)
    run = await store.get_run("r1", owner_sub=USER_A)
    for field in ("name", "description", "slug", "config_json",
                  "parent_run_id", "branch_turn", "completed_at"):
        assert run.get(field) is None, field


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


