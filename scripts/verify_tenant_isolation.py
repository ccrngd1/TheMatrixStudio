#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Prove §3 against the real account: scoped credentials REFUSE a cross-partition read.

`moto` does not evaluate IAM, so no unit test can establish this. Every other tenancy
control in the codebase is a predicate in application code and is unit-tested; this is
the one that says a *missing* predicate cannot leak data, and the only way to know that
is to try it against a real DynamoDB and watch it fail.

Usage:
    AWS_PROFILE=... python scripts/verify_tenant_isolation.py --stack matrix-studio-stack

Exits non-zero if any check fails. A check that cannot be performed — a missing
output, no credentials — is also a failure, never a skip: a verification script that
quietly does nothing is worse than one that is not run, because it reports success.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from matrix_studio.storage.credentials import session_policy  # noqa: E402

USER_A = "verify-tenant-aaaa"
USER_B = "verify-tenant-bbbb"


def outputs(stack: str, region: str) -> Dict[str, str]:
    import boto3

    cfn = boto3.client("cloudformation", region_name=region)
    stacks = cfn.describe_stacks(StackName=stack)["Stacks"]
    return {
        o["OutputKey"]: o["OutputValue"]
        for o in stacks[0].get("Outputs", [])
    }


def assume(
    role_arn: str, owner_sub: str, table_arns: List[str], bucket_arn: str,
    region: str,
):
    """Credentials scoped to one tenant.

    The region is threaded explicitly rather than inherited. A `boto3.Session`
    constructed from raw credentials does NOT pick up the ambient region, so every
    client built from it fails with `NoRegionError` — which reads like a missing
    environment variable rather than what it is.
    """
    import boto3

    policy = session_policy(
        owner_sub, table_arns=table_arns, bucket_arn=bucket_arn
    )
    creds = boto3.client("sts", region_name=region).assume_role(
        RoleArn=role_arn,
        RoleSessionName=f"verify-{owner_sub}"[:64],
        Policy=json.dumps(policy),
        DurationSeconds=900,
    )["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )


def is_access_denied(exc: Exception) -> bool:
    text = str(exc)
    return "AccessDenied" in text or "not authorized" in text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", default="matrix-studio-stack")
    parser.add_argument("--prefix", default="matrix-studio")
    parser.add_argument("--region", default=None)
    args = parser.parse_args()

    import os

    import boto3

    region = (
        args.region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or boto3.Session().region_name
    )
    if not region:
        print("FAIL: no region. Pass --region or set AWS_REGION.")
        return 1

    out = outputs(args.stack, region)
    role_arn = out.get("TenantRoleArn")
    bucket = out.get("DataBucketName")
    if not role_arn or not bucket:
        print("FAIL: the stack has no TenantRoleArn/DataBucketName output. "
              "Deploy the current stack first.")
        return 1

    identity = boto3.client("sts", region_name=region).get_caller_identity()
    account = identity["Account"]
    table_arns = [
        f"arn:aws:dynamodb:{region}:{account}:table/{args.prefix}-{t}"
        for t in ("runs", "events", "snapshots", "summaries", "threads",
                  "thread-messages", "documents")
    ]
    bucket_arn = f"arn:aws:s3:::{bucket}"

    print(f"account {account} · region {region}")
    print(f"tenant role: {role_arn}\n")

    failures: List[str] = []

    def check(name: str, ok: bool, detail: str = "", hint: str = "") -> None:
        """`detail` is context shown either way; `hint` explains a FAILURE only.

        Kept separate because the first version printed a failure explanation next to
        a PASS — "PASS  a persona sees cast-wide passages — the cast_wide arm of the
        filter is not working" — which reads as a contradiction and is exactly the
        wrong thing for a verification script to be ambiguous about.
        """
        suffix = f" — {detail}" if detail else ""
        if not ok and hint:
            suffix += f" — {hint}"
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{suffix}")
        if not ok:
            failures.append(name)

    session_a = assume(role_arn, USER_A, table_arns, bucket_arn, region)
    session_b = assume(role_arn, USER_B, table_arns, bucket_arn, region)
    runs_a = session_a.resource("dynamodb").Table(f"{args.prefix}-runs")
    runs_b = session_b.resource("dynamodb").Table(f"{args.prefix}-runs")

    print("DynamoDB — dynamodb:LeadingKeys")

    # 1. A writes into its own partition. If this fails the rest proves nothing,
    #    because a role that can do nothing trivially cannot cross a boundary.
    try:
        runs_a.put_item(Item={"pk": f"USER#{USER_A}", "sk": "RUN#verify",
                              "id": "verify", "topic": "isolation check"})
        check("A can write its own partition", True)
    except Exception as exc:  # noqa: BLE001
        check("A can write its own partition", False, str(exc)[:120])
        print("\nThe positive case failed, so the negative cases below would be "
              "vacuous. Fix this first.")
        return 1

    # 2. A reads its own partition.
    try:
        got = runs_a.get_item(Key={"pk": f"USER#{USER_A}", "sk": "RUN#verify"})
        check("A can read its own partition", "Item" in got)
    except Exception as exc:  # noqa: BLE001
        check("A can read its own partition", False, str(exc)[:120])

    # 3. THE CHECK. B asks for A's partition by name — the shape a missing tenant
    #    filter in application code produces.
    try:
        runs_b.get_item(Key={"pk": f"USER#{USER_A}", "sk": "RUN#verify"})
        check("B is REFUSED a GetItem on A's partition", False,
              hint="the read succeeded — isolation is NOT enforced")
    except Exception as exc:  # noqa: BLE001
        check("B is REFUSED a GetItem on A's partition", is_access_denied(exc),
              type(exc).__name__)

    # 4. The same via Query, which is what `list_runs` and `get_events` issue.
    try:
        runs_b.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": f"USER#{USER_A}"},
        )
        check("B is REFUSED a Query on A's partition", False,
              hint="the query succeeded — isolation is NOT enforced")
    except Exception as exc:  # noqa: BLE001
        check("B is REFUSED a Query on A's partition", is_access_denied(exc),
              type(exc).__name__)

    # 5. A Scan, which names no partition at all. This is the case `LeadingKeys`
    #    exists for: an unscoped read cannot satisfy a condition on the leading key,
    #    so it is refused rather than returning every tenant's rows.
    try:
        runs_b.scan(Limit=1)
        check("B is REFUSED a Scan (no partition named)", False,
              hint="the scan succeeded — an unscoped read returns other tenants' data")
    except Exception as exc:  # noqa: BLE001
        check("B is REFUSED a Scan (no partition named)", is_access_denied(exc),
              type(exc).__name__)

    # 6. And B may not WRITE into A's partition, which would be worse than reading.
    try:
        runs_b.put_item(Item={"pk": f"USER#{USER_A}", "sk": "RUN#injected",
                              "id": "injected"})
        check("B is REFUSED a write into A's partition", False,
              hint="the write succeeded — one tenant can inject runs into another")
    except Exception as exc:  # noqa: BLE001
        check("B is REFUSED a write into A's partition", is_access_denied(exc),
              type(exc).__name__)

    print("\nS3 — per-user prefixes")
    s3_a = session_a.client("s3")
    s3_b = session_b.client("s3")
    key_a = f"snapshots/{USER_A}/verify/000001.json"

    try:
        s3_a.put_object(Bucket=bucket, Key=key_a, Body=b"{}")
        check("A can write under its own prefix", True)
    except Exception as exc:  # noqa: BLE001
        check("A can write under its own prefix", False, str(exc)[:140])

    try:
        s3_b.get_object(Bucket=bucket, Key=key_a)
        check("B is REFUSED A's object", False, "the read succeeded")
    except Exception as exc:  # noqa: BLE001
        check("B is REFUSED A's object", is_access_denied(exc), type(exc).__name__)

    # `ListBucket` is a BUCKET-level action scoped by `s3:prefix`, not by the object
    # ARN. Conditioning it on the ARN — the natural mistake — grants a listing of the
    # whole bucket, which discloses every tenant's run and document ids in the keys.
    try:
        s3_b.list_objects_v2(Bucket=bucket, Prefix=f"snapshots/{USER_A}/")
        check("B is REFUSED a listing of A's prefix", False,
              hint="the listing succeeded — keys disclose other tenants' run ids")
    except Exception as exc:  # noqa: BLE001
        check("B is REFUSED a listing of A's prefix", is_access_denied(exc),
              type(exc).__name__)

    try:
        s3_b.list_objects_v2(Bucket=bucket)
        check("B is REFUSED an unprefixed listing", False,
              hint="the listing succeeded — the whole bucket is visible")
    except Exception as exc:  # noqa: BLE001
        check("B is REFUSED an unprefixed listing", is_access_denied(exc),
              type(exc).__name__)

    # Clean up what this script created, with A's own credentials.
    for cleanup in (
        lambda: runs_a.delete_item(
            Key={"pk": f"USER#{USER_A}", "sk": "RUN#verify"}),
        lambda: s3_a.delete_object(Bucket=bucket, Key=key_a),
    ):
        try:
            cleanup()
        except Exception as exc:  # noqa: BLE001
            print(f"  note: cleanup failed ({exc})", file=sys.stderr)

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures)}")
        print("Tenant isolation is not enforced as §3 claims. The application-level "
              "checks may still be correct — but the guarantee that a MISSING one "
              "cannot leak data does not currently hold.")
        return 1
    print("All checks passed: scoped credentials refuse cross-tenant access, so a "
          "missing tenant filter in application code cannot leak data (§3).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
