# SPDX-License-Identifier: Apache-2.0
"""
The scoring phrase lists against the hand labels they were validated on.

`ACCOMMODATION` and `DISMISSAL` in scripts/score_validation.py decide every accommodation and
dismissal rate this project reports, and the lists silently depend on a model's idiom: validated on
Haiku, they matched 0 of 10 accommodating turns on Sonnet 5 (docs/labels/sonnet-labels.json). These
tests pin both label sets, so a pattern edit that stops matching a labelled turn — or starts matching
an unlabelled one — fails here rather than moving a reported rate unnoticed.
"""

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import score_validation as sv  # noqa: E402

LABELS = json.loads((ROOT / "docs/labels/sonnet-labels.json").read_text())
TRANSCRIPTS = json.loads((ROOT / "docs/labels/sonnet-transcripts.json").read_text())
HAIKU = json.loads((ROOT / "docs/labels/dismissal-labels.json").read_text())

LISTS = {"dismissal": sv.DISMISSAL, "accommodation": sv.ACCOMMODATION}

#: Known, documented disagreements (sonnet-labels.json "held_out" and the dev-run misses). Adding to
#: this set is a decision to record in the label file, not a way to make a failing test pass.
KNOWN_MISSES = {("327167c9", 7, "dismissal"), ("bc73fe52", 12, "dismissal"), ("486c6dbb", 11, "accommodation")}
KNOWN_EXTRA = {("486c6dbb", 9, "dismissal")}  # labelled soft


def _hit(text, kind):
    return any(re.search(p, text, re.I) for p in LISTS[kind])


CASES = [
    (rid, m["turn"], kind, m["content"])
    for rid, run in TRANSCRIPTS.items()
    for m in run["conversation"]
    for kind in LISTS
]


@pytest.mark.parametrize("rid,turn,kind,text", CASES, ids=[f"{c[0]}-t{c[1]}-{c[2]}" for c in CASES])
def test_sonnet_turns_score_as_they_were_read(rid, turn, kind, text):
    labels = LABELS["runs"][rid][kind]
    clear, soft = turn in labels["clear"], turn in labels["soft"]
    hit = _hit(text, kind)
    if clear and (rid, turn, kind) not in KNOWN_MISSES:
        assert hit, f"labelled {kind} but no pattern matches"
    if not clear and not soft:
        assert not hit or (rid, turn, kind) in KNOWN_EXTRA, f"a pattern matches a turn read as no {kind}"


@pytest.mark.parametrize("arm,tier,quote", [
    (arm, tier, x["quote"]) for arm, v in HAIKU["arms"].items() for tier in ("clear", "soft") for x in v.get(tier, [])
])
def test_the_haiku_dismissal_labels_still_hold(arm, tier, quote):
    """The Sonnet patches must not cost the Haiku labels they were added alongside."""
    assert _hit(quote, "dismissal") == (tier == "clear")
