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

#: Every table this layer reads, without the deployment prefix.
#:
#: Named once because two things need it: the per-tenant session policy (which has to
#: grant exactly these) and the test that checks the storage layer, the CDK stack and
#: the fixture all name the same set.
_TABLES = (
    "runs", "events", "snapshots", "summaries", "threads", "thread-messages",
    "documents",
)


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
        # `is None` rather than a falsy check, so an explicit empty string means
        # "explicitly none" instead of "not supplied". The `or` form conflated them:
        # `DynamoStorage(bucket="")` silently picked up `DATA_BUCKET` from the
        # environment, which makes "no bucket configured" impossible to express and
        # impossible to test.
        self.table_prefix = (
            table_prefix if table_prefix is not None
            else os.environ.get("TABLE_PREFIX", "matrix-studio")
        )
        self.bucket = (
            bucket if bucket is not None else os.environ.get("DATA_BUCKET", "")
        )
        self.region = (
            region if region is not None
            else (os.environ.get("AWS_REGION") or "us-east-1")
        )
        self._ddb = None
        self._s3 = None
        # The low-level client, needed for `transact_write_items`, which has no
        # resource-level equivalent. Kept separate and named for what it is: the two
        # levels take DIFFERENT item formats, and mixing them is a silent-looking
        # ParamValidationError at the first write.
        self._client = None
        self._s3vectors = None
        self._tables: Dict[str, Any] = {}
        # Set only by `for_owner`. Absent on the unbound store, which is
        # what makes an unbound call raise instead of guessing.
        self._owner_sub: Optional[str] = None
        # A tenant-scoped boto3 Session, set by `for_owner` when a tenant role is
        # configured. None means "use the ambient credentials", which is the local
        # single-user case.
        self._session = None

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
        self._ddb = self._make("resource", "dynamodb")
        self._client = self._make("client", "dynamodb")
        self._s3 = self._make("client", "s3")
        await self._announce()

    def _make(self, kind: str, service: str):
        """Build a client or resource, from the tenant-scoped session when bound.

        Every AWS object in this class goes through here, so there is exactly one place
        that decides which credentials are used — and therefore one place to get the
        tenancy boundary wrong.
        """
        import boto3

        source = self._session or boto3
        factory = source.resource if kind == "resource" else source.client
        return factory(service, region_name=self.region)

    async def _announce(self) -> None:
        """Say plainly which store this is, and WARN when it holds nothing.

        This is the same job the SQLite layer's startup log did, for the same reason
        and against the same failure. There, a relative `data_dir` resolved against the
        working directory, so starting the server from a subdirectory silently created
        a second empty database — the UI honestly reported no previous conversations
        while the real runs sat in another file, and nothing in the logs distinguished
        that from data loss.

        The analogue here is exact: a wrong `TABLE_PREFIX` (or the wrong region, or the
        wrong account) points at tables that are absent or empty, and the symptom is
        identical — an empty history and no error. So the same remedy applies: name what
        was opened, and raise the level when there is nothing in it.

        `DescribeTable` rather than a count: it is key-addressed and cheap, it
        distinguishes "the table is not there" from "the table is empty" — which are
        different mistakes with different fixes — and its approximate `ItemCount` is
        free. A denial is logged at debug and ignored, because the per-request scoped
        role has no reason to hold `DescribeTable` and this is a convenience, not a
        precondition.
        """
        target = (
            f"tables '{self.table_prefix}-*' in {self.region}, bodies in "
            f"s3://{self.bucket or '<UNSET — DATA_BUCKET is empty, writes will fail>'}"
        )
        try:
            described = await self._call(
                self._client.describe_table,
                TableName=f"{self.table_prefix}-runs",
            )
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            if "ResourceNotFound" in name or "ResourceNotFound" in str(exc):
                logger.warning(
                    "Storage: %s — THE RUNS TABLE DOES NOT EXIST, so no previous "
                    "conversations will be listed and every write will fail. If you "
                    "expected existing runs, check TABLE_PREFIX and the region.",
                    target,
                )
                return
            logger.debug("Storage: %s (could not describe: %s)", target, exc)
            logger.info("Storage: %s", target)
            return

        count = int(described["Table"].get("ItemCount") or 0)
        if count:
            logger.info("Storage: %s (~%d run item(s))", target, count)
        else:
            # "Reports empty", not "is empty". DynamoDB's `ItemCount` is refreshed
            # roughly every six hours, so a table populated minutes ago still reports
            # zero — observed on the first real deployment, where this warned EMPTY with
            # two runs present.
            #
            # The wording matters more than it looks. A warning that turns out to be
            # wrong is worse than none: an operator who sees "EMPTY" contradicted by a
            # working UI learns to ignore the line, and then it cannot do its job on the
            # day the prefix really is wrong. Saying the count is approximate keeps the
            # signal and drops the false certainty.
            logger.warning(
                "Storage: %s — reports NO run items. If you expected existing runs, "
                "check TABLE_PREFIX and the region. Note the count is DynamoDB's "
                "approximate ItemCount and lags by up to ~6 hours, so a recently "
                "populated table can report zero.",
                target,
            )

    async def close(self) -> None:
        """Drop the clients. Nothing to flush — every write is already durable."""
        self._ddb = None
        self._client = None
        self._s3 = None
        self._s3vectors = None
        self._tables.clear()

    def for_owner(self, owner_sub: str) -> "DynamoStorage":
        """A view of this store bound to one tenant, for the life of one request.

        **Why binding rather than an argument on every call.** The Phase 2 key design
        (§6) weighed two options — pass `owner_sub` explicitly everywhere, or resolve
        it inside the store from `run_id` — and chose the first, on the grounds that a
        caller who forgets an explicit argument fails with a `TypeError` rather than
        reading the wrong partition. That reasoning is right and this preserves it;
        what it missed is that there was a third option.

        Threading the argument through every call meant **325 call sites** (63 in the
        application, 262 in the tests and scripts). The number is not the objection —
        the objection is that a change of that size, applied mechanically, is where a
        wrong `owner_sub` gets typed once and is never noticed, because every site
        looks like every other site.

        Binding is *stronger* than the explicit argument, not a relaxation of it:

          * The owner is named **once per request**, at the boundary, where the
            identity actually arrives from the JWT — so a route cannot omit it, it can
            only fail to bind at all.
          * An unbound store raises on first use (see `_owner`), so "forgot to bind"
            is loud in exactly the way "forgot the argument" was.
          * An explicit `owner_sub=` still wins, which is what lets a test assert a
            cross-tenant read is refused.

        It also mirrors §3's real mechanism: per-request credentials scoped to a `sub`
        for the duration of a request. `for_owner` is the code-level shape of the same
        idea, and it is where those credentials attach.

        **Binding does two things, and they are easy to conflate.** It scopes the QUERY
        (the partition key it reads) *and* it attaches the CREDENTIALS the call is signed
        with. Those are independent, and missing the second is how this broke twice on
        real AWS:

        * `db.method(..., owner_sub=user)` scopes the query and leaves the credentials
          ambient — so on a deployment where the function's own role holds no storage
          rights, every such call is an AccessDenied whose message blames permissions
          rather than the mixed idiom. Half of `api/app.py` used that form, inherited
          from Phase 0.2 when there were no scoped credentials to attach.
        * The run-partitioned tables (`documents`, `threads`, `summaries`) take **no
          owner at all**, because they are keyed `RUN#{run_id}` so a shared knowledge
          base can be read by principals who do not own it. It is tempting to conclude
          they need no binding either. They do: they need no owner for the KEY and they
          still need CREDENTIALS.

        So there is one idiom — bind at the boundary — and `owner_sub=` survives only as
        an override for tests asserting a cross-tenant read is refused.

        The view shares this store's clients — it is a shallow copy, not a new
        connection — so creating one per request costs nothing. The thing not to do is
        hold one past its request, since it carries an identity.
        """
        import copy as _copy

        from matrix_studio.storage.credentials import scoped_session, tenant_role_arn

        bound = _copy.copy(self)
        bound._owner_sub = owner_sub

        # §3's isolation attaches HERE, and this is the whole of it in the application:
        # when a tenant role is configured, the bound view's clients come from
        # credentials whose session policy pins `dynamodb:LeadingKeys` to
        # `USER#{sub}` and S3 object ARNs to `…/{sub}/*`. A query that omits the tenant
        # filter is then refused by the service, not filtered by us.
        #
        # This was missing at first, and the gap is worth recording: the role, the
        # policy builder and the CDK wiring all existed and were verified against the
        # real account, and NOTHING CONNECTED THEM to the storage layer — so the
        # deployed API called DynamoDB with the function's own credentials and got
        # AccessDenied on every read. The unit suite could not have caught it, because
        # `moto` does not evaluate IAM. Only deploying did.
        role = tenant_role_arn()
        if role:
            bound._session = scoped_session(
                owner_sub,
                role_arn=role,
                table_arns=[
                    f"arn:aws:dynamodb:{self.region}:*:table/{self.table_prefix}-{t}"
                    for t in _TABLES
                ],
                bucket_arn=f"arn:aws:s3:::{self.bucket}",
                region=self.region,
            )
            # Force the clients to be rebuilt from the scoped session rather than
            # inherited from the unbound store by the shallow copy.
            bound._ddb = None
            bound._client = None
            bound._s3 = None
            bound._s3vectors = None
            bound._tables = {}
        return bound

    def _owner(self, owner_sub: Optional[str]) -> str:
        """Resolve the tenant for a call: the explicit argument, else the binding.

        Raises rather than defaulting. An unbound store with no explicit owner means
        the identity was lost somewhere upstream, and the safe answer to "whose data
        is this" being unanswerable is not "the local user's" — that would attribute
        one person's conversation to a shared bucket, silently.
        """
        bound = getattr(self, "_owner_sub", None)
        resolved = owner_sub or bound
        if not resolved:
            raise StorageError(
                "no owner for this call. Either bind the store with "
                "`db.for_owner(sub)` at the request boundary, or pass "
                "`owner_sub=` explicitly."
            )
        # An explicit owner that disagrees with the SCOPED CREDENTIALS cannot work, and
        # the way it fails is unhelpful: the query would be built for one tenant and
        # signed for another, so DynamoDB answers AccessDenied and the message says
        # nothing about the mismatch.
        #
        # Only checked when a scoped session exists. Without one — the local
        # single-user server, and the test suite — the override is exactly how a
        # negative case asserts that another tenant's data is unreachable, so it must
        # keep working.
        if (
            owner_sub
            and bound
            and owner_sub != bound
            and getattr(self, "_session", None) is not None
        ):
            raise StorageError(
                f"this store is bound to {bound!r} and holds credentials scoped to "
                f"it, but the call asked for {owner_sub!r}. Bind a separate view with "
                "`db.for_owner(...)` instead of overriding the owner on a bound store."
            )
        return resolved

    @property
    def vec_available(self) -> bool:
        """Whether vector retrieval can be used. Always true on S3 Vectors.

        **This property is load-bearing and its absence is silent.**
        `retrieval.retrieve_for_turn` gates the entire vector arm on
        `getattr(db, "vec_available", False)` — so a store without it degrades every
        run to lexical retrieval, which is measured 22× worse at recall@1 (0.017 vs
        0.367, PHASE5-RETRIEVAL-MEASUREMENT §5f). The port omitted it at first, and
        nothing failed: retrieval simply became the arm that was measured not to work,
        with one warning line to show for it.

        On SQLite this was genuinely conditional — it reported whether the optional
        `sqlite-vec` extension had loaded. Here the vector store is a service, not an
        extension, so the answer is a constant. Kept as a property rather than deleted
        because `retrieval.py` asks the question and this is the honest answer to it;
        removing it would mean the caller's `getattr` default silently wins.

        Whether an individual query *succeeds* is a different matter, and the fallback
        for that failure already exists — `vector_search` returns no rows and
        `retrieve_for_turn` degrades to lexical with a warning naming the cause.
        """
        return True

    def _ensure_clients(self) -> None:
        """Build the clients if they are not there yet.

        A bound view discards the clients it inherited from the unbound store (they
        carry the wrong credentials), so it builds its own on first use from the scoped
        session. Every accessor goes through here.

        It has to be EVERY accessor, which is how this was wrong: the rebuild lived
        inside `_table()`, so a bound view whose first operation was an S3 write —
        attaching a document, saving a snapshot — hit `self._s3` still None and failed
        with `'NoneType' object has no attribute 'put_object'`. Nothing in the unit
        suite reached it, because without a tenant role there is no rebuild to miss.
        """
        if self._ddb is None:
            self._ddb = self._make("resource", "dynamodb")
        if self._client is None:
            self._client = self._make("client", "dynamodb")
        if self._s3 is None:
            self._s3 = self._make("client", "s3")

    def _table(self, name: str):
        if name not in self._tables:
            self._ensure_clients()
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
        self._ensure_clients()
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
        self._ensure_clients()
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
        self._ensure_clients()
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
        owner_sub: Optional[str] = None,
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
        # Falls back to LOCAL_USER_SUB only when there is neither an argument nor a
        # binding — the CLI, the import script, a direct engine call. A BOUND store
        # attributes the run to its binding, which is what makes the API path correct
        # without every caller repeating the owner.
        owner_sub = owner_sub or getattr(self, "_owner_sub", None) or LOCAL_USER_SUB
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
            reasons = getattr(exc, "response", {}).get("CancellationReasons") or []
            codes = [r.get("Code") for r in reasons]
            if not any(c == "ConditionalCheckFailed" for c in codes):
                raise
            # `CancellationReasons` is positionally aligned with `TransactItems`, so it
            # says WHICH condition failed. Worth extracting: the two causes have
            # different remedies — a duplicate run id is a caller bug, while a
            # duplicate name is expected and callers recover from it by appending a
            # suffix (see `manager.create_run`). An error naming both leaves whoever
            # reads it to guess, and they will guess the wrong one.
            if codes[0] == "ConditionalCheckFailed":
                raise DuplicateNameError(
                    f"a run with id {run_id!r} already exists for this owner"
                ) from exc
            raise DuplicateNameError(
                f"this owner already has a run named {name!r}"
            ) from exc

    async def name_exists(self, name: str, *, owner_sub: Optional[str] = None) -> bool:
        """Whether THIS OWNER already has a run with this name.

        Reads the marker item, not the runs — so it is a `GetItem` rather than a
        query, and it stays correct even for a run whose row was deleted while its
        name marker remained.
        """
        owner_sub = self._owner(owner_sub)
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
        owner_sub: Optional[str] = None,
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
        owner_sub = self._owner(owner_sub)
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
        self, run_id: str, *, owner_sub: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """One run by id.

        `owner_sub` is required, unlike the SQLite version which took an id alone and
        was documented "internal, post-authorisation use only". Under a
        user-partitioned table there is no id-only read to offer, which turns that
        comment into something the type system enforces.
        """
        owner_sub = self._owner(owner_sub)
        got = await self._call(
            self._table("runs").get_item,
            Key={"pk": _user_pk(owner_sub), "sk": _run_sk(run_id)},
        )
        item = got.get("Item")
        return _row(item, _RUN_FIELDS) if item else None

    async def get_run_by_ref(
        self, ref: str, *, owner_sub: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Resolve one of this owner's runs by id OR memorable name.

        Two reads at worst, and the id is tried first because that is what the SPA
        sends once a run is open. A name resolves through its marker item, so this
        needs no index and no scan.

        Returns None for "not found" *and* "not yours" — they are indistinguishable
        on purpose, and here that is free rather than deliberate: a ref outside the
        caller's partition simply is not there.
        """
        owner_sub = self._owner(owner_sub)
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
        self, q: Optional[str] = None, limit: int = 200, *, owner_sub: Optional[str] = None
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
        owner_sub = self._owner(owner_sub)
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
        self, run_id: str, *, owner_sub: Optional[str] = None
    ) -> Dict[str, Any]:
        """Turn count, total cost and last-event time, aggregated from the log.

        Recomputed from events rather than read off a counter on the run row. §4
        suggests keeping `total_cost_usd` there, and that is a fair optimisation
        later — but a maintained counter can drift from the log, and the log is the
        source of truth, so the derived-on-read version is the one that cannot be
        wrong. Bounded work: measured 80 events per run.
        """
        owner_sub = self._owner(owner_sub)
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
        self, run_id: str, *, owner_sub: Optional[str] = None
    ) -> Dict[str, Any]:
        """The full lineage forest rooted at this run's earliest ancestor.

        Returns `{root_id, nodes}` where each node carries the fields the tree view
        needs, including `config_json` so the caller can read `branch_mutation` for an
        edge label without a second round trip.
        """
        owner_sub = self._owner(owner_sub)
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
        self, run_id: str, *, owner_sub: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Runs forked directly from this one, newest first."""
        owner_sub = self._owner(owner_sub)
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
        owner_sub: Optional[str] = None,
    ) -> None:
        """Append one event.

        Conditional on the item not existing, which reproduces the SQLite
        `UNIQUE(run_id, turn, seq)` constraint. Without it a re-delivered write —
        a Step Functions retry, say — would silently overwrite a different event
        that happened to reuse the seq, and the log would be corrupt with no error.
        """
        owner_sub = self._owner(owner_sub)
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
        owner_sub: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """This run's events in a turn range, ordered by (turn, seq).

        Turn is not in the sort key — seq is — so the range is applied as an
        attribute filter over the run's contiguous items rather than through a second
        index. Key design §2: a GSI keyed on turn would add write cost to the
        hottest write path in the system to save filtering ~80 items.
        """
        owner_sub = self._owner(owner_sub)
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
        owner_sub: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Events strictly after a global seq — the polling and replay path.

        A native sort-key range read, which is the whole reason the sort key leads
        with the run id and ends in a zero-padded seq. `after_seq=-1` means "from the
        beginning", and it cannot be expressed as `sk > RUN#{id}#-000000000001`, so
        it becomes a prefix query instead of a range.
        """
        owner_sub = self._owner(owner_sub)
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

    async def last_event_turn(self, run_id: str, *, owner_sub: Optional[str] = None) -> int:
        """Highest turn in the log, or 0. Reads the last item, not all of them."""
        owner_sub = self._owner(owner_sub)
        items = await self._query_all(
            "events",
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": _user_pk(owner_sub),
                ":prefix": _run_prefix(run_id),
            },
        )
        return max((int(i["turn"]) for i in items), default=0)

    async def max_seq(self, run_id: str, *, owner_sub: Optional[str] = None) -> int:
        """Highest event seq, or -1 for a run with no events.

        A single backwards read: the sort key ends in a zero-padded seq, so the last
        item in key order IS the highest seq. That equivalence is the payoff for the
        padding, and it is why this is O(1) rather than a scan of the run.
        """
        owner_sub = self._owner(owner_sub)
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
        self, run_id: str, turn: int, *, owner_sub: Optional[str] = None
    ) -> int:
        """Delete events and snapshots past a turn; return the events removed.

        Used only when resuming an interrupted run in place, where the tail past the
        last checkpoint is a partial turn plus an interruption marker. Two queries
        and a batch delete, because DynamoDB has no `DELETE … WHERE`: the keys have
        to be read before they can be deleted.
        """
        owner_sub = self._owner(owner_sub)
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
        owner_sub: Optional[str] = None,
    ) -> int:
        """Copy a run's log up to a turn into another run, preserving (turn, seq).

        The branch primitive. Same owner for both by construction — a branch inherits
        its parent's owner (`branching.create_branch_run` reads it off the parent
        rather than accepting it), so one partition is read and written.
        """
        owner_sub = self._owner(owner_sub)
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
        self, snapshot: SimSnapshot, *, owner_sub: Optional[str] = None
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
        owner_sub = self._owner(owner_sub)
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
        self, run_id: str, turn: Optional[int] = None, *, owner_sub: Optional[str] = None
    ) -> Optional[SimSnapshot]:
        """A snapshot at a turn, or the latest. Pointer read, then one S3 GET."""
        owner_sub = self._owner(owner_sub)
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
        self, run_id: str, *, owner_sub: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Checkpoint turns for a run: `{turn, status, created_at}`, turn ascending.

        Reads only DynamoDB. `status` comes off the pointer rather than the body,
        which is what keeps this one query instead of one query plus N S3 GETs.
        """
        owner_sub = self._owner(owner_sub)
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
        self, run_id: str, *, owner_sub: Optional[str] = None
    ) -> Optional[int]:
        """Highest checkpoint turn, or None. One backwards read, as `get_snapshot`."""
        owner_sub = self._owner(owner_sub)
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
        owner_sub: Optional[str] = None,
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
        owner_sub = self._owner(owner_sub)
        from matrix_studio.documents import join_chunks

        from matrix_studio.documents import chunk_text

        doc_id = document_id or uuid.uuid4().hex[:12]
        key = self._document_key(owner_sub, run_id, doc_id)
        body = text if text is not None else join_chunks(list(chunks))
        await self._put_text(key, body)

        # `chunk_count` records the chunking OF THE STORED TEXT, not the length of the
        # list that was passed in. Those can differ, and when they do the recorded
        # number is the wrong one: retrieval re-chunks the stored body, so that is what
        # a passage's `ordinal` refers to and what the document-frequency ratio's
        # denominator has to match.
        #
        # Measured: three short hand-supplied chunks ("egress inspection", "egress
        # evidence", "unrelated text") reassemble into a body that re-chunks to ONE, so
        # a recorded 3 would make the df ratio's numerator and denominator disagree. On
        # the real ingest path the two always agree — the chunks came from `chunk_text`
        # of that same text — so this only bites hand-supplied chunks (the import
        # script, fixtures). Deriving it removes the class of mismatch rather than
        # relying on every caller being consistent.
        stored_chunks = len(chunk_text(body))

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
            "chunk_count": stored_chunks,
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
            self._ensure_clients()
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
        self, from_run_id: str, to_run_id: str, *, owner_sub: Optional[str] = None
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
        owner_sub = self._owner(owner_sub)
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
    # Retrieval: vectors in S3 Vectors, lexical in process
    # ------------------------------------------------------------------ #
    #
    # ONE SHARED INDEX, metadata-filtered — not one index per run.
    #
    # §8b recommends index-per-KB, and that reasoning is sound *for knowledge
    # bases*: the ceiling is 10,000 indexes per vector bucket, a company has few
    # KBs, and per-index IAM grants become possible. It does NOT transfer to
    # index-per-RUN, which is the only mapping available before Phase 6 introduces
    # KBs — that would cap the whole install at 10,000 conversations, and the stated
    # target is "something a large company installs". Ten thousand conversations is
    # a year for one team.
    #
    # So the interim is a single index filtered on `owner_sub`, `run_id` and
    # `persona_name`, whose ceiling is 2 billion vectors. The cost is that isolation
    # here is application-enforced rather than IAM-enforced, which is why the filter
    # is applied in ONE method (`_slice_filter`) that every read goes through, rather
    # than assembled at each call site. Phase 6 replaces this with per-KB indexes and
    # recovers the IAM boundary; nothing above this layer changes when it does.

    def _vector_index(self) -> str:
        return os.environ.get("VECTOR_INDEX", f"{self.table_prefix}-chunks")

    @staticmethod
    def _vector_key(document_id: str, ordinal: int) -> str:
        """S3 Vectors keys are strings, and (document, ordinal) is the natural one."""
        return f"{document_id}:{ordinal}"

    @staticmethod
    def chunk_id_for(document_id: str, ordinal: int) -> int:
        """A stable integer chunk id, because `RetrievedPassage.chunk_id` is an int.

        SQLite gave this for free as an `AUTOINCREMENT` rowid. There is no such thing
        here, and the type cannot simply become a string: `retrieval.py` does
        `int(row["chunk_id"])`, reciprocal-rank fusion keys on it, and the
        `document.retrieved` event records it for provenance.

        So it is derived — a 63-bit hash of `{document_id}:{ordinal}` — which gives
        the two properties that matter: **stable** (the lexical and vector arms
        compute the same id for the same chunk, which is what makes fusion by chunk
        id meaningful) and **derivable without a lookup**.

        The collision risk, stated rather than waved at: with 63 bits, a million
        chunks in one query's scope collide with probability ~5e-8, and a run holds a
        few hundred. A collision would fuse two passages in RRF — a degraded ranking,
        not a corrupted record, since the event log stores `document_id` and `ordinal`
        alongside and those are exact.
        """
        import hashlib

        digest = hashlib.sha256(f"{document_id}:{ordinal}".encode()).digest()
        return int.from_bytes(digest[:8], "big") >> 1

    def _slice_filter(
        self, run_id: str, persona_name: Optional[str], owner_sub: str
    ) -> Dict[str, Any]:
        """The metadata filter defining what a persona may retrieve.

        The single chokepoint for retrieval scoping, deliberately. With one shared
        index this filter *is* the isolation boundary, so it exists once rather than
        being spelled out at each call site — the failure mode being guarded against
        is a future read that forgets a clause.

        **Every object carries exactly ONE key.** That is a hard S3 Vectors rule, not
        a style preference, and the first version broke it: `{"owner_sub": …,
        "run_id": …}` is rejected with a bare `ValidationException: Invalid filter`,
        which names neither the offending object nor the rule. Determined empirically
        against the real service, since the error says nothing:

            {"a": 1, "b": 2}                        -> Invalid filter
            {"a": 1}                                -> ok
            {"$and": [{"a": 1}, {"b": 2}]}          -> ok
            {"$and": [{"a": 1, "b": 2}, {...}]}     -> Invalid filter
            {"$and": [{"a": 1}, {"$or": [...]}]}    -> ok

        So the conjunction is always explicit and always flat.

        `persona_name` absent means cast-wide. A cast-wide chunk carries no
        `persona_name` metadata at all — absent metadata cannot be matched by a filter
        — so it carries an explicit `cast_wide: True` instead, and the persona case is
        an `$or` over "mine" and "everyone's". That mirrors the SQL
        `(persona_name = ? OR persona_name IS NULL)` it replaces; dropping the second
        arm would make cast-wide documents retrievable by nobody, which is the same
        trap that made them invisible in `list_documents`.
        """
        clauses: List[Dict[str, Any]] = [
            {"owner_sub": owner_sub},
            {"run_id": run_id},
        ]
        if persona_name is not None:
            clauses.append(
                {"$or": [{"persona_name": persona_name}, {"cast_wide": True}]}
            )
        return {"$and": clauses}

    async def embedding_model(self) -> Optional[str]:
        """The model the stored vectors were produced with, if any.

        Recorded so a change of embedding model is detected rather than silently
        mixing dimensions — which would produce distances that are arithmetic
        nonsense. On S3 Vectors the index's dimension is fixed at creation, so a
        genuinely different width would be refused by the service; this catches the
        subtler case of a same-width model whose vectors are not comparable.
        """
        got = await self._call(
            self._table("documents").get_item,
            Key={"pk": "EMBEDDING", "sk": "META"},
        )
        item = got.get("Item")
        return str(item["model"]) if item and item.get("model") else None

    async def store_chunk_vectors(
        self,
        run_id: str,
        vectors: List[tuple],
        model: str,
        *,
        owner_sub: Optional[str] = None,
        chunks: Optional[Dict[int, Dict[str, Any]]] = None,
    ) -> int:
        """Store embeddings. `vectors` is `[(chunk_id, [floats]), ...]`.

        `chunks` maps chunk_id to `{document_id, ordinal, content, persona_name}` —
        required, because a vector is useless without the metadata that scopes and
        renders it, and the chunk_id alone cannot be reversed into a document.

        The passage text rides along as metadata, which is the point of §4a: one
        `QueryVectors` returns ids, distances *and* passages, so the per-turn hot path
        is a single round trip. `text` is declared non-filterable at index creation
        (filterable metadata has a ~2 KB per-vector budget and nothing filters on
        text), and that declaration is immutable — see the CDK stack.

        Batched at 500, which is the `PutVectors` limit.
        """
        owner_sub = self._owner(owner_sub)
        if not vectors:
            return 0
        chunks = chunks or {}
        client = self._vectors_client()
        bucket = os.environ.get("VECTOR_BUCKET", "")
        if not bucket:
            raise StorageError(
                "VECTOR_BUCKET is not set, so there is nowhere to store embeddings."
            )

        payload = []
        for chunk_id, vector in vectors:
            meta = chunks.get(int(chunk_id))
            if not meta:
                # Refusing rather than storing an unscoped vector. A vector with no
                # owner_sub would be returned to every tenant by a filtered query
                # that cannot exclude what it cannot see.
                raise StorageError(
                    f"no metadata for chunk {chunk_id}; a vector without owner_sub "
                    "and run_id cannot be scoped and must not be stored"
                )
            entry: Dict[str, Any] = {
                "owner_sub": owner_sub,
                "run_id": run_id,
                "document_id": str(meta["document_id"]),
                "ordinal": int(meta["ordinal"]),
                "text": str(meta.get("content") or ""),
            }
            persona = meta.get("persona_name")
            if persona:
                entry["persona_name"] = persona
            else:
                # An explicit flag, because "absent" cannot be matched by a filter.
                # This is the vector-store form of the same trap that made cast-wide
                # documents invisible in `list_documents`.
                entry["cast_wide"] = True
            payload.append(
                {
                    "key": self._vector_key(
                        str(meta["document_id"]), int(meta["ordinal"])
                    ),
                    "data": {"float32": [float(v) for v in vector]},
                    "metadata": entry,
                }
            )

        for start in range(0, len(payload), 500):
            await self._call(
                client.put_vectors,
                vectorBucketName=bucket,
                indexName=self._vector_index(),
                vectors=payload[start:start + 500],
            )

        await self._call(
            self._table("documents").put_item,
            Item=_to_ddb({"pk": "EMBEDDING", "sk": "META", "model": model,
                          "created_at": int(time.time())}),
        )
        return len(payload)

    async def vector_search(
        self,
        run_id: str,
        vector: List[float],
        persona_name: Optional[str] = None,
        k: int = 3,
        *,
        owner_sub: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """k-NN over the persona's slice. Returns the same shape the SQLite path did.

        `score` is the cosine DISTANCE, matching `sqlite-vec` — smaller is better —
        because `apply_similarity_floor` converts distance to cosine similarity and
        would be inverted by a similarity here.

        Passages come back inline via `returnMetadata`, so there is no second lookup:
        this is the round trip §4a's design saves.
        """
        owner_sub = self._owner(owner_sub)
        if not vector or k <= 0:
            return []
        bucket = os.environ.get("VECTOR_BUCKET", "")
        if not bucket:
            logger.warning("VECTOR_BUCKET is not set; vector search is unavailable.")
            return []
        try:
            result = await self._call(
                self._vectors_client().query_vectors,
                vectorBucketName=bucket,
                indexName=self._vector_index(),
                queryVector={"float32": [float(v) for v in vector]},
                topK=k,
                filter=self._slice_filter(run_id, persona_name, owner_sub),
                returnMetadata=True,
                returnDistance=True,
            )
        except Exception as exc:  # noqa: BLE001
            # Same contract as the SQLite path: a retrieval failure degrades to "no
            # supporting passage", which the prompt handles honestly, rather than
            # ending a run.
            logger.warning("Vector search failed for run %s: %s", run_id, exc)
            return []

        titles = {
            d["id"]: d.get("title") for d in await self.list_documents(run_id)
        }
        rows = []
        for hit in result.get("vectors") or []:
            meta = hit.get("metadata") or {}
            doc_id = str(meta.get("document_id") or "")
            ordinal = int(meta.get("ordinal") or 0)
            rows.append(
                {
                    "chunk_id": self.chunk_id_for(doc_id, ordinal),
                    "document_id": doc_id,
                    "ordinal": ordinal,
                    "content": str(meta.get("text") or ""),
                    "title": titles.get(doc_id) or doc_id,
                    "source_path": None,
                    "media_type": None,
                    "score": float(hit.get("distance") or 0.0),
                }
            )
        rows.sort(key=lambda r: r["score"])
        return rows[:k]

    async def count_chunk_vectors(self, run_id: str, *, owner_sub: Optional[str] = None) -> int:
        """How many of a run's chunks have a stored embedding."""
        owner_sub = self._owner(owner_sub)
        return len(await self._run_vector_keys(run_id, owner_sub))

    async def _run_vector_keys(self, run_id: str, owner_sub: str) -> set:
        """The vector keys already stored for a run.

        `ListVectors` has no filter parameter — unlike `QueryVectors` — so this pages
        the index and filters on returned metadata. Acceptable only because it is off
        the per-turn path: it serves the ingest and count endpoints. If the shared
        index grows large this becomes the reason to move to per-KB indexes early,
        rather than a thing to optimise here.
        """
        bucket = os.environ.get("VECTOR_BUCKET", "")
        if not bucket:
            return set()
        client = self._vectors_client()
        keys: set = set()
        token = None
        while True:
            params = {
                "vectorBucketName": bucket,
                "indexName": self._vector_index(),
                "returnMetadata": True,
                "maxResults": 500,
            }
            if token:
                params["nextToken"] = token
            try:
                result = await self._call(client.list_vectors, **params)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Listing vectors failed for run %s: %s", run_id, exc)
                return keys
            for entry in result.get("vectors") or []:
                meta = entry.get("metadata") or {}
                if meta.get("run_id") == run_id and meta.get("owner_sub") == owner_sub:
                    keys.add(entry["key"])
            token = result.get("nextToken")
            if not token:
                return keys

    async def chunks_missing_vectors(
        self, run_id: str, limit: Optional[int] = None, *, owner_sub: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """A run's chunks with no stored embedding, so ingest is resumable.

        Chunks are re-derived from the stored document text rather than read from a
        chunks table — there isn't one. That is safe *only* because the stored text is
        the original (see `add_document`'s `text` parameter and the §4a correction):
        re-chunking the original reproduces the chunks the vectors were built from,
        while re-chunking a `join_chunks` reassembly shifts 2–7% of boundaries.
        Documents written without `text` carry `text_is_original: False`, and their
        ordinals cannot be trusted to line up — logged rather than silently embedded
        against the wrong text.
        """
        owner_sub = self._owner(owner_sub)
        from matrix_studio.documents import chunk_text

        stored = await self._run_vector_keys(run_id, owner_sub)
        out: List[Dict[str, Any]] = []
        for doc in await self.list_documents(run_id):
            if doc.get("text_is_original") is False:
                logger.warning(
                    "Document %s was stored as a chunk reassembly, so re-chunking "
                    "may not reproduce the ordinals its passages were cited under. "
                    "Re-upload it to make retrieval provenance exact.",
                    doc["id"],
                )
            text = await self.document_text(str(doc["id"]))
            if not text:
                continue
            for chunk in chunk_text(text):
                key = self._vector_key(str(doc["id"]), chunk.ordinal)
                if key in stored:
                    continue
                out.append(
                    {
                        "chunk_id": self.chunk_id_for(str(doc["id"]), chunk.ordinal),
                        "content": chunk.content,
                        "document_id": str(doc["id"]),
                        "ordinal": chunk.ordinal,
                        "persona_name": doc.get("persona_name"),
                    }
                )
                if limit is not None and len(out) >= limit:
                    return out
        return out

    async def _run_chunks(
        self, run_id: str, persona_name: Optional[str] = None
    ) -> List[tuple]:
        """`[(chunk_id, content), ...]` for a persona's slice, from the stored text.

        One S3 GET per document (mean 11 KB, 1–5 documents), re-chunked in memory.
        This is what §4a's "chunk text does not belong in DynamoDB" costs on the
        lexical path, and it is cheap because the lexical path is an inspection
        endpoint rather than the per-turn one.
        """
        from matrix_studio.documents import chunk_text

        out: List[tuple] = []
        for doc in await self.list_documents(run_id, persona_name):
            text = await self.document_text(str(doc["id"]))
            if not text:
                continue
            for chunk in chunk_text(text):
                out.append(
                    (
                        self.chunk_id_for(str(doc["id"]), chunk.ordinal),
                        chunk.content,
                        str(doc["id"]),
                        chunk.ordinal,
                        doc.get("title"),
                        doc.get("source_path"),
                        doc.get("media_type"),
                    )
                )
        return out

    async def search_documents(
        self,
        run_id: str,
        query: str,
        persona_name: Optional[str] = None,
        k: int = 3,
        corpus: str = "run",
    ) -> List[Dict[str, Any]]:
        """BM25 over a persona's slice, best match first — in process, not FTS5.

        `corpus` is accepted and effectively ignored, and that is a simplification
        rather than a gap. It existed to distinguish statistics drawn from this run's
        slice from statistics drawn from the whole SQLite index — the v0.6 bug where
        `bm25()` computed over every run while `run_id` was only an outer filter, so a
        score depended on what else the database held. Here the index is *built from
        the run's chunks*, so run-scoping is structural: `"database"` cannot be
        implemented without deliberately reintroducing the contamination. Rejecting an
        unknown value is kept, since a typo should still fail loudly.

        Never raises: a search failure degrades to "no supporting passage found",
        which the prompt handles honestly, rather than ending a run.
        """
        if not query or k <= 0:
            return []
        if corpus not in ("run", "database"):
            raise ValueError(f"corpus must be 'run' or 'database', not {corpus!r}")
        try:
            from matrix_studio.storage.lexical import Bm25Index, extract_query_terms

            rows = await self._run_chunks(run_id, persona_name)
            index = Bm25Index([(r[0], r[1]) for r in rows])
            by_id = {r[0]: r for r in rows}
            hits = index.search(extract_query_terms(query), k)
            return [
                {
                    "chunk_id": chunk_id,
                    "document_id": by_id[chunk_id][2],
                    "ordinal": by_id[chunk_id][3],
                    "content": by_id[chunk_id][1],
                    "title": by_id[chunk_id][4],
                    "source_path": by_id[chunk_id][5],
                    "media_type": by_id[chunk_id][6],
                    "score": score,
                }
                for chunk_id, score in hits
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Document search failed for run %s: %s", run_id, exc)
            return []

    async def term_document_frequencies(
        self,
        run_id: str,
        terms: List[str],
        persona_name: Optional[str] = None,
    ) -> Dict[str, int]:
        """How many chunks in the slice contain each term.

        Feeds discriminative-term selection, which is default-off and was measured
        harmful — so this is kept for contract parity rather than because anything
        depends on it. Returning an empty dict on failure is what the SQLite version
        did, and the caller treats a missing count as "unknown" and falls back to using
        every candidate term.
        """
        if not terms:
            return {}
        try:
            from matrix_studio.storage.lexical import Bm25Index

            rows = await self._run_chunks(run_id, persona_name)
            index = Bm25Index([(r[0], r[1]) for r in rows])
            return index.document_frequencies(terms)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Term frequency probe failed for run %s: %s", run_id, exc)
            return {}

    async def chunk_count(
        self, run_id: str, persona_name: Optional[str] = None
    ) -> int:
        """Chunks in a persona's slice, read from the document metadata.

        `chunk_count` on the document row is what makes this a metadata read rather
        than a fetch-and-re-chunk of every document — one of the four jobs §4a says
        the row earns its place with.
        """
        docs = await self.list_documents(run_id, persona_name)
        return sum(int(d.get("chunk_count") or 0) for d in docs)

    async def reindex_documents(self) -> int:
        """No-op, and returning 0 would be a lie about what it did.

        In SQLite this rebuilt the FTS5 index from `doc_chunks` — a derived structure
        that could drift from its source. Here there is no derived structure: the
        vectors ARE the index, and rebuilding them means re-embedding, which
        `embed_pending_chunks` already does incrementally and idempotently via
        `chunks_missing_vectors`. The lexical arm is computed per query from the
        stored text and holds no state at all.

        So there is nothing to rebuild, and the honest return is the number of chunks
        that exist — which is what the endpoint reports and what the operator wants to
        see. Kept rather than removed because the route exists and its contract is
        "tell me the index is consistent"; the answer is now "it cannot be otherwise".
        """
        total = 0
        for run in await self._scan_all(
            "documents",
            FilterExpression="begins_with(sk, :prefix)",
            ExpressionAttributeValues={":prefix": "DOC#"},
        ):
            total += int(_from_ddb(run).get("chunk_count") or 0)
        return total

    def _vectors_client(self):
        if self._s3vectors is None:
            self._s3vectors = self._make("client", "s3vectors")
        return self._s3vectors

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
