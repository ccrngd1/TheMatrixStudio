# SPDX-License-Identifier: Apache-2.0
"""
Per-request credentials scoped to one tenant's partition.

This is the mechanism behind §3, which the architecture document calls its most
important recommendation, and it is worth being precise about what it buys. Every
other tenancy control in this codebase is a predicate in application code:
`get_run_by_ref(ref, owner_sub=...)`, the required keyword arguments, the 404s. Those
are good and they are tested — and they all share one failure mode, which is that a
route added next year forgets one.

These credentials remove that failure mode. The API Lambda reads `sub` from the
verified JWT, assumes a role with a **session policy** pinning
`dynamodb:LeadingKeys` to `USER#{sub}` and S3 object ARNs to `…/{sub}/*`, and does
all storage work with the result. A query that omits the tenant filter does not
return the wrong data — **DynamoDB refuses it.** The boundary moves from "every
developer remembers the predicate forever" to "the credentials cannot express the
wrong query".

**Effective permissions are the intersection** of the role's own policy and the
session policy passed here. That is why the role can be granted the whole table: the
session policy is what narrows it, and it is constructed per request from a claim the
caller cannot forge. Getting that backwards — a narrow role and a broad session
policy — would produce credentials that work in testing and enforce nothing.

`moto` does not evaluate IAM, so this cannot be unit-tested. It is proven against the
real account by `scripts/verify_tenant_isolation.py`, which is the only honest way to
assert "DynamoDB refuses it".
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# STS caps a session at one hour for a role chained from another role, and this is
# well inside that. Short because the credentials are cached for their lifetime and a
# leaked set should expire quickly; not shorter, because a re-assume per request would
# add a round trip to every call.
SESSION_SECONDS = 900

# STS rejects a session policy longer than this, outright, with a validation error at
# AssumeRole — i.e. on a user's request rather than at deploy time. Found by trying:
# the first version of `session_policy` was ~2,900 characters and could not be used at
# all. Asserted in code so it fails in a test instead.
MAX_SESSION_POLICY_CHARS = 2048

# STS rejects a RoleSessionName containing anything outside this set, and a Cognito
# `sub` is a UUID — safe today. Sanitised anyway because a federated identity's `sub`
# is whatever the IdP chose, and the failure would be an AssumeRole error on one
# user's first request, which is the worst possible time to discover it.
_SESSION_NAME_SAFE = re.compile(r"[^\w+=,.@-]")


def _session_name(owner_sub: str) -> str:
    """A RoleSessionName that STS will accept, and that identifies the tenant.

    The name appears in CloudTrail, so keeping the sub in it is what makes an audit
    trail attributable to a user rather than to "the API Lambda". Truncated to 64
    characters, which is the STS limit.
    """
    return f"tenant-{_SESSION_NAME_SAFE.sub('-', owner_sub)}"[:64]


def session_policy(
    owner_sub: str, *, table_arns: list, bucket_arn: str
) -> Dict[str, Any]:
    """The policy that narrows a session to one tenant.

    **Written to fit 2,048 characters, which is a hard STS limit on a session
    policy** and is not mentioned in the architecture document. The first version was
    ~2,900 characters — readable, with a `Sid` on each statement and every action
    spelled out — and `AssumeRole` rejected it outright with a validation error. So
    the compression below is a constraint, not a style choice:

      * ``dynamodb:*Item`` in place of seven action names. Every item-level action
        this application uses ends in `Item` — GetItem, BatchGetItem, PutItem,
        UpdateItem, DeleteItem, BatchWriteItem, ConditionCheckItem — so the wildcard
        is exactly equivalent and costs ~140 characters less. It does NOT widen the
        grant to anything the role does not already have, because effective
        permissions are the intersection of the two.
      * Region and account wildcarded in the table ARNs. The role is account-scoped
        already, and this saves ~25 characters on each of a dozen ARNs.
      * No ``Sid`` fields. They are documentation, and the documentation belongs here
        where there is room for it.

    ``assert`` on the length rather than hoping: an over-long policy fails at
    `AssumeRole`, i.e. on a user's request, and the error names a length rather than
    the statement that grew. Better to fail in a test.

    Three groups of permission, and the middle one is a gap worth seeing rather than
    an omission somebody has to notice:

    1. **Item access to the user-partitioned tables, conditioned on
       ``dynamodb:LeadingKeys``.** The load-bearing statement. It permits an
       operation only when the partition key equals ``USER#{sub}``, so a query that
       names another partition — or that names none, like a `Scan` — is refused by
       the service rather than filtered by us. `runs`, `events` and `snapshots` are
       keyed that way precisely so this condition can reach them (§4 records that
       run-keying was the earlier, weaker draft for exactly this reason).

    2. **Item access to the run-partitioned tables, UNCONDITIONED.** Summaries,
       threads and documents are keyed by run, because a shared knowledge base is
       read by principals who do not own it, so `LeadingKeys` cannot express their
       scope. Authorisation for these stays the application's ownership check on the
       run, and from Phase 6 the `kb_grants` check re-run at query time.

    3. **S3 objects under the tenant's prefix**, with ``ListBucket`` separated out.
       The two take different condition keys: object actions are scoped by the ARN,
       while `ListBucket` is a bucket-level action scoped by ``s3:prefix``.
       Conditioning `ListBucket` on the ARN instead — the natural mistake — grants a
       listing of the whole bucket, and the keys disclose every tenant's run and
       document ids.
    """
    user_scoped = sorted({_table_name(a) for a in table_arns
                          if _is_user_partitioned(a)})
    run_scoped = sorted({_table_name(a) for a in table_arns
                         if not _is_user_partitioned(a)})
    prefix = f"{owner_sub}/"

    def arns(names: list) -> list:
        # Region and account wildcarded: the role is already account-scoped, and this
        # is worth ~25 characters per ARN against a 2,048-character ceiling.
        out = []
        for name in names:
            out.append(f"arn:aws:dynamodb:*:*:table/{name}")
            out.append(f"arn:aws:dynamodb:*:*:table/{name}/index/*")
        return out

    statements: list = []
    if user_scoped:
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["dynamodb:*Item", "dynamodb:Query"],
                "Resource": arns(user_scoped),
                "Condition": {
                    "ForAllValues:StringEquals": {
                        "dynamodb:LeadingKeys": [f"USER#{owner_sub}"]
                    }
                },
            }
        )
    if run_scoped:
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["dynamodb:*Item", "dynamodb:Query"],
                "Resource": arns(run_scoped),
            }
        )
    statements.extend(
        [
            {
                "Effect": "Allow",
                "Action": "s3:*Object",
                # Every prefix this application writes carries the sub as its second
                # component: snapshots/{sub}/…, docs/{sub}/…, uploads/{sub}/…,
                # avatars/{sub}/…. One wildcard covers them all, and a prefix added
                # later is scoped by construction rather than by remembering.
                "Resource": f"{bucket_arn}/*/{prefix}*",
            },
            {
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": bucket_arn,
                "Condition": {"StringLike": {"s3:prefix": f"*/{prefix}*"}},
            },
        ]
    )
    policy = {"Version": "2012-10-17", "Statement": statements}

    encoded = json.dumps(policy, separators=(",", ":"))
    if len(encoded) > MAX_SESSION_POLICY_CHARS:
        raise ValueError(
            f"session policy is {len(encoded)} characters, over STS's "
            f"{MAX_SESSION_POLICY_CHARS} limit. AssumeRole would reject it on a "
            "user's request. Compress the actions or the ARNs — see this "
            "function's docstring."
        )
    return policy


def _table_name(table_arn: str) -> str:
    return table_arn.rsplit("/", 1)[-1]


#: Tables keyed `USER#{sub}` — the ones `dynamodb:LeadingKeys` can enforce.
USER_PARTITIONED = ("runs", "events", "snapshots")


def _is_user_partitioned(table_arn: str) -> bool:
    name = table_arn.rsplit("/", 1)[-1]
    return any(name.endswith(f"-{suffix}") for suffix in USER_PARTITIONED)


class TenantCredentials:
    """Caches one scoped session per tenant for its lifetime.

    Without the cache this is an `sts:AssumeRole` on every request, which is a round
    trip and a throttling surface on the hot path. With it, the cost is one call per
    user per 15 minutes.

    Cached in process memory, which is correct for both deployment shapes and for a
    reason worth stating: a Lambda sandbox serves one request at a time, so the cache
    holds one or two entries and cannot become a place where one tenant's credentials
    are handed to another. A shared cache across concurrent requests would be exactly
    that, which is why this is keyed on the sub and never consulted without one.
    """

    def __init__(
        self,
        *,
        role_arn: Optional[str] = None,
        table_arns: Optional[list] = None,
        bucket_arn: Optional[str] = None,
        region: Optional[str] = None,
    ) -> None:
        self.role_arn = role_arn or os.environ.get("TENANT_ROLE_ARN", "")
        self.table_arns = table_arns or []
        self.bucket_arn = bucket_arn or ""
        self.region = region or os.environ.get("AWS_REGION") or "us-east-1"
        self._cache: Dict[str, Tuple[Dict[str, str], float]] = {}
        self._sts = None

    @property
    def enabled(self) -> bool:
        """Whether scoped credentials are configured.

        When they are not — the local single-user server, or Phase 1's deployment —
        the storage layer uses the ambient role. That is a real reduction in
        guarantee, so it is a property callers can read and log rather than a silent
        difference in behaviour.
        """
        return bool(self.role_arn)

    async def for_tenant(self, owner_sub: str) -> Optional[Dict[str, str]]:
        """Credentials scoped to `owner_sub`, or None when not configured.

        Returns the shape boto3 wants (`aws_access_key_id` &c.) rather than STS's, so
        callers do not have to translate and cannot get the translation subtly wrong.
        """
        if not self.enabled:
            return None
        if not owner_sub:
            # Refusing rather than assuming a default: an empty sub reaching here
            # means the identity was lost upstream, and the safe response to "who is
            # this" being unanswerable is not "the local user".
            raise ValueError("cannot scope credentials without an owner_sub")

        cached = self._cache.get(owner_sub)
        # 60s of slack, so a request that starts just before expiry does not fail
        # part-way through with credentials that worked a moment earlier.
        if cached and cached[1] - 60 > time.time():
            return cached[0]

        import boto3

        if self._sts is None:
            self._sts = boto3.client("sts", region_name=self.region)

        import asyncio

        policy = session_policy(
            owner_sub, table_arns=self.table_arns, bucket_arn=self.bucket_arn
        )
        response = await asyncio.to_thread(
            lambda: self._sts.assume_role(
                RoleArn=self.role_arn,
                RoleSessionName=_session_name(owner_sub),
                Policy=json.dumps(policy),
                DurationSeconds=SESSION_SECONDS,
            )
        )
        creds = response["Credentials"]
        scoped = {
            "aws_access_key_id": creds["AccessKeyId"],
            "aws_secret_access_key": creds["SecretAccessKey"],
            "aws_session_token": creds["SessionToken"],
        }
        self._cache[owner_sub] = (
            scoped,
            time.time() + SESSION_SECONDS,
        )
        logger.debug("Assumed tenant-scoped credentials for %s", owner_sub)
        return scoped
