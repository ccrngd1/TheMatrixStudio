#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Every deployment verification, in one command, with one scoreboard.

## Why this exists

On 2026-09-13 a single session found six things wrong with the deployed stack that
**1,113 local tests and 15/15 live grant checks all passed through**:

  - every knowledge-base route returned 500 (`dynamodb:Scan` is not granted, on purpose)
  - a KB shared with a group appeared in nobody's listing
  - nothing in the stack could create a KB's vector index
  - us-west-2 Bedrock invocations were unlogged
  - the log-delivery role held `AdministratorAccess`
  - §3's tenancy boundary had never actually been tested against IAM

Every one was found by running something against the deployment. None could have been
found locally: `moto` does not evaluate IAM, and the checks that do exist were five
separate scripts nobody ran together. `BACKLOG.md` had already recorded that as a risk —
"a regression in the deployed turn loop is found when somebody thinks to look" — and this
is the cheap half of the fix. The expensive half is running it on a schedule.

## Why not CI, yet

The generation checks cost real Bedrock money (~$0.10 for the full sweep), so a
per-commit hook is the wrong shape. And CI would need an OIDC role and credential
plumbing that does not exist. One command that a human runs before a release is worth
more than a pipeline nobody has built, and it is the thing a pipeline would call anyway.

## The contract

**A check that cannot run is a FAILURE, not a skip.** A verification suite that quietly
does nothing is worse than one nobody runs, because it reports success. The one exception
is declared explicitly: `verify_tenant_isolation.py` requires the tenant role to trust a
human principal, which is a deliberate, temporary widening — so it reports NEEDS-SETUP
with the command, and that state is loud rather than green.

Usage:
    AWS_PROFILE=... python scripts/verify_deployment.py              # free checks only
    AWS_PROFILE=... python scripts/verify_deployment.py --paid       # + generation (~$0.10)
    AWS_PROFILE=... python scripts/verify_deployment.py --only kb-grants
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

ROOT = Path(__file__).resolve().parents[1]


class Check(NamedTuple):
    name: str
    script: str
    args: List[str]
    #: True when it makes billable Bedrock calls. Off by default, because a verification
    #: run that silently costs money is one people stop doing.
    paid: bool
    what: str
    #: Set when the check needs infrastructure a normal deployment does not have. It is
    #: reported as NEEDS-SETUP rather than PASS or FAIL, with the command to fix it —
    #: pretending it passed would be the worst of the three.
    needs_setup: Optional[str] = None


CHECKS: List[Check] = [
    Check(
        "bedrock-logging", "verify_bedrock_logging.py", [], False,
        "§7: invocation logging in every region the app uses, and the delivery role's "
        "privilege",
    ),
    Check(
        "kb-grants", "verify_kb_grants.py", [], False,
        "§8b: a binding is not permission, and a revoked grant stops working at query "
        "time",
    ),
    Check(
        "vector-retrieval", "verify_vector_retrieval.py", [], False,
        "§8a: S3 Vectors k-NN against the real service, which moto cannot do",
    ),
    Check(
        "tenant-isolation", "verify_tenant_isolation.py",
        ["--stack", "matrix-studio-stack"], False,
        "§3: scoped credentials REFUSE cross-tenant access — the one control that says a "
        "missing predicate cannot leak data",
        needs_setup=(
            "the tenant role must temporarily trust a human principal:\n"
            "      cd infra && npx cdk deploy --require-approval never \\\n"
            "        -c verify_principal_arn=arn:aws:iam::$(aws sts get-caller-identity "
            "--query Account --output text):role/Admin\n"
            "      … run this check … then deploy again WITHOUT the flag to close it"
        ),
    ),
    Check(
        "turn-loop", "verify_turn_loop.py", ["--turns", "6"], True,
        "§5: completion, stop, cost cap, branch and resume against the real state machine",
    ),
    Check(
        "kb-retrieval", "verify_kb_retrieval.py", [], True,
        "§8b end to end through the HTTP API with a real token: a collection is created, "
        "a document becomes retrievable, and a persona quotes it",
    ),
]


def env_ready() -> List[str]:
    """Environment the scripts need. Missing values are a failure, not a prompt."""
    missing = [
        name for name in ("TABLE_PREFIX", "DATA_BUCKET", "VECTOR_BUCKET")
        if not os.environ.get(name)
    ]
    return missing


def run(check: Check, timeout: int) -> tuple[str, float, str]:
    """`(verdict, seconds, tail)` — verdict is PASS, FAIL, NEEDS-SETUP or ERROR."""
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / check.script), *check.args],
            capture_output=True, text=True, timeout=timeout, cwd=ROOT,
        )
    except subprocess.TimeoutExpired:
        return "ERROR", time.monotonic() - started, f"timed out after {timeout}s"

    elapsed = time.monotonic() - started
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0:
        return "PASS", elapsed, ""

    # A tenancy check that cannot assume the role is a setup problem, not a broken
    # boundary — and conflating the two would send somebody hunting a leak that is not
    # there. Distinguished by the error AWS actually returns.
    if check.needs_setup and (
        "not authorized to perform: sts:AssumeRole" in output
        or "AccessDenied" in output and "assume_role" in output.lower()
    ):
        return "NEEDS-SETUP", elapsed, ""

    # Prefer the lines that name a failure. A sub-script's prose — the attribution
    # paragraph in `verify_bedrock_logging.py`, say — is written for someone reading that
    # script's own output, and reproducing it here buries the scoreboard it belongs under.
    lines = [ln.rstrip() for ln in output.strip().splitlines() if ln.strip()]
    named = [ln for ln in lines if "✗" in ln or "FAILED" in ln or "Error" in ln]
    tail = "\n".join(named or lines[-6:])
    return "FAIL", elapsed, tail[-1200:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paid", action="store_true",
        help="also run checks that make billable Bedrock calls (~$0.10)",
    )
    parser.add_argument("--only", action="append", help="run only these checks, by name")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    missing = env_ready()
    if missing:
        print(
            "Refusing to run: " + ", ".join(missing) + " not set.\n"
            "These come from the stack outputs. A verification run against the wrong "
            "account, or against nothing, is worse than no run at all.",
            file=sys.stderr,
        )
        return 2

    selected = [
        c for c in CHECKS
        if (not args.only or c.name in args.only) and (c.paid <= args.paid)
    ]
    skipped_paid = [c for c in CHECKS if c.paid and not args.paid and (
        not args.only or c.name in args.only
    )]

    print(f"Verifying the deployment — {len(selected)} check(s)\n")
    results: Dict[str, tuple[str, float, str]] = {}
    for check in selected:
        print(f"▶ {check.name}: {check.what}")
        verdict, elapsed, tail = run(check, args.timeout)
        results[check.name] = (verdict, elapsed, tail)
        mark = {"PASS": "✓", "FAIL": "✗", "NEEDS-SETUP": "!", "ERROR": "✗"}[verdict]
        print(f"  {mark} {verdict} ({elapsed:.1f}s)")
        if verdict == "NEEDS-SETUP":
            print(f"    {check.needs_setup}")
        elif tail:
            for line in tail.splitlines()[-8:]:
                print(f"    {line}")
        print()

    print("=" * 72)
    for check in selected:
        verdict, elapsed, _ = results[check.name]
        print(f"  {verdict:<11} {check.name:<18} {elapsed:>6.1f}s")
    for check in skipped_paid:
        print(f"  {'NOT RUN':<11} {check.name:<18} {'—':>7}  (needs --paid)")

    failed = [n for n, (v, _, _) in results.items() if v in ("FAIL", "ERROR")]
    needs = [n for n, (v, _, _) in results.items() if v == "NEEDS-SETUP"]
    print("=" * 72)

    if skipped_paid:
        print(
            f"\n{len(skipped_paid)} generation check(s) were NOT RUN. They cost ~$0.10 and "
            "cover the turn loop — completion, stop, the cost cap, branch and resume. "
            "Run with --paid before a release."
        )
    if needs:
        print(
            f"\n{len(needs)} check(s) need setup and did not run. Not a pass: the "
            "tenancy boundary is the one control that says a missing predicate cannot "
            "leak data, and it is unverified until this runs."
        )
    if failed:
        print(f"\nFAILED: {', '.join(failed)}")
        return 1
    if needs:
        # Non-zero, deliberately. A green exit code with an unverified tenancy boundary is
        # exactly the "quietly does nothing" failure this script exists to prevent.
        return 3
    print("\nAll checks that ran, passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
