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
    # Added after HAND-LABELLING arms B and D (docs/labels/dismissal-labels.json).
    # The list above was written against the ORIGINAL arms and missed one genuine
    # dismissal in each of B and D — both bare-possessive forms with no following
    # infinitive, which the `not mine to` / `yours to own` patterns cannot reach.
    # Labelling first, then patching, is deliberate: the label file is the ground
    # truth these were checked against, so the patterns were not tuned until the
    # rate they produce matched a reading.
    r"\bnot mine\b",
    r"\btheir (job|problem|call) to (own|solve|answer|make)\b",
    r"\byour call to make\b",
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


def token_list(text: str) -> List[str]:
    """Content tokens in order, WITH repeats.

    Repeats are kept because the length-normalised measures below subsample the
    token *stream*: the counterfactual we want is "what if this speaker had
    written less", not "what if their vocabulary were smaller".
    """
    return [
        w
        for w in re.findall(r"[a-z0-9$%.\-]+", text.lower())
        if w not in STOPWORDS and len(w) > 2
    ]


def tokens(text: str) -> set:
    return set(token_list(text))


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# --------------------------------------------------------------------------
# Length normalisation
#
# Raw Jaccard on token SETS is monotonically increasing in text length, which
# makes it unusable for comparing arms whose turn lengths differ. Measured on one
# arm by truncating every turn and re-scoring:
#
#     turns truncated to  300 chars -> cross-speaker 0.1051
#                         500       -> 0.1336
#                         800       -> 0.1434
#                         full (962)-> 0.1597
#
# Same arm, same speakers, same positions: the number tracks length, not
# divergence. The mechanism is vocabulary growth — more text means more distinct
# words, and more chance that any given word appears in both speakers.
#
# This mattered concretely: Arm D (Phase 6) has 1318-char turns against Arm B's
# 962 and scored "worse" on divergence, so that comparison could not be read.
#
# TWO FIXES WERE TRIED AND REJECTED, recorded so they are not retried:
#
#   1. Subsample each speaker's token STREAM to a fixed count. Equalises token
#      count but not vocabulary size — a short stream repeats a small vocabulary
#      while a long one spreads over a large one. A length effect survived
#      (0.298 vs 0.243 on a fixture whose true overlap was identical).
#   2. Subsample each speaker's VOCABULARY to a fixed size. Worse (0.287 vs
#      0.145): drawing N words from vocabularies of genuinely different sizes
#      changes the chance of drawing the shared ones, so equal sample sizes do
#      not rescue unequal populations.
#   3. TF-cosine instead of Jaccard, on the theory that frequency vectors are
#      scale-invariant. Also length-sensitive (0.235 -> 0.392 under the same
#      truncation sweep), so it is not a fix either.
#
# What actually works is the boring thing: compare at EQUAL TEXT VOLUME. Truncate
# every speaker's token stream to a common budget, then apply the original metric.
# Deterministic, no sampling, no seed, and it answers the exact question — "if
# every arm had produced the same amount of text, how similar would the speakers
# be?"
#
# The budget remains a free parameter, so `normalised_similarity` also reports
# which orderings survive changing it. A normalised number is still a number
# produced by a choice.


def truncated_jaccard(stream_a: List[str], stream_b: List[str], budget: int) -> float:
    """Jaccard between the first ``budget`` tokens of each stream.

    Returns 0.0 when either stream is shorter than ``budget``: the caller picks a
    budget every speaker can meet, and silently comparing unequal volumes is the
    bug being fixed.
    """
    if budget <= 0 or len(stream_a) < budget or len(stream_b) < budget:
        return 0.0
    return jaccard(set(stream_a[:budget]), set(stream_b[:budget]))


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


def speaker_streams(conv: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Each speaker's full token stream, and each speaker's per-turn streams."""
    out: Dict[str, List[str]] = {}
    for m in conv:
        out.setdefault(m["speaker"], []).extend(token_list(m["content"]))
    return out


def turn_streams(conv: List[Dict[str, Any]]) -> Dict[str, List[List[str]]]:
    out: Dict[str, List[List[str]]] = {}
    for m in conv:
        out.setdefault(m["speaker"], []).append(token_list(m["content"]))
    return out


def normalised_similarity(
    arms: Dict[str, List[Dict[str, Any]]]
) -> Dict[str, Any]:
    """Length-normalised cross- and within-speaker similarity for every arm.

    Must see ALL arms at once: the point is that every comparison uses the same
    token budget, and the largest budget every speaker in every arm can meet is
    not knowable from one arm. That is why this is not part of ``score_arm``.

    The budgets are reported alongside the numbers, because a budget is part of
    the measurement — a reader who does not know it cannot compare these figures
    to anything else.
    """
    streams = {label: speaker_streams(conv) for label, conv in arms.items()}
    turns = {label: turn_streams(conv) for label, conv in arms.items()}

    # Budgets are token COUNTS: the largest volume every speaker (and every turn)
    # in every arm can supply, so nothing is compared against a shorter sample.
    cross_budget = min(
        len(st) for per_speaker in streams.values() for st in per_speaker.values()
    )
    within_budget = min(
        len(t)
        for per_speaker in turns.values()
        for tlist in per_speaker.values()
        for t in tlist
    )

    # Budget robustness. A normalised number is still a number produced by a
    # choice, and this one has a free parameter. Measured: the A-vs-D ordering
    # FLIPS between budget 100 and 140, while B-lowest and C-highest hold at every
    # budget. So the honest output is which orderings survive the parameter, not a
    # single ranking presented as fact. Having just replaced one metric for hiding
    # a confound, shipping its replacement without this check would repeat the
    # mistake in a new place.
    # Budgets BELOW ~100 tokens are excluded. Measured: at 40-80 tokens the arm
    # ordering scrambles completely (C<A<B<D at 40, D<A<B<C at 61 and 80) and then
    # settles from 102 upward. Eighty content tokens is a couple of sentences per
    # speaker — too little text for vocabulary overlap to mean anything — so
    # including them manufactures instability rather than detecting it. Getting
    # this wrong initially made every pair look uncallable and hid a real result.
    MIN_MEANINGFUL_BUDGET = 100
    fractions = (0.55, 0.7, 0.85, 1.0)
    out_stability: Dict[str, Any] = {}
    for frac in fractions:
        b = int(cross_budget * frac)
        if b < MIN_MEANINGFUL_BUDGET:
            continue
        ranking = {}
        for label in arms:
            sp = sorted(streams[label])
            vals = [
                truncated_jaccard(streams[label][x], streams[label][y], b)
                for x, y in combinations(sp, 2)
            ]
            ranking[label] = sum(vals) / len(vals) if vals else 0.0
        out_stability[str(b)] = sorted(ranking, key=lambda k: ranking[k])

    if not out_stability:
        # Every candidate budget was below the meaningfulness floor: the arms are
        # too short to compare at all. Say so rather than emitting a ranking.
        out_stability["insufficient_text"] = sorted(arms)

    stable_pairs, unstable_pairs = [], []
    for x, y in combinations(sorted(arms), 2):
        orders = {
            tuple(o.index(x) < o.index(y) for _ in (0,))[0]
            for o in out_stability.values()
        }
        (stable_pairs if len(orders) == 1 else unstable_pairs).append(f"{x} vs {y}")

    out: Dict[str, Any] = {
        "cross_budget_tokens": cross_budget,
        "within_budget_tokens": within_budget,
        "method": "equal-volume truncation (deterministic; no sampling)",
        "arms": {},
        "budget_sensitivity": out_stability,
        "ordering_stable_for": stable_pairs,
        "ordering_FLIPS_for": unstable_pairs,
    }

    for label in arms:
        speakers = sorted(streams[label])
        cross = [
            truncated_jaccard(streams[label][a], streams[label][b], cross_budget)
            for a, b in combinations(speakers, 2)
        ]
        within = []
        for sp in speakers:
            tl = turns[label][sp]
            if len(tl) < 2:
                continue
            pairs = [
                truncated_jaccard(a, b, within_budget)
                for a, b in combinations(tl, 2)
            ]
            if pairs:
                within.append(sum(pairs) / len(pairs))
        out["arms"][label] = {
            "cross_speaker_similarity_norm": round(
                sum(cross) / len(cross) if cross else 0.0, 4
            ),
            "within_speaker_similarity_norm": round(
                sum(within) / len(within) if within else 0.0, 4
            ),
        }
    return out


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
    norm = normalised_similarity(arms)

    labels = [l for l in ARM_FILES if l in arms]
    metrics = [
        ("cross_speaker_similarity", "cross-speaker similarity RAW (length-biased)"),
        ("within_speaker_similarity", "within-speaker similarity RAW (length-biased)"),
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

    # The RAW similarity rows above are retained only so previously published
    # numbers stay reproducible. They rise with turn length regardless of
    # divergence, so cross-arm comparison must use the normalised rows.
    nrows = [["metric (length-normalised)"] + labels]
    for key, desc in (
        ("cross_speaker_similarity_norm", "cross-speaker similarity (LOWER = more divergent)"),
        ("within_speaker_similarity_norm", "within-speaker similarity (LOWER = less repetitive)"),
    ):
        nrows.append([desc] + [str(norm["arms"][l][key]) for l in labels])
    print(
        f"\n\nLENGTH-NORMALISED (equal token volume: {norm['cross_budget_tokens']} "
        f"per speaker, {norm['within_budget_tokens']} per turn; "
        "deterministic)\n"
    )
    print(fmt_table(nrows))
    print(
        "\n  Use these rows to compare arms. The RAW rows above are length-biased:\n"
        "  the same arm truncated to 300-char turns scores 0.105 and at full length\n"
        "  0.160, with identical speakers and positions."
    )
    if norm["ordering_FLIPS_for"]:
        print(
            "\n  NOT CALLABLE — cross-speaker ordering flips with the token budget for:\n"
            + "\n".join(f"    {p}" for p in norm["ordering_FLIPS_for"])
            + "\n  These pairs are within the instrument's resolution. Do not rank them."
        )
    if norm["ordering_stable_for"]:
        print(
            "\n  Stable across every budget tried "
            f"({', '.join(sorted(norm['budget_sensitivity']))} tokens):\n"
            + "\n".join(f"    {p}" for p in norm["ordering_stable_for"])
        )

    report: Dict[str, Any] = {"deterministic": det, "length_normalised": norm}

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
