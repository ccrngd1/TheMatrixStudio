#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Refuse a commit that would put operator-private material into this (public) repository.

The denylist is NOT in the repository — it is `private/sensitive-terms.txt`, gitignored, one
case-insensitive regex per line. A tracked list of the terms to keep out would itself publish them.
Without the file the check says so and passes, so a fresh clone can still commit.

    scripts/check_private_terms.py --staged        # pre-commit: added lines in the index
    scripts/check_private_terms.py --message FILE  # commit-msg: the message being written
    scripts/check_private_terms.py --all           # every tracked file, as a one-off audit

Install the hooks with `scripts/check_private_terms.py --install`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TERMS = ROOT / "private" / "sensitive-terms.txt"


def _patterns() -> list:
    if not TERMS.exists():
        print(f"check_private_terms: {TERMS.relative_to(ROOT)} not found; nothing checked.", file=sys.stderr)
        return []
    lines = [l.strip() for l in TERMS.read_text().splitlines()]
    return [re.compile(l, re.IGNORECASE) for l in lines if l and not l.startswith("#")]


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def _scan(label: str, text: str, pats: list) -> int:
    hits = 0
    for n, line in enumerate(text.splitlines(), 1):
        for p in pats:
            if p.search(line):
                # The pattern is reported by index, not text: the terminal output of a hook ends up
                # in CI logs and screenshots.
                print(f"  {label}:{n}: matches private term #{pats.index(p) + 1}", file=sys.stderr)
                hits += 1
                break
    return hits


def main(argv: list) -> int:
    if "--install" in argv:
        hooks = Path(_git("rev-parse", "--git-path", "hooks").strip())
        hooks = hooks if hooks.is_absolute() else ROOT / hooks
        for name, arg in (("pre-commit", "--staged"), ("commit-msg", '--message "$1"')):
            hook = hooks / name
            hook.write_text(f'#!/bin/sh\nexec python3 "$(git rev-parse --show-toplevel)/scripts/check_private_terms.py" {arg}\n')
            hook.chmod(0o755)
            print(f"installed {hook}")
        return 0
    pats = _patterns()
    if not pats:
        return 0
    hits = 0
    if "--staged" in argv:
        diff = _git("diff", "--cached", "-U0", "--no-color", "--diff-filter=ACMR")
        current = "?"
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                current = line[6:]
            elif line.startswith("+") and not line.startswith("+++"):
                hits += _scan(current, line[1:], pats)
    elif "--message" in argv:
        hits += _scan("commit message", Path(argv[argv.index("--message") + 1]).read_text(), pats)
    elif "--all" in argv:
        for f in _git("ls-files").split("\n"):
            path = ROOT / f
            if f and path.is_file():
                try:
                    hits += _scan(f, path.read_text(encoding="utf-8"), pats)
                except UnicodeDecodeError:
                    pass
    else:
        print(__doc__, file=sys.stderr)
        return 2
    if hits:
        print(f"check_private_terms: {hits} line(s) match private terms; not committing.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
