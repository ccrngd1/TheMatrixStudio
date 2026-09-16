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

**Every check runs, including the ones that cost money.** `--paid` used to gate the two that
make billable Bedrock calls, and the default was to skip them — which meant the command people
actually typed verified the control plane and not the thing the product does. The turn loop is
the most valuable check in the suite and it was the one most often not run. ~$0.10 is cheaper
than shipping a broken engine, so it is no longer a decision.

To run a subset deliberately, name it: `--only kb-grants` takes any check by name, which is the
same escape hatch with an explicit choice attached rather than a silent default.

Usage:
    AWS_PROFILE=... python scripts/verify_deployment.py              # everything (~$0.10)
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

#: When `tenant-isolation` last passed, and against which deployment.
#:
#: Hand-maintained, which is a weakness worth naming: it is exactly as trustworthy as the last
#: person to run the check and edit this line. It exists because the alternative is worse — that
#: check can only run behind a deliberate, temporary widening of the tenant role's trust policy,
#: so it will never be green in a routine run, and a permanent amber with no date tells a reader
#: nothing about whether the boundary was ever tested or when.
#:
#: Update it in the same commit as the run. If you cannot say when it last passed, it has not.
TENANT_ISOLATION_LAST_PASSED = "2026-09-16 (10/10 checks, us-east-1, account 791580863750)"


class Check(NamedTuple):
    name: str
    script: str
    args: List[str]
    #: True when it makes billable Bedrock calls. Kept as a LABEL, not a gate: it is worth
    #: knowing which checks spend money, and it is printed in the summary. It used to mean
    #: "skipped unless asked for", which made the turn loop — the check that exercises the
    #: actual product — the one least often run.
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
            f"last passed {TENANT_ISOLATION_LAST_PASSED}\n"
            "      To run it again, the tenant role must temporarily trust a human "
            "principal:\n"
            "      cd infra && npx cdk deploy --require-approval never \\\n"
            "        -c verify_principal_arn=arn:aws:iam::$(aws sts get-caller-identity "
            "--query Account --output text):role/Admin\n"
            "      … run this check … then deploy again WITHOUT the flag to close it.\n"
            "      Step three is not cleanup: until it runs, anything that can assume that "
            "principal can reach every tenant's data."
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

    selected = [c for c in CHECKS if not args.only or c.name in args.only]
    billable = [c for c in selected if c.paid]

    print(f"Verifying the deployment — {len(selected)} check(s)"
          + (f", {len(billable)} of which make billable Bedrock calls (~$0.10)"
             if billable else "")
          + "\n")
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
        print(f"  {verdict:<11} {check.name:<18} {elapsed:>6.1f}s"
              + ("  (billable)" if check.paid else ""))

    failed = [n for n, (v, _, _) in results.items() if v in ("FAIL", "ERROR")]
    needs = [n for n, (v, _, _) in results.items() if v == "NEEDS-SETUP"]
    print("=" * 72)

    unrun = [c for c in CHECKS if c not in selected]
    if unrun:
        # Only reachable via `--only`, which is a deliberate narrowing. Still said out loud:
        # the contract at the top of this file is that a check which did not run is never
        # silently green.
        print(
            f"\n{len(unrun)} check(s) were not selected: "
            + ", ".join(c.name for c in unrun)
            + ". Drop --only to run the whole suite."
        )
    if needs:
        print(
            f"\n{len(needs)} check(s) need setup and did not run. Not a pass: the "
            "tenancy boundary is the one control that says a missing predicate cannot "
            "leak data, and it is unverified until this runs. The date above is the last "
            "recorded pass and is maintained by hand, so treat it as a note rather than "
            "as evidence about the deployment in front of you."
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
