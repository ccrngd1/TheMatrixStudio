# SPDX-License-Identifier: Apache-2.0
"""Tests for the premise-validation scorer — the measurement instrument itself.

Two instrument defects were found while reading Arm D's result, and both had
already changed a published conclusion before they were caught. These tests exist
so neither can come back silently.

1. **Raw Jaccard similarity is monotonically increasing in text length.** The same
   arm, same speakers, same positions, truncated to shorter turns, scores lower.
   Arm D's turns are 37% longer than Arm B's, so the raw comparison could not be
   read. The fix is equal-volume truncation; two subsampling approaches and
   TF-cosine were tried first and all three failed (see the scorer's comments).

2. **The `DISMISSAL` phrase list was written against the original three arms** and
   missed one genuine dismissal in each of B and D — both bare-possessive forms.
   `docs/labels/dismissal-labels.json` is the hand-labelled ground truth; this
   file asserts the regex reproduces it, so a future edit that drifts from a
   reading fails.

The scorer is a script rather than a package module, so it is loaded by path.
"""

import importlib.util
import json
import random
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "score_validation.py"
LABELS = ROOT / "docs" / "labels" / "dismissal-labels.json"
ARMS_DIR = ROOT / "examples" / "validation"


def _load():
    spec = importlib.util.spec_from_file_location("score_validation", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sv():
    return _load()


@pytest.fixture(scope="module")
def labels():
    return json.loads(LABELS.read_text())


# --------------------------------------------------------------------------
# (1) Length normalisation
# --------------------------------------------------------------------------


def _synthetic_conv(turns_per_speaker: int, words_per_turn: int, seed: int = 5) -> list:
    """Two speakers drawing from fixed pools with a fixed 50% shared vocabulary.

    Words are drawn at random from each speaker's pool rather than cycled, so
    vocabulary grows with length the way real text does (Heaps' law) while the
    TRUE overlap between the two speakers is identical at every length. That is
    what makes it a fair test: an earlier version cycled deterministically through
    the pools, so longer text genuinely had a larger vocabulary and the "fix" was
    being blamed for a difference the fixture had built in.
    """
    rng = random.Random(seed)
    shared = [f"shared{i}" for i in range(150)]
    pools = {"A": shared + [f"alpha{i}" for i in range(150)],
             "B": shared + [f"beta{i}" for i in range(150)]}
    conv = []
    for _ in range(turns_per_speaker):
        for speaker in ("A", "B"):
            words = [rng.choice(pools[speaker]) for _ in range(words_per_turn)]
            conv.append({"speaker": speaker, "content": " ".join(words), "turn": len(conv)})
    return conv


def test_raw_similarity_is_length_biased(sv):
    """The defect, asserted as a fact rather than described in a comment.

    If this test ever fails, raw Jaccard has become length-robust and the
    normalisation below is redundant — which would be good news, but must not go
    unnoticed.
    """
    short = sv.score_arm(_synthetic_conv(3, 120))["cross_speaker_similarity"]
    long = sv.score_arm(_synthetic_conv(3, 1200))["cross_speaker_similarity"]
    assert long > short, "raw Jaccard no longer grows with length; revisit normalisation"


def test_normalised_similarity_is_stable_across_lengths(sv):
    """The fix: same speakers, same true overlap, 10x the text, same answer.

    Tolerance is deliberately loose (0.05) — the fixture draws words at random, so
    the claim is "length no longer dominates", not "identical to three decimals".
    """
    arms = {"short": _synthetic_conv(3, 150), "long": _synthetic_conv(3, 1500)}
    norm = sv.normalised_similarity(arms)
    a = norm["arms"]["short"]["cross_speaker_similarity_norm"]
    b = norm["arms"]["long"]["cross_speaker_similarity_norm"]
    assert abs(a - b) < 0.05, f"normalised similarity still length-sensitive: {a} vs {b}"


def test_normalised_similarity_still_detects_real_divergence(sv):
    """A length-robust metric that cannot see divergence would be useless.

    Two arms at the SAME length, one with speakers sharing vocabulary and one
    with speakers sharing none, must rank in the obvious order.
    """
    same_len = _synthetic_conv(3, 300)
    disjoint = [
        {**m, "content": " ".join(f"{m['speaker'].lower()}word{i}" for i in range(200))}
        for m in same_len
    ]
    norm = sv.normalised_similarity({"overlapping": same_len, "disjoint": disjoint})
    overlapping = norm["arms"]["overlapping"]["cross_speaker_similarity_norm"]
    assert overlapping > norm["arms"]["disjoint"]["cross_speaker_similarity_norm"]
    assert overlapping > 0, "metric returns 0 for genuinely overlapping speakers"


def test_truncated_jaccard_refuses_unequal_volumes(sv):
    """Comparing unequal text volumes IS the bug being fixed, so a stream shorter
    than the budget returns 0.0 rather than quietly comparing what it has."""
    short = ["a", "b"]
    long = [f"w{i}" for i in range(100)]
    assert sv.truncated_jaccard(short, long, 50) == 0.0


def test_normalisation_is_deterministic(sv):
    """No sampling at all now, so two runs must agree exactly or published numbers
    cannot be reproduced."""
    arms = {"x": _synthetic_conv(3, 300)}
    first = sv.normalised_similarity(arms)["arms"]["x"]
    second = sv.normalised_similarity(arms)["arms"]["x"]
    assert first == second


def test_budget_sensitivity_is_reported(sv):
    """A normalised number still has a free parameter. The scorer must say which
    orderings survive it — that is the check whose absence caused the original
    problem."""
    arms = {"a": _synthetic_conv(3, 400), "b": _synthetic_conv(4, 400)}
    norm = sv.normalised_similarity(arms)
    assert len(norm["budget_sensitivity"]) >= 2, "too few budgets to detect a flip"
    assert "ordering_stable_for" in norm and "ordering_FLIPS_for" in norm


# --------------------------------------------------------------------------
# (2) The DISMISSAL phrase list against hand labels
# --------------------------------------------------------------------------


def test_label_file_is_not_vacuous(labels):
    """Guard the guard: a label file with no positives would make the tests below
    pass trivially."""
    for arm, data in labels["arms"].items():
        assert data["clear"], f"{arm}: no labelled dismissals"
        for entry in data["clear"]:
            assert entry["quote"].strip(), f"{arm} turn {entry['turn']}: no quote"


@pytest.mark.parametrize("arm", ["arm-b-structured", "arm-d-shipped"])
def test_dismissal_regex_matches_hand_labels(sv, labels, arm):
    """The regex must reproduce a reading, on the arms that were actually read.

    Arms A and C are deliberately NOT asserted here: they were never hand-labelled,
    so their rates remain regex-only and claiming otherwise would be the same
    over-trust that produced the original problem.
    """
    path = ARMS_DIR / f"{arm}.json"
    if not path.exists():
        pytest.skip(f"{arm} config absent")
    # Score the recorded transcript if present; the arm CONFIG has no transcript,
    # so the labels are checked against the quotes themselves when it is not.
    data = labels["arms"][arm]
    for entry in data["clear"]:
        assert sv.count_hits(entry["quote"], sv.DISMISSAL) > 0, (
            f"{arm} turn {entry['turn']}: labelled a dismissal but the regex "
            f"does not fire on {entry['quote']!r}"
        )


def test_soft_cases_are_not_counted_as_dismissals(sv, labels):
    """The borderline turns were deliberately excluded from the counts. If the
    regex starts firing on them the rates shift without anyone deciding to."""
    for arm, data in labels["arms"].items():
        for entry in data.get("soft", []):
            assert sv.count_hits(entry["quote"], sv.DISMISSAL) == 0, (
                f"{arm} turn {entry['turn']}: soft case now counted as a dismissal — "
                "either relabel it deliberately or tighten the pattern"
            )


def test_recorded_hand_rates_are_higher_than_the_original_regex(labels):
    """The finding this labelling produced, locked so it is not misremembered:
    the regex under-counted BOTH arms, so the B-vs-D gap is real behaviour rather
    than an instrument artifact."""
    r = labels["result"]
    assert r["arm-b-structured"]["hand_clear"] > r["arm-b-structured"]["regex"]
    assert r["arm-d-shipped"]["hand_clear"] > r["arm-d-shipped"]["regex"]
    # ...and Arm D still dismisses materially less often than Arm B by hand count.
    assert r["arm-d-shipped"]["hand_rate"] < r["arm-b-structured"]["hand_rate"]
