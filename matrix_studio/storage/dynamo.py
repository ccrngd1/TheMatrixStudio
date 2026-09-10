# SPDX-License-Identifier: Apache-2.0
"""
DynamoDB + S3 storage. The Phase 2 replacement for the SQLite layer.

Read `docs/PHASE2-STORAGE-KEY-DESIGN.md` before changing anything here — it records
the six places where the original 53 methods relied on something SQLite makes free,
and why each is solved the way it is. Two of those are silent when wrong (sort-key
ordering, and a denormalised field drifting), so they will not announce themselves.

**Why `async def` over a synchronous SDK.** Every method here keeps the signature the
SQLite layer had, so the 788 existing tests remain the contract. boto3 is synchronous,
so each call goes through `asyncio.to_thread`: the event loop stays free, there is no
second AWS client library, and `moto` intercepts it (verified, including concurrent
writes and sort-key range reads). Blocking calls inline would be *almost* fine on
Lambda — one request per sandbox — and wrong for the local server, which is exactly
the kind of difference that shows up as a mystery under load.

**Key shapes**, all built by the helpers below rather than formatted inline, because a
sort key assembled in two places is a sort key that will eventually disagree:

    runs         USER#{sub}      RUN#{run_id}
    name markers USER#{sub}      NAME#{name}          (uniqueness, see _put_run)
    events       USER#{sub}      RUN#{run_id}#{seq:012d}
    snapshots    USER#{sub}      RUN#{run_id}#{turn:06d}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from matrix_studio.state import SimSnapshot
from matrix_studio.tenancy import LOCAL_USER_SUB

logger = logging.getLogger(__name__)

# Width of the numeric components of a sort key. Deliberately absurd relative to
# real values — measured maxima are 80 events and tens of turns — because the cost
# of a too-wide key is a few bytes and the cost of a too-narrow one is that ordering
# silently breaks at a threshold nobody is watching, having worked for a year.
SEQ_WIDTH = 12
TURN_WIDTH = 6


class StorageError(RuntimeError):
    """A storage operation failed in a way the caller has to handle.

    Deliberately not a subclass of anything AWS-specific: callers should not have to
    know whether the backend raised `ConditionalCheckFailedException` or
    `TransactionCanceledException`, only that the write was refused.
    """


class DuplicateNameError(StorageError):
    """This owner already has a run with that name.

    A distinct type because the SQLite layer raised `aiosqlite.IntegrityError` here
    and callers (`manager.create_run`, `branching.create_branch_run`) rely on
    detecting the collision to disambiguate rather than fail.
    """


# The full attribute set of each entity, so a read can restore the keys that were
# dropped on write. `_to_ddb` omits None rather than storing DynamoDB's NULL type,
# which means a nullable field comes back ABSENT — and absent is a `KeyError` where
# SQLite gave `None`.
#
# That difference is not theoretical. Five call sites subscript exactly these fields:
#   app.py       d["media_type"], d["persona_name"] is None   (the dossier)
#   app.py       row["agent_name"]                            (retrieved passages)
#   service.py   thread["persona_name"], reply["speaker"]     (aside threads)
# A cast-wide document has no `persona_name`, and `sim.started` has no `agent_name`,
# so those are the ordinary cases rather than edge ones — the dossier would 500 on
# any run with a cast-wide document.
#
# Fixed here rather than at the call sites, because the contract is "this layer
# returns what the SQLite layer returned". Patching five callers would leave the
# sixth, written later, to rediscover it.
_RUN_FIELDS = (
    "id", "owner_sub", "topic", "cast_json", "status", "created_at",
    "name", "description", "slug", "config_json", "parent_run_id",
    "branch_turn", "completed_at",
)
_EVENT_FIELDS = (
    "run_id", "turn", "seq", "event_type", "agent_name", "payload", "created_at",
)
_DOCUMENT_FIELDS = (
    "id", "document_id", "run_id", "persona_name", "title", "source_path",
    "media_type", "char_count", "chunk_count", "s3_key", "owner_sub",
    "text_is_original", "created_at",
)
_THREAD_FIELDS = (
    "id", "thread_id", "run_id", "target", "persona_name", "mode", "created_at",
)
_THREAD_MESSAGE_FIELDS = (
    "id", "thread_id", "role", "speaker", "content", "tokens_in", "tokens_out",
    "cost_usd", "created_at",
)


def _row(item: Dict[str, Any], fields: tuple) -> Dict[str, Any]:
    """Convert a read item and restore every declared field, absent ones as None."""
    out = _from_ddb(item)
    for field in fields:
        out.setdefault(field, None)
    return out


def _user_pk(owner_sub: str) -> str:
    return f"USER#{owner_sub}"


def _run_sk(run_id: str) -> str:
    return f"RUN#{run_id}"


def _name_sk(name: str) -> str:
    return f"NAME#{name}"


def _event_sk(run_id: str, seq: int) -> str:
    return f"RUN#{run_id}#{seq:0{SEQ_WIDTH}d}"


def _snapshot_sk(run_id: str, turn: int) -> str:
    return f"RUN#{run_id}#{turn:0{TURN_WIDTH}d}"


def _run_pk(run_id: str) -> str:
    """Partition key for run-scoped-but-not-user-scoped data.

    Summaries, threads and documents are keyed by run rather than by user, and that
    is §4's decision rather than an oversight: a shared knowledge base is read by
    principals who do not own it, so a `USER#{sub}` prefix would make sharing
    inexpressible. Authorisation for these is the caller's explicit ownership check
    on the run (and, from Phase 6, the `kb_grants` check re-run at query time).
    """
    return f"RUN#{run_id}"


def _thread_pk(thread_id: str) -> str:
    return f"THREAD#{thread_id}"


def _thread_sk(thread_id: str) -> str:
    return f"THREAD#{thread_id}"


def _document_sk(document_id: str) -> str:
    return f"DOC#{document_id}"


def _run_prefix(run_id: str) -> str:
    """Sort-key prefix matching every event or snapshot of one run.

    The trailing `#` matters: without it, run `abc` would also match run `abcdef`,
    so one run's history would include another's. Run ids are UUIDs so a real
    collision is unlikely — but `copy_events_upto` writes to a *caller-supplied*
    destination id, and an imported or test-fixture id like `r1` next to `r10` makes
    it reachable.
    """
    return f"RUN#{run_id}#"


class DynamoStorage:
    """Event-sourced storage over DynamoDB (metadata) and S3 (large bodies)."""

    def __init__(
        self,
        *,
        table_prefix: Optional[str] = None,
        bucket: Optional[str] = None,
        region: Optional[str] = None,
    ) -> None:
        self.table_prefix = table_prefix or os.environ.get(
            "TABLE_PREFIX", "matrix-studio"
        )
        self.bucket = bucket or os.environ.get("DATA_BUCKET", "")
        self.region = region or os.environ.get("AWS_REGION") or "us-east-1"
        self._ddb = None
        self._s3 = None
        # The low-level client, needed for `transact_write_items`, which has no
        # resource-level equivalent. Kept separate and named for what it is: the two
        # levels take DIFFERENT item formats, and mixing them is a silent-looking
        # ParamValidationError at the first write.
        self._client = None
        self._tables: Dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        """Create the clients. No network call, so this cannot fail on a bad name.

        Named `connect` to match the SQLite layer's lifecycle, but there is no
        connection: DynamoDB is request-per-call. The consequence worth knowing is
        that a wrong table name surfaces as a `ResourceNotFoundException` on first
        use rather than here — so the startup log below reports what it will TRY to
        use, which is the fact an operator needs when the first request fails.
        """
        import boto3

        self._ddb = boto3.resource("dynamodb", region_name=self.region)
        self._client = boto3.client("dynamodb", region_name=self.region)
        self._s3 = boto3.client("s3", region_name=self.region)
        logger.info(
            "Storage: DynamoDB tables '%s-*' in %s, bodies in s3://%s",
            self.table_prefix,
            self.region,
            self.bucket or "<UNSET — DATA_BUCKET is empty, snapshot writes will fail>",
        )

    async def close(self) -> None:
        """Drop the clients. Nothing to flush — every write is already durable."""
        self._ddb = None
        self._client = None
        self._s3 = None
        self._tables.clear()

    def _table(self, name: str):
        if name not in self._tables:
            if self._ddb is None:
                raise StorageError("connect() has not been called")
            self._tables[name] = self._ddb.Table(f"{self.table_prefix}-{name}")
        return self._tables[name]

    @staticmethod
    async def _call(fn: Callable, **kwargs) -> Any:
        """Run one boto3 call off the event loop.

        Every AWS call in this module goes through here, so there is one place to add
        retry, timing, or tracing — and one place to look when something blocks.
        """
        return await asyncio.to_thread(lambda: fn(**kwargs))

    # ------------------------------------------------------------------ #
    # Counters — the AUTOINCREMENT replacement (key design §4)
    # ------------------------------------------------------------------ #

    async def _next_id(self, table: str, pk: str) -> int:
        """Atomically allocate the next integer id within a partition.

        `ADD` is atomic server-side and returns the post-increment value, so this is
        a genuine autoincrement with no read-modify-write and no lost updates under
        concurrency. That matters for thread messages specifically: they are ordered
        BY this id, and a timestamp would have one-second resolution — two fast
        replies would order arbitrarily, which is precisely when it happens.

        Not used for events. The engine already assigns a monotonic per-run `seq`,
        and putting a counter on the hottest write path would double its cost to
        produce a number the caller is holding.
        """
        result = await self._call(
            self._table(table).update_item,
            Key={"pk": pk, "sk": "COUNTER"},
            UpdateExpression="ADD next_id :one",
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return int(result["Attributes"]["next_id"])

    # ------------------------------------------------------------------ #
    # S3 bodies
    # ------------------------------------------------------------------ #

    def _snapshot_key(self, owner_sub: str, run_id: str, turn: int) -> str:
        """Per-user prefix, so the scoped role's `…/{sub}/*` ARN condition applies."""
        return f"snapshots/{owner_sub}/{run_id}/{turn:0{TURN_WIDTH}d}.json"

    async def _put_body(self, key: str, body: str) -> None:
        if not self.bucket:
            raise StorageError(
                "DATA_BUCKET is not set, so there is nowhere to write the snapshot "
                "body. Refusing rather than writing a pointer to an object that "
                "does not exist — a dangling pointer looks like a saved snapshot."
            )
        await self._call(
            self._s3.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="application/json",
        )

    async def _put_text(self, key: str, text: str) -> None:
        """Write a document's normalised text. Same guard as a snapshot body."""
        if not self.bucket:
            raise StorageError(
                "DATA_BUCKET is not set, so there is nowhere to write the document "
                "text. Refusing rather than writing metadata for a document whose "
                "content does not exist — that would list and fail to open."
            )
        await self._call(
            self._s3.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=text.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )

    async def _get_text(self, key: str) -> Optional[str]:
        return await self._get_body(key)

    async def _get_body(self, key: str) -> Optional[str]:
        """Fetch a body, or None if the object is gone.

        None rather than raising, because a missing body with a live pointer is a
        recoverable inconsistency for a *reader* — the caller falls back to replaying
        the event log, which is the source of truth. It is logged loudly because it
        should never happen.
        """
        try:
            obj = await self._call(self._s3.get_object, Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - botocore raises many shapes
            logger.warning("Snapshot body missing at s3://%s/%s: %s",
                           self.bucket, key, exc)
            return None
        return await asyncio.to_thread(lambda: obj["Body"].read().decode("utf-8"))

    # ------------------------------------------------------------------ #
    # Runs
    # ------------------------------------------------------------------ #

    async def create_run(
        self,
        run_id: str,
        topic: str,
        cast: List[Dict[str, Any]],
        name: Optional[str] = None,
        description: Optional[str] = None,
        slug: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        parent_run_id: Optional[str] = None,
        branch_turn: Optional[int] = None,
        owner_sub: str = LOCAL_USER_SUB,
    ) -> None:
        """Create a run, refusing a name this owner already used.

        Per-user name uniqueness was a partial unique index in SQLite. DynamoDB has
        no unique constraint other than the primary key, so it is expressed as a
        **marker item** `USER#{sub}` / `NAME#{name}` written in the same transaction
        as the run, conditional on not already existing.

        A transaction rather than two writes: if the marker succeeded and the run
        failed, the name would be permanently unavailable to its owner with no run to
        show for it — and nothing would report why.
        """
        now = int(time.time())
        item = {
            "pk": _user_pk(owner_sub),
            "sk": _run_sk(run_id),
            "id": run_id,
            "owner_sub": owner_sub,
            "topic": topic,
            "cast_json": json.dumps(cast),
            "status": "pending",
            "created_at": now,
            # Written even when None so the returned dict has the same keys the
            # SQLite row did — callers index these directly and a missing key is an
            # AttributeError where SQLite gave None.
            "name": name,
            "description": description,
            "slug": slug or name,
            "config_json": json.dumps(config) if config else None,
            "parent_run_id": parent_run_id,
            "branch_turn": branch_turn,
            "completed_at": None,
        }

        writes: List[Dict[str, Any]] = [
            {
                "Put": {
                    "TableName": f"{self.table_prefix}-runs",
                    "Item": _to_wire(item),
                    "ConditionExpression": "attribute_not_exists(pk) "
                    "AND attribute_not_exists(sk)",
                }
            }
        ]
        if name:
            writes.append(
                {
                    "Put": {
                        "TableName": f"{self.table_prefix}-runs",
                        "Item": _to_wire(
                            {
                                "pk": _user_pk(owner_sub),
                                "sk": _name_sk(name),
                                "run_id": run_id,
                            }
                        ),
                        "ConditionExpression": "attribute_not_exists(sk)",
                    }
                }
            )

        try:
            await self._call(self._client.transact_write_items, TransactItems=writes)
        except Exception as exc:  # noqa: BLE001
            if "ConditionalCheckFailed" in str(exc) or "TransactionCanceled" in str(exc):
                raise DuplicateNameError(
                    f"run id {run_id!r} or name {name!r} already exists for this owner"
                ) from exc
            raise

    async def name_exists(self, name: str, *, owner_sub: str) -> bool:
        """Whether THIS OWNER already has a run with this name.

        Reads the marker item, not the runs — so it is a `GetItem` rather than a
        query, and it stays correct even for a run whose row was deleted while its
        name marker remained.
        """
        got = await self._call(
            self._table("runs").get_item,
            Key={"pk": _user_pk(owner_sub), "sk": _name_sk(name)},
        )
        return "Item" in got

    async def update_run_status(
        self,
        run_id: str,
        status: str,
        completed_at: Optional[int] = None,
        *,
        owner_sub: str,
    ) -> None:
        """Set a run's lifecycle status, and its completion time when terminal.

        **Conditional on the run existing, and that is not a nicety.** DynamoDB's
        `UpdateItem` UPSERTS, where SQL's `UPDATE … WHERE id = ?` is a silent no-op on
        a missing row. Without the condition, updating a run that is not there
        *creates* one — measured: a row with a status, a `completed_at`, and no topic,
        no cast and no `created_at`, which then appears in the caller's history list.
        A wrong `owner_sub`, a deleted run, or a resume of something already gone
        would each manufacture a phantom conversation.

        A missing run is logged and ignored rather than raised, which matches what
        SQLite did and what callers assume: the engine calls this at the end of a run,
        and turning "the row is gone" into an exception there would fail a run that
        had already finished. The warning is what makes a real bug findable, since
        silence is how this class of thing survives.
        """
        expr = "SET #s = :s"
        values: Dict[str, Any] = {":s": status}
        if completed_at is not None:
            expr += ", completed_at = :c"
            values[":c"] = completed_at
        try:
            await self._call(
                self._table("runs").update_item,
                Key={"pk": _user_pk(owner_sub), "sk": _run_sk(run_id)},
                UpdateExpression=expr,
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(sk)",
            )
        except Exception as exc:  # noqa: BLE001
            if "ConditionalCheckFailed" not in str(exc):
                raise
            logger.warning(
                "Ignored a status update to %r for run %s, which does not exist "
                "under owner %s. Nothing was written — but a caller reaching here "
                "has a run id or an owner that does not match its data.",
                status, run_id, owner_sub,
            )

    async def get_run(
        self, run_id: str, *, owner_sub: str
    ) -> Optional[Dict[str, Any]]:
        """One run by id.

        `owner_sub` is required, unlike the SQLite version which took an id alone and
        was documented "internal, post-authorisation use only". Under a
        user-partitioned table there is no id-only read to offer, which turns that
        comment into something the type system enforces.
        """
        got = await self._call(
            self._table("runs").get_item,
            Key={"pk": _user_pk(owner_sub), "sk": _run_sk(run_id)},
        )
        item = got.get("Item")
        return _row(item, _RUN_FIELDS) if item else None

    async def get_run_by_ref(
        self, ref: str, *, owner_sub: str
    ) -> Optional[Dict[str, Any]]:
        """Resolve one of this owner's runs by id OR memorable name.

        Two reads at worst, and the id is tried first because that is what the SPA
        sends once a run is open. A name resolves through its marker item, so this
        needs no index and no scan.

        Returns None for "not found" *and* "not yours" — they are indistinguishable
        on purpose, and here that is free rather than deliberate: a ref outside the
        caller's partition simply is not there.
        """
        found = await self.get_run(ref, owner_sub=owner_sub)
        if found:
            return found
        marker = await self._call(
            self._table("runs").get_item,
            Key={"pk": _user_pk(owner_sub), "sk": _name_sk(ref)},
        )
        item = marker.get("Item")
        if not item:
            return None
        return await self.get_run(str(item["run_id"]), owner_sub=owner_sub)

    async def list_runs(
        self, q: Optional[str] = None, limit: int = 200, *, owner_sub: str
    ) -> List[Dict[str, Any]]:
        """This owner's runs, newest first, optionally substring-filtered.

        The filter is applied in Python rather than as a DynamoDB `FilterExpression`.
        Both read the same items — a filter expression is applied *after* the read
        and is billed the same — so the only difference is that `contains()` on three
        attributes with case-insensitivity is far clearer here, and cannot be got
        subtly wrong in the way the SQL version was (`owner = ? AND a OR b OR c`
        parses as `(owner AND a) OR b OR c`, which leaked across tenants until it was
        parenthesised).
        """
        items = await self._query_all(
            "runs",
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": _user_pk(owner_sub),
                ":prefix": "RUN#",
            },
        )
        runs = [_row(i, _RUN_FIELDS) for i in items]

        if q:
            needle = q.lower()
            runs = [
                r
                for r in runs
                if needle in (r.get("name") or "").lower()
                or needle in (r.get("description") or "").lower()
                or needle in (r.get("topic") or "").lower()
            ]

        runs.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
        runs = runs[:limit]
        for run in runs:
            run.update(await self.get_run_stats(run["id"], owner_sub=owner_sub))
        return runs

    async def list_runs_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Every run in a status, ACROSS ALL OWNERS. A `Scan`, and the only one here.

        Used solely by the startup stale-run sweep, which is a system operation with
        no caller identity — a crash orphans every tenant's in-flight run, so a
        scoped version would leave everyone else's reporting as live forever.

        A `Scan` is acceptable *because of who calls it*: it runs once per process
        start, and on AWS not at all (`STARTUP_SWEEP=false`, since the sweep's "this
        is the only process" premise is false with concurrent Lambda sandboxes).
        Nothing on a request path may use this.
        """
        items = await self._scan_all(
            "runs",
            FilterExpression="#s = :s AND begins_with(sk, :prefix)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status, ":prefix": "RUN#"},
        )
        runs = [_row(i, _RUN_FIELDS) for i in items]
        runs.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
        return runs

    async def get_run_stats(
        self, run_id: str, *, owner_sub: str
    ) -> Dict[str, Any]:
        """Turn count, total cost and last-event time, aggregated from the log.

        Recomputed from events rather than read off a counter on the run row. §4
        suggests keeping `total_cost_usd` there, and that is a fair optimisation
        later — but a maintained counter can drift from the log, and the log is the
        source of truth, so the derived-on-read version is the one that cannot be
        wrong. Bounded work: measured 80 events per run.
        """
        items = await self._query_all(
            "events",
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": _user_pk(owner_sub),
                ":prefix": _run_prefix(run_id),
            },
        )
        turn_count = 0
        total_cost = 0.0
        last_event_at: Optional[int] = None
        for item in items:
            created = item.get("created_at")
            if created is not None:
                created = int(created)
                last_event_at = created if last_event_at is None else max(
                    last_event_at, created
                )
            if item.get("event_type") != "agent.response":
                continue
            turn_count += 1
            try:
                payload = json.loads(item.get("payload") or "{}")
                total_cost += float(payload.get("cost_usd", 0.0) or 0.0)
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
        return {
            "turn_count": turn_count,
            "total_cost_usd": total_cost,
            "last_event_at": last_event_at,
        }

    # ------------------------------------------------------------------ #
    # Lineage
    # ------------------------------------------------------------------ #

    async def _all_runs(self, owner_sub: str) -> Dict[str, Dict[str, Any]]:
        """Every run this owner has, keyed by id. One query.

        The lineage methods below were two recursive SQL CTEs. Under a
        user-partitioned table the whole forest lives in one partition, so reading it
        once and walking it in memory is both simpler and fewer round trips than
        emulating recursion with a query per hop — and it is bounded by how many runs
        one person has, not by the table.

        Scoping falls out of the partition rather than being remembered: the
        recursion cannot leave the tenant because it never reads outside it. The SQL
        version needed an explicit `owner_sub` filter on every hop for the same
        guarantee, which is the kind of predicate that gets dropped in a refactor.
        """
        items = await self._query_all(
            "runs",
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": _user_pk(owner_sub),
                ":prefix": "RUN#",
            },
        )
        return {str(i["id"]): _row(i, _RUN_FIELDS) for i in items if "id" in i}

    async def get_run_tree(
        self, run_id: str, *, owner_sub: str
    ) -> Dict[str, Any]:
        """The full lineage forest rooted at this run's earliest ancestor.

        Returns `{root_id, nodes}` where each node carries the fields the tree view
        needs, including `config_json` so the caller can read `branch_mutation` for an
        edge label without a second round trip.
        """
        runs = await self._all_runs(owner_sub)
        if run_id not in runs:
            return {"root_id": run_id, "nodes": {}}

        # Walk up to the root, guarding against a cycle. A cycle cannot occur through
        # the normal branch path — a child always points at an existing parent — but
        # this walk is over data, and an import or a hand-edited row could produce
        # one. Without the guard that is an infinite loop in a request handler.
        root_id = run_id
        seen = {root_id}
        while True:
            parent = runs.get(root_id, {}).get("parent_run_id")
            if not parent or parent not in runs or parent in seen:
                break
            root_id = str(parent)
            seen.add(root_id)

        children: Dict[str, List[str]] = {}
        for rid, run in runs.items():
            parent = run.get("parent_run_id")
            if parent:
                children.setdefault(str(parent), []).append(rid)

        # Collect the subtree from the root down.
        nodes: Dict[str, Any] = {}
        frontier = [root_id]
        while frontier:
            rid = frontier.pop()
            if rid in nodes or rid not in runs:
                continue
            run = runs[rid]
            stats = await self.get_run_stats(rid, owner_sub=owner_sub)
            nodes[rid] = {
                "id": rid,
                "name": run.get("name"),
                "slug": run.get("slug"),
                "status": run.get("status"),
                "branch_turn": run.get("branch_turn"),
                "parent_run_id": run.get("parent_run_id"),
                "config_json": run.get("config_json"),
                "created_at": run.get("created_at"),
                "turn_count": stats["turn_count"],
                "total_cost_usd": stats["total_cost_usd"],
            }
            frontier.extend(children.get(rid, []))

        return {"root_id": root_id, "nodes": nodes}

    async def list_branches(
        self, run_id: str, *, owner_sub: str
    ) -> List[Dict[str, Any]]:
        """Runs forked directly from this one, newest first."""
        runs = await self._all_runs(owner_sub)
        out = [
            {
                "run_id": rid,
                "name": run.get("name"),
                "branch_turn": run.get("branch_turn"),
                "status": run.get("status"),
                "created_at": run.get("created_at"),
            }
            for rid, run in runs.items()
            if run.get("parent_run_id") == run_id
        ]
        out.sort(key=lambda b: (-(b["created_at"] or 0), b["run_id"]))
        return out

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #

    async def append_event(
        self,
        run_id: str,
        turn: int,
        seq: int,
        event_type: str,
        payload: Dict[str, Any],
        agent_name: Optional[str] = None,
        *,
        owner_sub: str,
    ) -> None:
        """Append one event.

        Conditional on the item not existing, which reproduces the SQLite
        `UNIQUE(run_id, turn, seq)` constraint. Without it a re-delivered write —
        a Step Functions retry, say — would silently overwrite a different event
        that happened to reuse the seq, and the log would be corrupt with no error.
        """
        item = {
            "pk": _user_pk(owner_sub),
            "sk": _event_sk(run_id, seq),
            "run_id": run_id,
            "turn": turn,
            "seq": seq,
            "event_type": event_type,
            "agent_name": agent_name,
            "payload": json.dumps(payload),
            "created_at": int(time.time()),
        }
        try:
            await self._call(
                self._table("events").put_item,
                Item=_to_ddb(item),
                ConditionExpression="attribute_not_exists(sk)",
            )
        except Exception as exc:  # noqa: BLE001
            if "ConditionalCheckFailed" in str(exc):
                raise StorageError(
                    f"event (run={run_id}, seq={seq}) already exists"
                ) from exc
            raise

    async def get_events(
        self,
        run_id: str,
        from_turn: int = 0,
        to_turn: Optional[int] = None,
        *,
        owner_sub: str,
    ) -> List[Dict[str, Any]]:
        """This run's events in a turn range, ordered by (turn, seq).

        Turn is not in the sort key — seq is — so the range is applied as an
        attribute filter over the run's contiguous items rather than through a second
        index. Key design §2: a GSI keyed on turn would add write cost to the
        hottest write path in the system to save filtering ~80 items.
        """
        items = await self._query_all(
            "events",
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": _user_pk(owner_sub),
                ":prefix": _run_prefix(run_id),
            },
        )
        out = [
            _row(i, _EVENT_FIELDS)
            for i in items
            if int(i["turn"]) >= from_turn
            and (to_turn is None or int(i["turn"]) <= to_turn)
        ]
        out.sort(key=lambda e: (e["turn"], e["seq"]))
        return out

    async def get_events_after(
        self,
        run_id: str,
        after_seq: int = -1,
        limit: Optional[int] = None,
        *,
        owner_sub: str,
    ) -> List[Dict[str, Any]]:
        """Events strictly after a global seq — the polling and replay path.

        A native sort-key range read, which is the whole reason the sort key leads
        with the run id and ends in a zero-padded seq. `after_seq=-1` means "from the
        beginning", and it cannot be expressed as `sk > RUN#{id}#-000000000001`, so
        it becomes a prefix query instead of a range.
        """
        if after_seq < 0:
            items = await self._query_all(
                "events",
                KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
                ExpressionAttributeValues={
                    ":pk": _user_pk(owner_sub),
                    ":prefix": _run_prefix(run_id),
                },
                limit=limit,
            )
        else:
            items = await self._query_all(
                "events",
                KeyConditionExpression=(
                    "pk = :pk AND sk BETWEEN :low AND :high"
                ),
                ExpressionAttributeValues={
                    ":pk": _user_pk(owner_sub),
                    # Exclusive lower bound: BETWEEN is inclusive, so start one past.
                    ":low": _event_sk(run_id, after_seq + 1),
                    # Upper bound is the prefix followed by the highest character
                    # DynamoDB will sort — keeps the read inside this run.
                    ":high": _run_prefix(run_id) + "9" * SEQ_WIDTH,
                },
                limit=limit,
            )
        out = [_row(i, _EVENT_FIELDS) for i in items]
        out.sort(key=lambda e: (e["turn"], e["seq"]))
        return out[:limit] if limit is not None else out

    async def last_event_turn(self, run_id: str, *, owner_sub: str) -> int:
        """Highest turn in the log, or 0. Reads the last item, not all of them."""
        items = await self._query_all(
            "events",
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": _user_pk(owner_sub),
                ":prefix": _run_prefix(run_id),
            },
        )
        return max((int(i["turn"]) for i in items), default=0)

    async def max_seq(self, run_id: str, *, owner_sub: str) -> int:
        """Highest event seq, or -1 for a run with no events.

        A single backwards read: the sort key ends in a zero-padded seq, so the last
        item in key order IS the highest seq. That equivalence is the payoff for the
        padding, and it is why this is O(1) rather than a scan of the run.
        """
        result = await self._call(
            self._table("events").query,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": _user_pk(owner_sub),
                ":prefix": _run_prefix(run_id),
            },
            ScanIndexForward=False,
            Limit=1,
        )
        items = result.get("Items") or []
        return int(items[0]["seq"]) if items else -1

    async def truncate_after_turn(
        self, run_id: str, turn: int, *, owner_sub: str
    ) -> int:
        """Delete events and snapshots past a turn; return the events removed.

        Used only when resuming an interrupted run in place, where the tail past the
        last checkpoint is a partial turn plus an interruption marker. Two queries
        and a batch delete, because DynamoDB has no `DELETE … WHERE`: the keys have
        to be read before they can be deleted.
        """
        events = await self._query_all(
            "events",
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": _user_pk(owner_sub),
                ":prefix": _run_prefix(run_id),
            },
        )
        doomed = [i for i in events if int(i["turn"]) > turn]
        await self._batch_delete("events", [
            {"pk": i["pk"], "sk": i["sk"]} for i in doomed
        ])

        snapshots = await self._query_all(
            "snapshots",
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": _user_pk(owner_sub),
                ":prefix": _run_prefix(run_id),
            },
        )
        stale = [i for i in snapshots if int(i["turn"]) > turn]
        await self._batch_delete("snapshots", [
            {"pk": i["pk"], "sk": i["sk"]} for i in stale
        ])
        # The S3 bodies are deliberately left. They are content-addressed by
        # (run, turn) and a later snapshot at the same turn overwrites in place, so an
        # orphan is invisible and cheap; deleting them would add a failure mode to a
        # path whose whole job is to make a broken run resumable. Phase 7 owns
        # lifecycle expiry.
        return len(doomed)

    async def copy_events_upto(
        self,
        source_run_id: str,
        dest_run_id: str,
        upto_turn: int,
        *,
        owner_sub: str,
    ) -> int:
        """Copy a run's log up to a turn into another run, preserving (turn, seq).

        The branch primitive. Same owner for both by construction — a branch inherits
        its parent's owner (`branching.create_branch_run` reads it off the parent
        rather than accepting it), so one partition is read and written.
        """
        items = await self._query_all(
            "events",
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": _user_pk(owner_sub),
                ":prefix": _run_prefix(source_run_id),
            },
        )
        source = sorted(
            (i for i in items if int(i["turn"]) <= upto_turn),
            key=lambda i: (int(i["turn"]), int(i["seq"])),
        )
        table = self._table("events")

        def write_batch(batch):
            with table.batch_writer() as writer:
                for item in batch:
                    writer.put_item(Item=item)

        copies = [
            _to_ddb(
                {
                    **_from_ddb(item),
                    "pk": _user_pk(owner_sub),
                    "sk": _event_sk(dest_run_id, int(item["seq"])),
                    "run_id": dest_run_id,
                }
            )
            for item in source
        ]
        if copies:
            await asyncio.to_thread(write_batch, copies)
        return len(copies)

    # ------------------------------------------------------------------ #
    # Snapshots
    # ------------------------------------------------------------------ #

    async def save_snapshot(
        self, snapshot: SimSnapshot, *, owner_sub: str
    ) -> None:
        """Body to S3, pointer to DynamoDB.

        Measured on the 38 real runs: mean 45 KB, max 2.2 MB, and 1 of 619 already
        over DynamoDB's 400 KB item limit — so inline storage is not a size risk to
        monitor, it is a write that fails at turn 30 of a real conversation.

        `status` is copied onto the pointer. `list_snapshots` needs it and nothing
        else from the body, so without this the checkpoint list would issue one S3
        GET per turn to render a column (key design §3). It is safe to denormalise
        because a snapshot is immutable once written — the copy is made in the same
        call as the body and never updated.

        Body first, pointer second, and the order is load-bearing: a body with no
        pointer is invisible and harmless, while a pointer with no body is a
        checkpoint that claims to exist and fails on read.
        """
        key = self._snapshot_key(owner_sub, snapshot.run_id, snapshot.turn)
        await self._put_body(key, snapshot.model_dump_json())
        await self._call(
            self._table("snapshots").put_item,
            Item=_to_ddb(
                {
                    "pk": _user_pk(owner_sub),
                    "sk": _snapshot_sk(snapshot.run_id, snapshot.turn),
                    "run_id": snapshot.run_id,
                    "turn": snapshot.turn,
                    "status": snapshot.status,
                    "s3_key": key,
                    "created_at": int(time.time()),
                }
            ),
        )

    async def get_snapshot(
        self, run_id: str, turn: Optional[int] = None, *, owner_sub: str
    ) -> Optional[SimSnapshot]:
        """A snapshot at a turn, or the latest. Pointer read, then one S3 GET."""
        if turn is not None:
            got = await self._call(
                self._table("snapshots").get_item,
                Key={
                    "pk": _user_pk(owner_sub),
                    "sk": _snapshot_sk(run_id, turn),
                },
            )
            item = got.get("Item")
        else:
            # Highest turn == last item in key order, because the sort key ends in a
            # zero-padded turn. One backwards read rather than a full query.
            result = await self._call(
                self._table("snapshots").query,
                KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
                ExpressionAttributeValues={
                    ":pk": _user_pk(owner_sub),
                    ":prefix": _run_prefix(run_id),
                },
                ScanIndexForward=False,
                Limit=1,
            )
            items = result.get("Items") or []
            item = items[0] if items else None

        if not item:
            return None
        body = await self._get_body(str(item["s3_key"]))
        if body is None:
            return None
        return SimSnapshot.model_validate_json(body)

    async def list_snapshots(
        self, run_id: str, *, owner_sub: str
    ) -> List[Dict[str, Any]]:
        """Checkpoint turns for a run: `{turn, status, created_at}`, turn ascending.

        Reads only DynamoDB. `status` comes off the pointer rather than the body,
        which is what keeps this one query instead of one query plus N S3 GETs.
        """
        items = await self._query_all(
            "snapshots",
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": _user_pk(owner_sub),
                ":prefix": _run_prefix(run_id),
            },
        )
        out = [
            {
                "turn": int(i["turn"]),
                "status": i.get("status"),
                "created_at": int(i["created_at"]) if i.get("created_at") else None,
            }
            for i in items
        ]
        out.sort(key=lambda s: s["turn"])
        return out

    async def last_checkpoint_turn(
        self, run_id: str, *, owner_sub: str
    ) -> Optional[int]:
        """Highest checkpoint turn, or None. One backwards read, as `get_snapshot`."""
        result = await self._call(
            self._table("snapshots").query,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": _user_pk(owner_sub),
                ":prefix": _run_prefix(run_id),
            },
            ScanIndexForward=False,
            Limit=1,
        )
        items = result.get("Items") or []
        return int(items[0]["turn"]) if items else None

    # ------------------------------------------------------------------ #
    # Summaries
    # ------------------------------------------------------------------ #

    async def save_summary(
        self,
        run_id: str,
        payload: Dict[str, Any],
        kind: str = "generated",
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        instructions: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append a summary version. The getters return the latest per kind.

        Versioned rather than replaced, so a regenerated summary does not destroy
        the one it replaced — and a generated summary can never overwrite an
        imported one, because they are different `kind` values.

        The sort key is `SUM#{kind}#{id:012d}` with `id` from the atomic counter, so
        "latest of this kind" is a single backwards read on a `SUM#{kind}#` prefix
        rather than a sort over every version. A timestamp would not do: two
        regenerations in the same second would order arbitrarily.

        No `owner_sub`: summaries are partitioned by run, and the caller reached this
        run through `get_run_by_ref(ref, owner_sub=...)` — which is also why the
        `documents`, `threads` and `summaries` tables are run-partitioned at all
        (§4: a shared knowledge base is read by principals who do not own it, so a
        user prefix would make sharing inexpressible).
        """
        seq = await self._next_id("summaries", _run_pk(run_id))
        created_at = int(time.time())
        item = {
            "pk": _run_pk(run_id),
            "sk": f"SUM#{kind}#{seq:0{SEQ_WIDTH}d}",
            "id": seq,
            "run_id": run_id,
            "kind": kind,
            "payload_json": json.dumps(payload),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
            "instructions": instructions,
            "created_at": created_at,
        }
        await self._call(self._table("summaries").put_item, Item=_to_ddb(item))
        return {
            "id": seq,
            "run_id": run_id,
            "kind": kind,
            "payload": payload,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
            "instructions": instructions,
            "created_at": created_at,
        }

    async def get_summaries(self, run_id: str) -> List[Dict[str, Any]]:
        """The latest summary of each kind, with its payload parsed."""
        items = await self._query_all(
            "summaries",
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={":pk": _run_pk(run_id), ":prefix": "SUM#"},
        )
        latest: Dict[str, Dict[str, Any]] = {}
        for raw in sorted(items, key=lambda i: int(i["id"]), reverse=True):
            row = _from_ddb(raw)
            if row["kind"] in latest:
                continue
            try:
                parsed = json.loads(row.get("payload_json") or "{}")
            except json.JSONDecodeError:
                parsed = {}
            latest[row["kind"]] = {
                "id": row["id"],
                "run_id": row["run_id"],
                "kind": row["kind"],
                "payload": parsed,
                "tokens_in": row.get("tokens_in", 0),
                "tokens_out": row.get("tokens_out", 0),
                "cost_usd": row.get("cost_usd", 0.0),
                "instructions": row.get("instructions"),
                "created_at": row["created_at"],
            }
        return list(latest.values())

    # ------------------------------------------------------------------ #
    # Aside threads
    # ------------------------------------------------------------------ #

    async def create_thread(
        self,
        thread_id: str,
        run_id: str,
        target: str,
        persona_name: Optional[str] = None,
        mode: str = "aside",
    ) -> Dict[str, Any]:
        """Open an aside thread over a run.

        `thread_id` is written as its own attribute as well as into the sort key,
        because the `by-thread-id` GSI needs it as a partition key — a GSI cannot
        key on a substring of another key.
        """
        created_at = int(time.time())
        item = {
            "pk": _run_pk(run_id),
            "sk": _thread_sk(thread_id),
            "thread_id": thread_id,
            "id": thread_id,
            "run_id": run_id,
            "target": target,
            "persona_name": persona_name,
            "mode": mode,
            "created_at": created_at,
        }
        await self._call(self._table("threads").put_item, Item=_to_ddb(item))
        return {
            "id": thread_id,
            "run_id": run_id,
            "target": target,
            "persona_name": persona_name,
            "mode": mode,
            "created_at": created_at,
        }

    async def get_thread(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """A thread by its own id, with no run context — via the GSI (key design §5).

        Two reads: the KEYS_ONLY index gives the base-table key, then a `GetItem`
        returns the item. The index deliberately projects no attributes, so there is
        no way to serve a thread straight off it and skip the base-table read that
        the caller's ownership check is paired with.

        Authorisation is the caller's: `api/app.py` routes both thread endpoints
        through `_require_thread_run`, which resolves the run via the scoped
        `get_run_by_ref` and answers 404 for another tenant's thread.
        """
        found = await self._call(
            self._table("threads").query,
            IndexName="by-thread-id",
            KeyConditionExpression="thread_id = :tid",
            ExpressionAttributeValues={":tid": thread_id},
            Limit=1,
        )
        keys = found.get("Items") or []
        if not keys:
            return None
        got = await self._call(
            self._table("threads").get_item,
            Key={"pk": keys[0]["pk"], "sk": keys[0]["sk"]},
        )
        item = got.get("Item")
        return _row(item, _THREAD_FIELDS) if item else None

    async def list_threads(self, run_id: str) -> List[Dict[str, Any]]:
        """A run's threads, oldest first, each with its message count and cost.

        The counts were a SQL `LEFT JOIN … GROUP BY`. DynamoDB has no join, so they
        come from one query per thread. Bounded by how many asides a human opens on
        one run — single digits — and issued concurrently rather than in series,
        because the alternative is maintaining counters on the thread row that can
        drift from the messages they describe.
        """
        items = await self._query_all(
            "threads",
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={":pk": _run_pk(run_id), ":prefix": "THREAD#"},
        )
        threads = [_row(i, _THREAD_FIELDS) for i in items]
        threads.sort(key=lambda t: (t.get("created_at") or 0, t["id"]))
        if not threads:
            return []

        message_lists = await asyncio.gather(
            *[self.get_thread_messages(t["id"]) for t in threads]
        )
        for thread, messages in zip(threads, message_lists):
            thread["message_count"] = len(messages)
            thread["total_cost_usd"] = sum(
                float(m.get("cost_usd") or 0.0) for m in messages
            )
        return threads

    async def add_thread_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        speaker: Optional[str] = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
    ) -> Dict[str, Any]:
        """Append a message to a thread.

        The counter's main reason for existing (key design §4): messages are ordered
        BY this id, and `int(time.time())` has one-second resolution — two fast
        replies would order arbitrarily, which is exactly when it happens.
        """
        seq = await self._next_id("thread-messages", _thread_pk(thread_id))
        created_at = int(time.time())
        item = {
            "pk": _thread_pk(thread_id),
            "sk": f"MSG#{seq:0{SEQ_WIDTH}d}",
            "id": seq,
            "thread_id": thread_id,
            "role": role,
            "speaker": speaker,
            "content": content,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
            "created_at": created_at,
        }
        await self._call(self._table("thread-messages").put_item, Item=_to_ddb(item))
        return {
            "id": seq,
            "thread_id": thread_id,
            "role": role,
            "speaker": speaker,
            "content": content,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
            "created_at": created_at,
        }

    async def get_thread_messages(self, thread_id: str) -> List[Dict[str, Any]]:
        """A thread's messages, oldest first — native sort-key order."""
        items = await self._query_all(
            "thread-messages",
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": _thread_pk(thread_id),
                ":prefix": "MSG#",
            },
        )
        out = [_row(i, _THREAD_MESSAGE_FIELDS) for i in items]
        out.sort(key=lambda m: int(m["id"]))
        return out

    async def thread_cost(self, thread_id: str) -> float:
        """Total cost of a thread's messages, counted separately from the run's."""
        messages = await self.get_thread_messages(thread_id)
        return sum(float(m.get("cost_usd") or 0.0) for m in messages)

    # ------------------------------------------------------------------ #
    # Documents (metadata in DynamoDB, text in S3)
    # ------------------------------------------------------------------ #

    def _document_key(self, owner_sub: str, run_id: str, doc_id: str) -> str:
        return f"docs/{owner_sub}/{run_id}/{doc_id}.txt"

    async def add_document(
        self,
        run_id: str,
        title: str,
        chunks: List[str],
        persona_name: Optional[str] = None,
        source_path: Optional[str] = None,
        media_type: Optional[str] = None,
        char_count: int = 0,
        document_id: Optional[str] = None,
        text: Optional[str] = None,
        *,
        owner_sub: str,
    ) -> str:
        """Store a document: normalised text to S3, metadata to DynamoDB.

        **`text` is new, and it exists because §4a's plan does not hold without it.**

        §4a says chunk text need not be stored anywhere, because the lexical arm can
        re-chunk the normalised text in memory: `chunk_text()` is deterministic, so
        it "reproduces the same chunks and the same `ordinal`s the vectors were built
        from". Determinism is the wrong property. What is needed is a round trip —
        `chunk_text(stored_text)` must equal the chunks that were embedded — and
        measured on ten real documents that **fails on two of them**:

            docs/AWS-SERVERLESS-ARCHITECTURE.md   3 of 110 ordinals differ
            README.md                             4 of  55 ordinals differ

        The cause is that `join_chunks` is not a perfect inverse of `chunk_text`. It
        de-overlaps well but not exactly — 72,149 characters reassembled from 72,136,
        and 37,359 from 37,348 — and those extra characters shift a few chunk
        boundaries. So an ordinal in the re-chunked text can point at *different
        text* from the vector built at that ordinal, and hybrid retrieval fuses the
        two arms by chunk id. A passage cited under the wrong ordinal, silently.

        Passing the **original extracted text** removes the problem at the root
        rather than papering over it: re-chunking then feeds the same input to the
        same function, so it reproduces the chunks exactly. Every caller already
        holds it as `ExtractedDocument.text`.

        Falling back to `join_chunks(chunks)` when `text` is absent keeps the older
        callers working, at the cost of the boundary drift above. `chunk_count` is
        recorded either way, so a mismatch is at least detectable.

        Text first, metadata second: an object with no metadata row is invisible and
        harmless, while a row pointing at a missing object is a document that lists
        but cannot be opened.
        """
        from matrix_studio.documents import join_chunks

        doc_id = document_id or uuid.uuid4().hex[:12]
        key = self._document_key(owner_sub, run_id, doc_id)
        body = text if text is not None else join_chunks(list(chunks))
        await self._put_text(key, body)

        now = int(time.time())
        item = {
            "pk": _run_pk(run_id),
            "sk": _document_sk(doc_id),
            "document_id": doc_id,
            "id": doc_id,
            "run_id": run_id,
            "persona_name": persona_name,
            "title": title,
            "source_path": source_path,
            "media_type": media_type,
            "char_count": char_count,
            "chunk_count": len(chunks),
            # Whether the stored text is the original or a reassembly. Phase 3 needs
            # to know: re-chunking is only safe against the original (see above), so
            # a document written by an older caller has to keep its chunks.
            "text_is_original": text is not None,
            "s3_key": key,
            "owner_sub": owner_sub,
            "created_at": now,
        }
        await self._call(self._table("documents").put_item, Item=_to_ddb(item))
        return doc_id

    async def list_documents(
        self, run_id: str, persona_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """A run's documents, newest first.

        `persona_name` filters to what that persona may retrieve: its own plus
        cast-wide (a NULL persona). Applied in Python — the alternative, a
        `FilterExpression` with `attribute_not_exists`, reads the same items and is
        billed the same, so the only difference would be that the OR-with-NULL case
        is harder to see.
        """
        items = await self._query_all(
            "documents",
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={":pk": _run_pk(run_id), ":prefix": "DOC#"},
        )
        docs = [_row(i, _DOCUMENT_FIELDS) for i in items]
        if persona_name is not None:
            docs = [
                d for d in docs
                if d.get("persona_name") in (persona_name, None)
            ]
        docs.sort(key=lambda d: (-(d.get("created_at") or 0), d["id"]))
        return docs

    async def _find_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        """Locate a document by its own id via the GSI (key design §5)."""
        found = await self._call(
            self._table("documents").query,
            IndexName="by-document-id",
            KeyConditionExpression="document_id = :did",
            ExpressionAttributeValues={":did": document_id},
            Limit=1,
        )
        keys = found.get("Items") or []
        if not keys:
            return None
        got = await self._call(
            self._table("documents").get_item,
            Key={"pk": keys[0]["pk"], "sk": keys[0]["sk"]},
        )
        item = got.get("Item")
        return _row(item, _DOCUMENT_FIELDS) if item else None

    async def document_text(self, document_id: str) -> str:
        """A document's full text — one S3 GET.

        Empty string for a missing document, matching the SQLite version (which
        returned `join_chunks([])`). The setup-export path calls this for every
        document and treats an empty result as "nothing to carry", so raising would
        turn one deleted document into a failed export.
        """
        doc = await self._find_document(document_id)
        if not doc or not doc.get("s3_key"):
            return ""
        return await self._get_text(str(doc["s3_key"])) or ""

    async def delete_document(self, document_id: str) -> bool:
        """Delete a document's metadata and its S3 object. True if it existed.

        Metadata first, then the object: it is the metadata that makes the document
        listable and retrievable, so removing it first means a failure part-way
        leaves an orphaned object rather than a listed document that cannot be read.
        Phase 3 adds the third store — the document's vectors — and `chunk_count` on
        the row is what makes those bounded to delete (§4a).
        """
        doc = await self._find_document(document_id)
        if not doc:
            return False
        await self._call(
            self._table("documents").delete_item,
            Key={"pk": _run_pk(str(doc["run_id"])), "sk": _document_sk(document_id)},
        )
        key = doc.get("s3_key")
        if key and self.bucket:
            try:
                await self._call(
                    self._s3.delete_object, Bucket=self.bucket, Key=str(key)
                )
            except Exception as exc:  # noqa: BLE001
                # The document is already gone from the user's point of view. An
                # orphaned object costs storage, not correctness, so this must not
                # turn a successful delete into an error the operator has to retry.
                logger.warning(
                    "Document %s deleted, but its S3 object %s was not: %s",
                    document_id, key, exc,
                )
        return True

    async def count_documents(self, run_id: str) -> int:
        """How many documents a run has."""
        return len(await self.list_documents(run_id))

    async def copy_documents_to_run(
        self, from_run_id: str, to_run_id: str, *, owner_sub: str
    ) -> int:
        """Copy a run's documents to another run — the branch path.

        **Each copy gets a NEW document id and its OWN S3 object**, matching the
        SQLite version, whose docstring said why: "new document ids are minted so the
        branch owns its own rows".

        An earlier version of this method reused the id and shared the object,
        rationalised as "document text is immutable once ingested, so copying bytes
        buys nothing". Immutable *content* was the wrong property to reason from —
        what matters is *lifetime*, and two things went wrong:

        1. **Two items shared one `document_id`**, so the `by-document-id` GSI held a
           duplicate and `_find_document`'s `Limit=1` could resolve to either run's
           row. `delete_document` would then delete the wrong run's document while
           reporting success.
        2. **Deleting either copy destroyed the shared object**, so the other run's
           `document_text` silently became "" — a persona whose background material
           evaporated because somebody tidied up a different conversation.

        Copying is cheap enough that the hazard is not worth managing: measured mean
        document text is 11 KB, and branching is a deliberate, infrequent action.
        Phase 6 removes the copy entirely — a branch will inherit KB *bindings* — and
        that is the right place for sharing, because a binding is a reference with
        explicit lifetime rather than an implicit one.
        """
        source = await self.list_documents(from_run_id)
        if not source:
            return 0

        for doc in source:
            text = await self.document_text(str(doc["id"]))
            new_id = uuid.uuid4().hex[:12]
            key = self._document_key(owner_sub, to_run_id, new_id)
            await self._put_text(key, text)
            await self._call(
                self._table("documents").put_item,
                Item=_to_ddb(
                    {
                        **doc,
                        "pk": _run_pk(to_run_id),
                        "sk": _document_sk(new_id),
                        "id": new_id,
                        "document_id": new_id,
                        "run_id": to_run_id,
                        "owner_sub": owner_sub,
                        "s3_key": key,
                        "created_at": int(time.time()),
                    }
                ),
            )
        return len(source)

    # ------------------------------------------------------------------ #
    # Paging helpers
    # ------------------------------------------------------------------ #

    async def _query_all(
        self, table: str, *, limit: Optional[int] = None, **kwargs
    ) -> List[Dict[str, Any]]:
        """Query, following `LastEvaluatedKey` to the end.

        Paging is not optional. DynamoDB caps a page at 1 MB, and a run's events are
        measured at mean 1.2 KB — so ~800 events fit one page and a longer run
        silently returns a prefix. "Silently" is the problem: a truncated event log
        replays as a shorter conversation with no error anywhere, which is
        indistinguishable from a run that really was that short.
        """
        out: List[Dict[str, Any]] = []
        start_key: Optional[Dict[str, Any]] = None
        while True:
            params = dict(kwargs)
            if start_key:
                params["ExclusiveStartKey"] = start_key
            result = await self._call(self._table(table).query, **params)
            out.extend(result.get("Items") or [])
            if limit is not None and len(out) >= limit:
                return out[:limit]
            start_key = result.get("LastEvaluatedKey")
            if not start_key:
                return out

    async def _scan_all(self, table: str, **kwargs) -> List[Dict[str, Any]]:
        """Scan, following pages. See `list_runs_by_status` for the only caller."""
        out: List[Dict[str, Any]] = []
        start_key: Optional[Dict[str, Any]] = None
        while True:
            params = dict(kwargs)
            if start_key:
                params["ExclusiveStartKey"] = start_key
            result = await self._call(self._table(table).scan, **params)
            out.extend(result.get("Items") or [])
            start_key = result.get("LastEvaluatedKey")
            if not start_key:
                return out

    async def _batch_delete(self, table: str, keys: List[Dict[str, Any]]) -> None:
        if not keys:
            return
        handle = self._table(table)

        def run():
            with handle.batch_writer() as writer:
                for key in keys:
                    writer.delete_item(Key=key)

        await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# DynamoDB type conversion
# --------------------------------------------------------------------------- #
#
# DynamoDB has no NULL-able attribute in the SQL sense and no float type. Both
# gaps would otherwise leak into callers, which index `run["description"]`
# expecting None and compare `cost_usd` as a float.


def _to_ddb(item: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare an item for writing.

    Floats become `Decimal`, because the DynamoDB serialiser refuses `float`
    outright — a design choice on its part, since binary floats do not round-trip.
    `None` values are dropped rather than stored: DynamoDB's NULL type exists but
    reads back as `None` only if the attribute is present, and an absent attribute
    is cheaper and reads the same way through `_from_ddb`.
    """
    from decimal import Decimal

    out: Dict[str, Any] = {}
    for key, value in item.items():
        if value is None:
            continue
        out[key] = Decimal(str(value)) if isinstance(value, float) else value
    return out


def _to_wire(item: Dict[str, Any]) -> Dict[str, Any]:
    """Serialise to the low-level `{"S": "…"}` wire format.

    Needed only for `transact_write_items`, which exists on the client and not on the
    resource — and the client takes the wire format while `Table.put_item` takes plain
    Python. The two levels look interchangeable and are not; getting it wrong is a
    `ParamValidationError` listing every attribute, which reads like a schema problem
    rather than a layering one.
    """
    from boto3.dynamodb.types import TypeSerializer

    serializer = TypeSerializer()
    return {k: serializer.serialize(v) for k, v in _to_ddb(item).items()}


def _from_ddb(item: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a read item back to plain Python.

    `Decimal` becomes `int` when it is integral and `float` otherwise. Without this
    every cost would arrive as a `Decimal`, and `Decimal("0.001") == 0.001` is False
    — so a test comparing a cost, or JSON-encoding a response, would fail in a way
    that points at the assertion rather than at the storage layer.
    """
    from decimal import Decimal

    out: Dict[str, Any] = {}
    for key, value in item.items():
        if isinstance(value, Decimal):
            out[key] = int(value) if value == value.to_integral_value() else float(value)
        else:
            out[key] = value
    return out
