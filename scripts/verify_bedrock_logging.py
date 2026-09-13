#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
§7: Bedrock model-invocation logging — "which security review will ask for".

## Why this is a CHECK and not a CDK resource

Model invocation logging is configured by `PutModelInvocationLoggingConfiguration`,
which is **account-level and region-level**: one setting, one per region, shared by every
Bedrock caller in the account. It is not a per-application resource.

Account <account-id> already has it enabled in us-east-1, by something outside this
stack, and other workloads live in the same account. A `CustomResource` in this stack calling
`Put…` would **overwrite their configuration** on every deploy, and delete it on
`cdk destroy`. That is not a cap this stack gets to own, so it verifies instead.

## What a security review actually gets, and what it does not

Verified by controlled experiment 2026-09-13: a one-turn run produced

    17:59:01  global.anthropic.claude-haiku-4-5-20251001-v1:0  matrix-studio-turn
    17:59:11  global.anthropic.claude-haiku-4-5-20251001-v1:0  matrix-studio-finalise

so this application's invocations ARE logged, with prompts and completions, attributed
to the Lambda execution roles. §7's "auth by execution role, no keys anywhere" holds:
nothing in the deployment carries a long-lived Bedrock credential.

**What the Bedrock log cannot tell you is which USER's run made a call.** Every
invocation is `matrix-studio-turn`, because that is the AWS identity, and the end user's
`sub` is not part of a Bedrock request. Per-user attribution comes from the
application's own event log — `agent.response` carries `cost_usd` per turn, per run,
under `USER#{sub}` — and from the monthly counter §7's spend cap maintains. Worth
stating because a reviewer will reasonably assume the Bedrock log answers "who spent
this", and it does not.

Usage:
    AWS_PROFILE=... python scripts/verify_bedrock_logging.py

Exits non-zero if a region the application uses has no logging. A check that cannot be
performed is a failure, never a skip.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Optional

PASSED: List[str] = []
FAILED: List[str] = []
NOTES: List[str] = []

# The regions this application invokes Bedrock in, and why each one.
#
# us-west-2 is not an oversight in the architecture — §7 says "the avatar model
# (stability.sd3-5-large-v1:0) is already configured separately in us-west-2 from the
# text model. That split must be preserved, not flattened." So it is a region the
# application genuinely uses, and therefore one whose invocations a review will ask about.
REGIONS = {
    "us-east-1": "text models, embeddings, and every conversational turn",
    "us-west-2": "the avatar image model (stability.sd3-5-large-v1:0), pinned by §7",
}

# Retention below this reads as "we log invocations" without being able to answer a
# question asked a fortnight later. Not a hard failure — it is somebody else's log group
# — but reported, because 7 days is shorter than most review cycles.
MIN_USEFUL_RETENTION_DAYS = 30


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    (PASSED if ok else FAILED).append(label)
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))


def logging_config(region: str) -> Optional[Dict[str, Any]]:
    """The region's invocation logging config, or None when unconfigured."""
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("bedrock", region_name=region)
    try:
        return client.get_model_invocation_logging_configuration().get("loggingConfig")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("ResourceNotFoundException", "ValidationException"):
            return None
        raise


def retention_days(region: str, log_group: str) -> Optional[int]:
    import boto3

    logs = boto3.client("logs", region_name=region)
    for group in logs.describe_log_groups(logGroupNamePrefix=log_group).get(
        "logGroups", []
    ):
        if group.get("logGroupName") == log_group:
            # Absent means "never expire", which is the safe direction and reported as
            # such rather than as a missing value.
            return group.get("retentionInDays")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    print("Bedrock model-invocation logging (§7)\n")

    for region, why in REGIONS.items():
        print(f"{region} — {why}")
        config = logging_config(region)
        if config is None:
            check(
                f"{region}: invocation logging is enabled",
                False,
                "NOT CONFIGURED. Every Bedrock call this application makes in this "
                "region is unlogged.",
            )
            print()
            continue

        check(f"{region}: invocation logging is enabled", True)

        # Prompts and completions, not just the fact of a call. A review asking for
        # invocation logging is asking what was sent, and this flag is what decides it.
        check(
            f"{region}: prompts and completions are delivered",
            bool(config.get("textDataDeliveryEnabled")),
            "textDataDeliveryEnabled is off, so only metadata is captured"
            if not config.get("textDataDeliveryEnabled") else "",
        )
        if region == "us-west-2":
            check(
                f"{region}: generated IMAGES are delivered",
                bool(config.get("imageDataDeliveryEnabled")),
                "the avatar model is an image model, so this is the flag that matters "
                "here" if not config.get("imageDataDeliveryEnabled") else "",
            )

        cw = config.get("cloudWatchConfig") or {}
        s3 = config.get("s3Config") or {}
        destination = cw.get("logGroupName") or s3.get("bucketName")
        check(
            f"{region}: a destination is configured",
            bool(destination),
            str(destination),
        )

        if cw.get("logGroupName"):
            days = retention_days(region, cw["logGroupName"])
            if days is None:
                NOTES.append(
                    f"{region}: {cw['logGroupName']} never expires — good for review, "
                    "and a cost to watch."
                )
            elif days < MIN_USEFUL_RETENTION_DAYS:
                NOTES.append(
                    f"{region}: {cw['logGroupName']} keeps only {days} days. A question "
                    "asked a fortnight after the fact cannot be answered, and this log "
                    "group is owned OUTSIDE this stack — raise it with whoever owns the "
                    "account rather than changing it here."
                )
        print()

    if NOTES:
        print("Notes — not failures, but things a reviewer will ask about:")
        for note in NOTES:
            print(f"  · {note}")
        print()

    print(
        "Attribution, stated because it is the thing most likely to be assumed:\n"
        "  Bedrock logs the AWS identity — `matrix-studio-turn` — not the end user.\n"
        "  A user's `sub` is not part of a Bedrock request, so per-user attribution\n"
        "  comes from this application's event log (cost per turn per run under\n"
        "  USER#{sub}) and the monthly spend counter, NOT from these logs.\n"
    )

    print(f"{len(PASSED)}/{len(PASSED) + len(FAILED)} checks passed")
    if FAILED:
        for label in FAILED:
            print(f"  FAILED: {label}")
        print(
            "\nEnabling logging in a region turns it on for EVERY Bedrock caller in "
            "this account, including other teams' prompts and completions. That is a "
            "privacy decision rather than a technical one, so this script reports it "
            "and does not fix it."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
