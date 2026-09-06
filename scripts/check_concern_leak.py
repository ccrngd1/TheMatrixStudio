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

## Calibration — why plain overlap does not work

The first version scored plain content-word overlap against the concern and flagged
**5 candidates in a cognition-OFF run**, where a memory-mediated leak is impossible by
construction. Reading them, every one was vocabulary shared with the *topic*:
"customer", "demo", "five minutes", "deal", "cost meter".

The reason is structural. A persona's concern and its public *position* are about the
same subject — that is what makes it the concern behind that position — so they share
most of their vocabulary. Overlap with the concern therefore mostly measures "is this
persona talking about its own topic", which is every turn.

So the score uses only the concern's **private words**: content words that appear in
the concern and do NOT appear in anything the persona says publicly (its position,
prose, `optimises_for`, `persuaded_by`, `formed_by`). Those are the words that can only
have come from the withheld text.

## This is a PRE-FILTER, not a verdict

Calibrating against the cognition-off negative control a second time, the private-word
version still flagged **4 of 30 turns** — because words like "thing", "gets", "want",
"good" and "nobody" happen not to appear in a persona's public text and so count as
"private" while carrying no information.

The honest conclusion is that **word overlap is the wrong instrument for this
question.** Whether a persona volunteered its private motivation is a semantic
judgment, and this project has already learned that lesson once: the Phase 5
disclosure-compliance regex scored 0/3 for every wording, the LLM judge that replaced
it scored 100% for every wording, and the decision was ultimately made *by reading the
output* (`docs/PHASE5-RETRIEVAL-MEASUREMENT.md`).

So this script is a **cheap pre-filter with a known-high false-positive rate**. Its job
is to narrow ~30 turns and ~50 memories down to a handful worth reading. The verdict
comes from reading them. The memory set per run is small enough (tens of items) that
reading all of it is feasible, which is the actually-reliable method.

The cognition-off run remains the negative control: anything the filter flags there is
by construction a false positive, since the memory route does not exist.

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
    same too very just now also because while get got
    # Added after calibration: these appeared as "private" words purely by not
    # occurring in a persona's public text, while carrying no content. Every one
    # produced a false positive against the cognition-off control.
    thing things want wants wanted good bad better best ever never always cannot
    gets getting gone one two three lose losing lost win keep kept make makes made
    take takes took come comes came give gives gave say says said know knows knew
    think thinks thought need needs needed point points nobody somebody anybody
    everyone number numbers live lives actually before after really quite still
    even much many lot lots able unable real thats theres heres yeah okay right
    wrong sure certain clear plain simple hard easy new old first last next
    """.split()
)


def content_words(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z']+", text.lower()) if w not in STOP and len(w) > 2]


def private_overlap(private: set, candidate: str) -> float:
    """Fraction of a concern's PRIVATE words present in the candidate text.

    Returns 0.0 when a concern has too few private words to score — that means the
    concern is not meaningfully distinguishable from what the persona says publicly,
    which is an authoring problem in the cast, not a leak. Reported separately.
    """
    if len(private) < MIN_CONCERN_WORDS:
        return 0.0
    return len(private & set(content_words(candidate))) / len(private)


def load_concerns() -> Dict[str, List[Dict[str, Any]]]:
    """Per persona: each concern plus the PRIVATE words unique to it.

    Private words are the concern's content words minus every word the persona says
    publicly. See the module docstring: without this subtraction the score mostly
    measures topic vocabulary and flags a clean run.
    """
    spec = importlib.util.spec_from_file_location(
        "bva", ROOT / "scripts" / "build_validation_arms.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    out: Dict[str, List[Dict[str, Any]]] = {}
    for m in mod.CAST:
        prefs = m.get("preferences", {})
        public_parts = [m.get("prose", ""), m.get("role", "")]
        public_parts += prefs.get("optimises_for", []) + prefs.get("persuaded_by", [])
        public_parts += prefs.get("dismisses", [])
        for vp in m.get("viewpoints", []):
            public_parts.append(vp.get("position", ""))
            public_parts.append(vp.get("formed_by", ""))
            public_parts.extend(vp.get("evidence_that_shifts", []))
        public_words = set(content_words(" ".join(public_parts)))

        entries = []
        for vp in m.get("viewpoints", []):
            concern = vp.get("underlying_concern")
            if not concern:
                continue
            private = set(content_words(concern)) - public_words
            entries.append({"concern": concern, "private": private})
        if entries:
            out[m["name"]] = entries
    return out


def scan(
    result: Dict[str, Any],
    concerns: Dict[str, List[str]],
    threshold: float = OVERLAP_THRESHOLD,
) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []

    for msg in result.get("conversation", []):
        speaker, content = msg.get("speaker"), msg.get("content", "")
        for entry in concerns.get(speaker, []):
            score = private_overlap(entry["private"], content)
            if score >= threshold:
                findings.append({
                    "kind": "utterance", "turn": msg.get("turn"), "speaker": speaker,
                    "score": round(score, 2), "concern": entry["concern"], "text": content,
                })

    # Memories and reflections live on the agent state in the result payload.
    for name, agent in (result.get("agents") or {}).items():
        for item in agent.get("memory_stream", []) or []:
            content = item.get("content", "")
            tags = item.get("tags") or []
            for entry in concerns.get(name, []):
                score = private_overlap(entry["private"], content)
                if score >= threshold:
                    findings.append({
                        "kind": "reflection" if "reflection" in tags else "memory",
                        "turn": None, "speaker": name, "score": round(score, 2),
                        "concern": entry["concern"], "text": content,
                    })
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+", type=Path, help="run result JSON file(s)")
    ap.add_argument("--threshold", type=float, default=OVERLAP_THRESHOLD)
    args = ap.parse_args()

    concerns = load_concerns()
    total = 0
    scorable = sum(1 for v in concerns.values() for e in v if len(e["private"]) >= MIN_CONCERN_WORDS)
    total_concerns = sum(len(v) for v in concerns.values())
    print(f"Checking {len(args.results)} run(s) against {scorable} of {total_concerns} "
          f"authored concerns (private-word overlap >= {args.threshold})")
    if scorable < total_concerns:
        print(f"  note: {total_concerns - scorable} concern(s) have < {MIN_CONCERN_WORDS} "
              "words not already said publicly, so they cannot be scored — an authoring\n"
              "  property of the cast, not a leak.")
    print()

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
