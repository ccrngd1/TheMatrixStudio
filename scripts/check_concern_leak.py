#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Did a persona's withheld ``underlying_concern`` reach the transcript?

## Why this exists as a separate instrument

``underlying_concern`` is withheld by the *code path*: it reaches only its owner's
system prompt, never the moderator's cast list, never the event log, never the
dossier. ``tests/test_personas.py`` and ``tests/test_structured_persona_engine.py``
lock all of those.

Every one of those checks inspects **prompt construction**. None of them can see the
route this script exists to test:

    with cognition on, a persona forms memories about its own reasoning, and 5 of
    them are injected into each of its later prompts. If a formed memory encodes the
    concern, the memory competes with the "do not volunteer this" instruction — and
    if the memory wins, the persona says out loud the thing the design exists to keep
    unsaid.

That is a correctness defect, not a quality metric, and it would be invisible to the
existing tests because nothing was rendered wrongly. The leak would be *generated*.

## Method

Three passes, because a verbatim search alone would miss a paraphrase:

1. **Utterance match** — n-gram overlap between each concern and each turn, which
   catches restatement as well as quotation.
2. **Memory match** — the same over the memories a persona actually formed. This
   catches the *route* even when no utterance leaked yet, which is the early warning.
3. **Reflection match** — reflections separately, since they are condensed beliefs
   carried at importance 0.9 and are the most likely place for a concern to end up.

Scored by content-word overlap rather than substring, with the threshold deliberately
LOW. A false positive costs one manual read; a false negative means shipping a leak.

Exit status is 1 if anything is flagged, so this can gate a run.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent

# Overlap fraction of the concern's content words that must appear together in a
# candidate text before it is flagged. 0.5 is low on purpose — see the docstring.
OVERLAP_THRESHOLD = 0.5
# Concerns shorter than this many content words are skipped: overlap is meaningless
# on a handful of words and would flag everything.
MIN_CONCERN_WORDS = 5

STOP = set(
    """a an the and or but if then than that this these those to of in on at for with
    is are was was were be been being do does did have has had having i you he she it
    we they me him her us them my your his its our their as by from about into over
    under so not no nor can could should would will shall may might must what which
    who when where why how all any both each few more most other some such only own
    same too very just now also because while get got""".split()
)


def content_words(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z']+", text.lower()) if w not in STOP and len(w) > 2]


def overlap(concern: str, candidate: str) -> float:
    """Fraction of the concern's distinct content words present in the candidate."""
    cw = set(content_words(concern))
    if len(cw) < MIN_CONCERN_WORDS:
        return 0.0
    return len(cw & set(content_words(candidate))) / len(cw)


def load_concerns() -> Dict[str, List[str]]:
    """Authored concerns per persona, straight from the arm generator."""
    spec = importlib.util.spec_from_file_location(
        "bva", ROOT / "scripts" / "build_validation_arms.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {
        m["name"]: [
            vp["underlying_concern"] for vp in m.get("viewpoints", [])
            if vp.get("underlying_concern")
        ]
        for m in mod.CAST
    }


def scan(
    result: Dict[str, Any],
    concerns: Dict[str, List[str]],
    threshold: float = OVERLAP_THRESHOLD,
) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []

    for msg in result.get("conversation", []):
        speaker, content = msg.get("speaker"), msg.get("content", "")
        for concern in concerns.get(speaker, []):
            score = overlap(concern, content)
            if score >= threshold:
                findings.append({
                    "kind": "utterance", "turn": msg.get("turn"), "speaker": speaker,
                    "score": round(score, 2), "concern": concern, "text": content,
                })

    # Memories and reflections live on the agent state in the result payload.
    for name, agent in (result.get("agents") or {}).items():
        for item in agent.get("memory_stream", []) or []:
            content = item.get("content", "")
            tags = item.get("tags") or []
            for concern in concerns.get(name, []):
                score = overlap(concern, content)
                if score >= threshold:
                    findings.append({
                        "kind": "reflection" if "reflection" in tags else "memory",
                        "turn": None, "speaker": name, "score": round(score, 2),
                        "concern": concern, "text": content,
                    })
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+", type=Path, help="run result JSON file(s)")
    ap.add_argument("--threshold", type=float, default=OVERLAP_THRESHOLD)
    args = ap.parse_args()

    concerns = load_concerns()
    total = 0
    print(f"Checking {len(args.results)} run(s) against {sum(len(v) for v in concerns.values())} "
          f"authored concerns (overlap >= {args.threshold})\n")

    for path in args.results:
        if not path.exists():
            print(f"  {path.name}: MISSING", file=sys.stderr)
            continue
        findings = scan(json.loads(path.read_text()), concerns, args.threshold)
        total += len(findings)
        if not findings:
            print(f"  {path.name}: clean")
            continue
        print(f"  {path.name}: {len(findings)} FLAGGED")
        for f in findings:
            where = f"turn {f['turn']}" if f["turn"] is not None else f["kind"]
            print(f"    [{f['kind']}] {f['speaker']} ({where}, overlap {f['score']})")
            print(f"      concern: {f['concern'][:110]}")
            print(f"      text:    {f['text'][:220]}")

    print(f"\n{'LEAK CANDIDATES: ' + str(total) if total else 'NO LEAK CANDIDATES'}")
    if total:
        print(
            "\nThese are CANDIDATES, not confirmed leaks — the threshold is set low on\n"
            "purpose. Read each one. A memory or reflection hit with no utterance hit\n"
            "is the early warning: the concern is in the persona's own context but has\n"
            "not been said out loud yet."
        )
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
