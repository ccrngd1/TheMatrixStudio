#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Score the Phase 5 premise-validation arms against each other.

Answers one question: does structured persona data — and then source grounding
on top of it — produce genuinely more divergent, more specific stakeholder
positions than the prose personas the engine ships with today?

Two tiers of measurement, deliberately separated because they carry different
weight:

DETERMINISTIC (primary). Computed from the transcript with no model in the loop,
so it is reproducible and cannot flatter a preferred outcome. Cross-speaker
similarity is the headline number: if structure works, the personas' language
should overlap LESS, because they are arguing from different premises.

BLIND JUDGE (secondary). Some questions genuinely need reading comprehension —
"how many distinct substantive positions are there" — so one judge call per arm
handles those. The judge is given arms under shuffled anonymous labels with no
indication of which is the control or what the hypothesis is, because a judge
told which arm is "the new one" will find it better.

Usage:
    scripts/score_validation.py /tmp/mss_val            # deterministic only
    scripts/score_validation.py /tmp/mss_val --judge    # + blind LLM judge

Arm D (Phase 6) is scored when present and skipped when not, so the original
three-arm comparison remains reproducible on its own.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List, Tuple

ARM_FILES = {
    "A_control": "arm-a-control.json",
    "B_structured": "arm-b-structured.json",
    "C_grounded": "arm-c-grounded.json",
    # Phase 6: Arm B's content as structured DATA + the re-tuned dismissal rule.
    # Optional so the original three-arm comparison still scores on its own.
    "D_shipped": "arm-d-shipped.json",
}

# Arms whose absence is not an error (added after the original experiment ran).
OPTIONAL_ARMS = {"D_shipped"}

# Phrases that concede ground or align the speaker with someone else. High rates
# mean the cast is harmonising, which is the failure mode under investigation.
ACCOMMODATION = [
    r"\byou'?re right\b",
    r"\b\w+ is right\b",
    r"\bi agree\b",
    r"\bi hear (you|both|all)\b",
    r"\bi appreciate\b",
    r"\bfair (point|enough)\b",
    r"\bgood point\b",
    r"\bthat'?s fair\b",
    r"\bi'?m not going to argue\b",
    r"\bi don'?t disagree\b",
    r"\byou'?re not wrong\b",
]

# Phrases where a speaker explicitly refuses to weigh something. This is the
# behaviour the `dismisses` field is supposed to produce; the control arm has no
# dismissals authored, so a gap here is direct evidence the field did something.
DISMISSAL = [
    r"\bnot mine to\b",
    r"\bnot my (problem|call|concern|gate)\b",
    r"\byours to (own|solve|answer)\b",
    r"\b(that'?s|it'?s) (dana|marcus|priya|tomas|simone)'?s\b",
    r"\bstay in my lane\b",
    r"\bnot the problem i (solve|own)\b",
    r"\bi'?m not (going to )?(weigh|weighing)\b",
    r"\bnot going to pretend\b.{0,40}\bmine\b",
    r"\bi don'?t care about\b",
]

# Evidence the speaker is pointing at a real artefact rather than asserting.
CITATION = [
    r"PROJECT-SPEC",
    r"PHASE4-REPORT",
    r"README",
    r"design\.md",
    r"\bsection \d",
    r"\bphase 4a\b",
    r"\bphase 3\b",
    r"\broadmap\b",
    r"20-40%",
    r"\$0\.0006",
    r"\beleven days\b",
    r"\b2019\b|\b2018\b|\b2023\b",
]

STOPWORDS = set(
    """a an the and or but if then than that this these those to of in on at for with without
    is are was were be been being do does did doing have has had having i you he she it we they
    me him her us them my your his its our their as by from about into over under so not no nor
    can could should would will shall may might must here there what which who whom when where
    why how all any both each few more most other some such only own same too very s t just don
    now also because while what's i'm we're it's that's don't going need needs needed one two""".split()
)


def tokens(text: str) -> set:
    return {
        w
        for w in re.findall(r"[a-z0-9$%.\-]+", text.lower())
        if w not in STOPWORDS and len(w) > 2
    }


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def count_hits(text: str, patterns: List[str]) -> int:
    return sum(1 for p in patterns if re.search(p, text, re.IGNORECASE))


def turns_matching(conv: List[Dict], patterns: List[str]) -> int:
    return sum(1 for m in conv if count_hits(m["content"], patterns) > 0)


def score_arm(conv: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(conv)
    speakers = sorted({m["speaker"] for m in conv})

    by_speaker: Dict[str, List[str]] = {s: [] for s in speakers}
    for m in conv:
        by_speaker[m["speaker"]].append(m["content"])

    # Headline divergence measure: how much do speakers share vocabulary?
    # Lower means the personas are genuinely arguing from different premises.
    sig = {s: tokens(" ".join(txts)) for s, txts in by_speaker.items()}
    pairs = list(combinations(speakers, 2))
    cross = [jaccard(sig[a], sig[b]) for a, b in pairs]
    cross_sim = sum(cross) / len(cross) if cross else 0.0

    # Does a speaker keep making the same argument across their own turns?
    # Some persistence is holding a position; total repetition is a stuck record.
    persistence = []
    for s in speakers:
        txts = by_speaker[s]
        if len(txts) < 2:
            continue
        tk = [tokens(t) for t in txts]
        persistence.append(
            sum(jaccard(a, b) for a, b in combinations(tk, 2))
            / len(list(combinations(tk, 2)))
        )
    self_sim = sum(persistence) / len(persistence) if persistence else 0.0

    return {
        "turns": n,
        "speakers": len(speakers),
        "cross_speaker_similarity": round(cross_sim, 4),
        "within_speaker_similarity": round(self_sim, 4),
        "accommodation_turns": turns_matching(conv, ACCOMMODATION),
        "accommodation_rate": round(turns_matching(conv, ACCOMMODATION) / n, 3),
        "dismissal_turns": turns_matching(conv, DISMISSAL),
        "dismissal_rate": round(turns_matching(conv, DISMISSAL) / n, 3),
        "citation_turns": turns_matching(conv, CITATION),
        "citation_rate": round(turns_matching(conv, CITATION) / n, 3),
        "mean_turn_chars": round(sum(len(m["content"]) for m in conv) / n),
        "turns_per_speaker": {s: len(by_speaker[s]) for s in speakers},
    }


JUDGE_SCHEMA = """{
  "distinct_positions": <int: how many genuinely distinct substantive positions on the decision appear>,
  "positions_citing_a_named_source": <int: positions that cite a specific named document or a concrete measured figure>,
  "participants_who_changed_position": <int>,
  "position_changes_justified_by_new_evidence": <int: of those, how many followed specific new evidence rather than social pressure>,
  "converged_to_single_view": <true|false: did the group drift toward one shared position by the end>,
  "talking_past_each_other": <int 0-5: 0 = fully engaging with each other's claims, 5 = parallel monologues that never connect>,
  "specificity": <int 0-5: 0 = generic advice any assistant would give, 5 = concrete and grounded in this project's particulars>,
  "most_distinctive_participant": "<name>",
  "least_distinctive_participant": "<name>",
  "one_line_characterisation": "<one sentence describing how this discussion behaved>"
}"""


async def judge_arm(label: str, conv: List[Dict[str, Any]], model: str) -> Dict[str, Any]:
    import litellm

    transcript = "\n\n".join(f"{m['speaker']}: {m['content']}" for m in conv)
    prompt = (
        "You are analysing one transcript of a group discussion among five "
        "stakeholders about whether a software tool should add a document "
        "retrieval layer.\n\n"
        "Assess only what is in front of you. You are not comparing it to "
        "anything, and there is no expected or preferred answer.\n\n"
        f"TRANSCRIPT\n{transcript}\n\n"
        f"Reply with ONLY a JSON object of this exact shape:\n{JUDGE_SCHEMA}"
    )
    resp = await litellm.acompletion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=900,
    )
    raw = resp.choices[0].message.content.strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {"error": "unparseable judge response", "raw": raw[:400]}
    out = json.loads(m.group(0))
    out["_judge_cost_usd"] = round(litellm.completion_cost(resp) or 0.0, 6)
    return out


async def run_judge(
    arms: Dict[str, List[Dict[str, Any]]], model: str, seed: int
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Judge each arm under a shuffled anonymous label.

    The mapping is withheld from the judge and only rejoined afterwards, so the
    judge cannot know which arm is the control or which one the experiment hopes
    will win.
    """
    names = list(arms)
    blind = [f"transcript_{i}" for i in range(1, len(names) + 1)]
    rng = random.Random(seed)
    rng.shuffle(names)
    mapping = dict(zip(blind, names))

    results = await asyncio.gather(
        *(judge_arm(b, arms[mapping[b]], model) for b in blind)
    )
    return {mapping[b]: r for b, r in zip(blind, results)}, mapping


def fmt_table(rows: List[List[str]]) -> str:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = []
    for idx, row in enumerate(rows):
        out.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip())
        if idx == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir", type=Path)
    ap.add_argument("--judge", action="store_true", help="also run the blind LLM judge")
    ap.add_argument("--model", default=None, help="judge model (default: settings)")
    ap.add_argument("--seed", type=int, default=7, help="blinding shuffle seed")
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()

    arms: Dict[str, List[Dict[str, Any]]] = {}
    for label, filename in ARM_FILES.items():
        path = args.results_dir / filename
        if not path.exists():
            if label in OPTIONAL_ARMS:
                print(f"note: skipping {label} (no {filename})", file=sys.stderr)
                continue
            print(f"missing result file: {path}", file=sys.stderr)
            return 1
        arms[label] = json.loads(path.read_text())["conversation"]

    det = {label: score_arm(conv) for label, conv in arms.items()}

    labels = [l for l in ARM_FILES if l in arms]
    metrics = [
        ("cross_speaker_similarity", "cross-speaker similarity (LOWER = more divergent)"),
        ("within_speaker_similarity", "within-speaker similarity"),
        ("accommodation_rate", "accommodation rate (LOWER = less harmonising)"),
        ("dismissal_rate", "dismissal rate (dismisses field firing)"),
        ("citation_rate", "citation rate (points at real sources)"),
        ("mean_turn_chars", "mean turn length (chars)"),
    ]
    rows = [["metric"] + labels]
    for key, desc in metrics:
        rows.append([desc] + [str(det[l][key]) for l in labels])
    print("DETERMINISTIC (no model in the loop)\n")
    print(fmt_table(rows))

    report: Dict[str, Any] = {"deterministic": det}

    if args.judge:
        model = args.model
        if not model:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from matrix_studio.settings import get_settings

            model = get_settings().litellm_model
        judged, mapping = asyncio.run(run_judge(arms, model, args.seed))
        report["judge"] = judged
        report["judge_blinding"] = mapping
        report["judge_model"] = model

        jkeys = [
            ("distinct_positions", "distinct substantive positions"),
            ("positions_citing_a_named_source", "positions citing a named source"),
            ("participants_who_changed_position", "participants who changed position"),
            (
                "position_changes_justified_by_new_evidence",
                "  ...of those, driven by new evidence",
            ),
            ("converged_to_single_view", "converged to a single view"),
            ("talking_past_each_other", "talking past each other (0-5, lower better)"),
            ("specificity", "specificity (0-5, higher better)"),
            ("most_distinctive_participant", "most distinctive participant"),
        ]
        jrows = [["judge metric (blind)"] + labels]
        for key, desc in jkeys:
            jrows.append([desc] + [str(judged[l].get(key, "?")) for l in labels])
        print("\n\nBLIND LLM JUDGE (arms anonymised and shuffled)\n")
        print(fmt_table(jrows))
        print()
        for l in labels:
            print(f"  {l}: {judged[l].get('one_line_characterisation','?')}")
        cost = sum(judged[l].get("_judge_cost_usd", 0) for l in labels)
        print(f"\njudge cost: ${cost:.6f}")

    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2) + "\n")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
