# SPDX-License-Identifier: Apache-2.0
"""Tests for the Phase 5 premise-validation arm generator.

The premise-validation experiment (``docs/PHASE5-PREMISE-VALIDATION.md``) only
means anything if two properties hold, so both are locked here rather than left
as claims in a document:

1. The three arms differ in EXACTLY ONE field (each persona's ``persona``
   string). Same topic, same cast names, same goals, same config. Otherwise a
   measured difference between arms is not attributable to persona structure.
2. ``validity`` — the authoring calibration note recording whether a position is
   actually correct — NEVER reaches a prompt. It exists only for post-run
   scoring. A leak would tell the cast which positions are the right ones and
   invalidate the whole run.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_validation_arms.py"


def _load():
    spec = importlib.util.spec_from_file_location("build_validation_arms", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


@pytest.fixture(scope="module")
def arms(mod):
    return {name: mod.build_arm(kind) for name, kind in mod.ARMS.items()}


def test_three_arms_built(arms):
    assert set(arms) == {"arm-a-control", "arm-b-structured", "arm-c-grounded"}


def test_arms_differ_only_in_persona_string(arms):
    """Topic, cast names, goals and config must be byte-identical across arms."""
    reference = arms["arm-a-control"]
    for name, arm in arms.items():
        assert arm["topic"] == reference["topic"], f"{name}: topic differs"
        assert arm["config"] == reference["config"], f"{name}: config differs"
        assert [c["name"] for c in arm["cast"]] == [
            c["name"] for c in reference["cast"]
        ], f"{name}: cast names/order differ"
        assert [c["goals"] for c in arm["cast"]] == [
            c["goals"] for c in reference["cast"]
        ], f"{name}: goals differ"


def test_persona_strings_actually_differ_between_arms(arms):
    """Guard against the arms silently collapsing into the same experiment."""
    for member in range(len(arms["arm-a-control"]["cast"])):
        a = arms["arm-a-control"]["cast"][member]["persona"]
        b = arms["arm-b-structured"]["cast"][member]["persona"]
        c = arms["arm-c-grounded"]["cast"][member]["persona"]
        assert a != b and b != c and a != c
        # B and C add material to the prose baseline, so they must be longer.
        assert len(b) > len(a)
        assert len(c) > len(b)


def test_validity_never_appears_in_any_persona_string(mod, arms):
    """The calibration note must not leak into a prompt, in any arm."""
    validities = {
        vp["validity"] for m in mod.CAST for vp in m["viewpoints"]
    }
    assert validities, "no validity values authored - test would be vacuous"

    for name, arm in arms.items():
        for member in arm["cast"]:
            persona = member["persona"]
            assert "validity" not in persona.lower(), f"{name}/{member['name']}"
            for value in validities:
                assert value not in persona.lower(), (
                    f"{name}/{member['name']} leaked validity {value!r}"
                )


def test_validity_not_in_topic(mod):
    """The shared brief must not name the calibration values either."""
    topic = mod.TOPIC.lower()
    for m in mod.CAST:
        for vp in m["viewpoints"]:
            assert vp["validity"] not in topic


def test_calibration_covers_all_four_validity_values(mod):
    """Requirement 6.2 of the source spec: span the full validity range so the
    operator cannot succeed by reflexively conceding or reflexively resisting."""
    got = {vp["validity"] for m in mod.CAST for vp in m["viewpoints"]}
    assert got == {"sound", "outdated", "misapplied", "overgeneralised"}


def test_firmness_is_not_a_proxy_for_correctness(mod):
    """Requirement 6.3: the firmest positions must not all be the unsound ones,
    nor all the sound ones, or firmness leaks the answer."""
    pairs = [
        (vp["firmness"], vp["validity"])
        for m in mod.CAST
        for vp in m["viewpoints"]
    ]
    hardest = {validity for firmness, validity in pairs if firmness != "negotiable"}
    assert "sound" in hardest, "no sound position is firmly held"
    assert hardest - {"sound"}, "only sound positions are firmly held"


def test_every_viewpoint_has_required_fields(mod):
    for m in mod.CAST:
        assert m["viewpoints"], f"{m['name']} has no viewpoints"
        for vp in m["viewpoints"]:
            for field in (
                "position",
                "underlying_concern",
                "formed_by",
                "firmness",
                "evidence_that_shifts",
                "validity",
            ):
                assert vp.get(field) is not None, f"{m['name']}: missing {field}"
            assert vp["firmness"] in mod.FIRMNESS_BEHAVIOUR


def test_dismisses_is_populated_for_every_persona(mod):
    """The source spec calls negative space the primary differentiator between
    personas, so an empty ``dismisses`` defeats the purpose of the test arm."""
    for m in mod.CAST:
        assert m["preferences"]["dismisses"], f"{m['name']} dismisses nothing"


def test_grounded_arm_carries_real_citations(mod, arms):
    """Arm C must actually contain quoted source text; that is its whole point."""
    for member in arms["arm-c-grounded"]["cast"]:
        assert "SOURCES YOU HAVE READ" in member["persona"]
    structured = arms["arm-b-structured"]["cast"]
    for member in structured:
        assert "SOURCES YOU HAVE READ" not in member["persona"]


def test_underlying_concern_is_marked_as_withheld(arms):
    """It must be present but explicitly gated, in both structured arms."""
    for name in ("arm-b-structured", "arm-c-grounded"):
        for member in arms[name]["cast"]:
            assert "do NOT volunteer this" in member["persona"]


def test_cognition_disabled_in_every_arm(arms):
    """Cognition would confound the variable under test."""
    for arm in arms.values():
        assert arm["config"]["cognition"]["enabled"] is False
