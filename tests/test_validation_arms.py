# SPDX-License-Identifier: Apache-2.0
"""Tests for the Phase 5 premise-validation arm generator.

The premise-validation experiment (``docs/PHASE5-PREMISE-VALIDATION.md``) only
means anything if two properties hold, so both are locked here rather than left
as claims in a document:

1. The arms differ in EXACTLY ONE field (each persona's ``persona`` string).
   Same topic, same cast names, same goals, same config. Otherwise a measured
   difference between arms is not attributable to persona structure.

   Arm D (Phase 6) is the single permitted exception, and it is enumerated rather
   than waived: the feature it tests is gated behind ``config.personas.enabled``,
   so it MUST differ there, and it carries a ``structured`` block. Every other
   field still has to match, and the extra keys are asserted to be exactly those
   two — a third difference appearing silently is what this test exists to catch.
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


# The Phase 6 arms' permitted deviations, enumerated. Anything outside these fails.
ARM_D = "arm-d-shipped"
# D, E and F share a cast and differ ONLY in config.personas.dismissal_rule.
STRUCTURED_ARMS = (ARM_D, "arm-e-mandatory", "arm-f-blunt", "arm-g-cognition")
ARM_D_CONFIG_KEYS = {"personas"}
ARM_D_CAST_KEYS = {"structured"}
# The variant each arm must carry. A drift here would make an arm silently test
# the wrong wording, which is the exact failure the engine bug
# (bool(dismissal_rule)) would have caused.
EXPECTED_RULES = {
    ARM_D: "retuned",
    "arm-e-mandatory": "mandatory",
    "arm-f-blunt": "blunt",
    # G's variable is cognition, so it reuses the shipped default wording.
    "arm-g-cognition": "mandatory",
}
# Arm G is the only arm that turns cognition ON. Enumerated rather than waived, so a
# second arm quietly enabling it would fail.
COGNITION_ARM = "arm-g-cognition"


def test_all_arms_built(arms):
    assert set(arms) == {
        "arm-a-control", "arm-b-structured", "arm-c-grounded",
        *STRUCTURED_ARMS,
    }


def test_arms_differ_only_in_persona_string(arms):
    """Topic, cast names, goals and config must be byte-identical across arms.

    Arm D may add exactly ``config.personas`` and a per-member ``structured``
    key — nothing else. Checked by set difference rather than by skipping the
    arm, so a fourth difference creeping in still fails.
    """
    reference = arms["arm-a-control"]
    for name, arm in arms.items():
        assert arm["topic"] == reference["topic"], f"{name}: topic differs"

        extra_config = set(arm["config"]) - set(reference["config"])
        assert extra_config == (ARM_D_CONFIG_KEYS if name in STRUCTURED_ARMS else set()), (
            f"{name}: unexpected config keys {sorted(extra_config)}"
        )
        shared = {k: v for k, v in arm["config"].items() if k in reference["config"]}
        if name == COGNITION_ARM:
            # G's whole point is cognition ON; everything else must still match.
            assert shared.pop("cognition") == {"enabled": True}
            expected = {k: v for k, v in reference["config"].items() if k != "cognition"}
            assert shared == expected, f"{name}: shared config differs beyond cognition"
        else:
            assert shared == reference["config"], f"{name}: shared config differs"

        assert [c["name"] for c in arm["cast"]] == [
            c["name"] for c in reference["cast"]
        ], f"{name}: cast names/order differ"
        assert [c["goals"] for c in arm["cast"]] == [
            c["goals"] for c in reference["cast"]
        ], f"{name}: goals differ"

        for member, ref_member in zip(arm["cast"], reference["cast"]):
            extra = set(member) - set(ref_member)
            assert extra == (ARM_D_CAST_KEYS if name in STRUCTURED_ARMS else set()), (
                f"{name}/{member['name']}: unexpected cast keys {sorted(extra)}"
            )


def test_rule_variant_arms_differ_only_in_the_rule(arms):
    """D, E and F are the dismissal-rule experiment. They must share a byte-identical
    cast, so the rendered wording is the single variable. If the cast drifted, the
    arms would be measuring two things at once — the mistake Arm D itself made
    against Arm B."""
    casts = {a: arms[a]["cast"] for a in STRUCTURED_ARMS}
    reference = casts[ARM_D]
    for name, cast in casts.items():
        assert cast == reference, f"{name}: cast differs from {ARM_D}"
    rules = {a: arms[a]["config"]["personas"]["dismissal_rule"] for a in STRUCTURED_ARMS}
    assert rules == EXPECTED_RULES, rules
    # D/E/F must each carry a DISTINCT wording — they are the wording experiment.
    # G deliberately shares E's wording because its variable is cognition.
    wording_arms = [a for a in STRUCTURED_ARMS if a != COGNITION_ARM]
    assert len({rules[a] for a in wording_arms}) == len(wording_arms)


def test_only_arm_g_enables_cognition(arms):
    """Cognition was OFF in all nine runs before Arm G. If another arm switched it on,
    every comparison against those nine would silently gain a second variable."""
    for name, arm in arms.items():
        on = bool(arm["config"].get("cognition", {}).get("enabled"))
        assert on == (name == COGNITION_ARM), f"{name}: cognition enabled={on}"


def test_rule_variants_produce_genuinely_different_prompts(arms):
    """The whole experiment rests on the variant reaching the prompt. A silent
    collapse to the default wording — which a `bool()` coercion in the engine did
    cause — would produce numbers that tested nothing."""
    from matrix_studio.personas import effective_persona, parse_structured

    rendered = {}
    for name in STRUCTURED_ARMS:
        member = arms[name]["cast"][0]
        rendered[name] = effective_persona(
            member["persona"],
            parse_structured(member["structured"]),
            dismissal_rule=arms[name]["config"]["personas"]["dismissal_rule"],
        )
    # G renders identically to E by design (same wording); D and F must differ.
    assert len(set(rendered.values())) == len(STRUCTURED_ARMS) - 1
    assert rendered["arm-g-cognition"] == rendered["arm-e-mandatory"]
    # Each arm's own wording, spot-checked by a phrase unique to it.
    assert "not on your attention" in rendered[ARM_D]
    assert "MUST say plainly" in rendered["arm-e-mandatory"]
    assert "Ignore the things you consider not your problem" in rendered["arm-f-blunt"]


def test_arm_d_is_the_control_prose_plus_structured_data(arms):
    """Arm D must reuse the CONTROL persona string, not Arm B's rendered prose.

    That is what makes the comparison mean something: D vs A isolates the Phase 6
    feature, and D vs B asks whether the engine's rendering reproduces what
    hand-written prose structure achieved. If D silently inherited B's persona
    string it would be measuring both at once.
    """
    for d, a, b in zip(
        arms[ARM_D]["cast"],
        arms["arm-a-control"]["cast"],
        arms["arm-b-structured"]["cast"],
    ):
        assert d["persona"] == a["persona"], f"{d['name']}: not the control prose"
        assert d["persona"] != b["persona"]
        assert d["structured"]["viewpoints"], f"{d['name']}: no positions to defend"


def test_arm_d_structured_block_parses_under_the_real_model(arms):
    """The generator and the shipped schema must not drift apart."""
    from matrix_studio.personas import parse_structured

    for member in arms[ARM_D]["cast"]:
        sp = parse_structured(member["structured"])
        assert sp is not None, member["name"]
        assert sp.preferences.dismisses, f"{member['name']}: nothing dismissed"


def test_arm_d_never_renders_validity(mod, arms):
    """Phase 6 accepts `validity` and guarantees it is never rendered. Arm D
    carries it through on purpose, so this asserts the guarantee end to end
    rather than dodging it by omitting the field."""
    from matrix_studio.personas import effective_persona, parse_structured

    validities = {
        vp["validity"] for m in mod.CAST for vp in m["viewpoints"] if vp.get("validity")
    }
    assert validities, "no calibration notes to leak — test would be vacuous"
    for member in arms[ARM_D]["cast"]:
        assert any(
            vp.get("validity") for vp in member["structured"]["viewpoints"]
        ), f"{member['name']}: validity was dropped, so the check is vacuous"
        rendered = effective_persona(
            member["persona"], parse_structured(member["structured"])
        )
        for v in validities:
            assert v not in rendered, f"{member['name']}: leaked {v!r}"


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


def test_cognition_disabled_in_every_arm_except_g(arms):
    """Cognition confounds the persona variable, so the original arms hold it off.

    Arm G is the deliberate exception — measuring that interaction IS its purpose —
    and it is named rather than skipped, so a third arm enabling cognition fails
    here. See also test_only_arm_g_enables_cognition.
    """
    for name, arm in arms.items():
        expected = name == COGNITION_ARM
        assert arm["config"]["cognition"]["enabled"] is expected, name
