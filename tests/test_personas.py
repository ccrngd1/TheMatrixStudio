# SPDX-License-Identifier: Apache-2.0
"""Phase 6 tests — structured personas (convictions, not just goals).

The most important tests here are the LEAKAGE ones. Two fields are private by
design and the whole exercise depends on them staying private:

- ``underlying_concern`` — the real worry behind a stated position. Drawing it
  out is the skill the panel exercises, so it must never appear in the
  moderator's persona list (one prompt away from the entire cast) or in the
  event log.
- ``validity`` — the operator's calibration note. Telling a persona its own
  position is "outdated" would collapse the exercise entirely.

Second most important: with the feature off, prompts must be byte-identical to
pre-Phase-6. That is what makes this safe to ship on by nobody.
"""

import pytest
from pydantic import ValidationError

from matrix_studio.personas import (
    FIRMNESS_LEVELS,
    FormativeEvent,
    PersonaBackground,
    PersonaPreferences,
    StructuredPersona,
    Viewpoint,
    effective_persona,
    parse_structured,
    public_persona,
    structured_payload,
)
from matrix_studio.state import AgentState, PersonaConfig

CONCERN = "I own the failure when a customer cannot get to a working run"
VALIDITY_NOTE = "overgeneralised"


def build(**overrides):
    data = dict(
        role="Head of Distribution & Packaging",
        background=PersonaBackground(
            tenure_years=9,
            prior_roles=["Release engineering for an on-prem analytics product"],
            formative_events=[
                FormativeEvent(
                    year=2023,
                    event="Shipped a product whose quickstart needed a separate vector database",
                    lesson="Every extra service costs you users before they see it work",
                )
            ],
        ),
        preferences=PersonaPreferences(
            optimises_for=["time-to-first-run", "a single deployable artifact"],
            dismisses=["retrieval answer quality", "research novelty"],
            persuaded_by=["a working install on a clean machine"],
        ),
        viewpoints=[
            Viewpoint(
                position="No feature may add a stateful external service to the default install",
                underlying_concern=CONCERN,
                formed_by="The 2023 product that stalled at the install step",
                firmness="firm",
                evidence_that_shifts=["an embedded index that is a file, not a service"],
                validity=VALIDITY_NOTE,
            )
        ],
    )
    data.update(overrides)
    return StructuredPersona(**data)


# --------------------------------------------------------------------------
# Leakage — the correctness core
# --------------------------------------------------------------------------


def test_underlying_concern_never_reaches_the_public_summary():
    """The moderator prompt lists every persona at once. A concern rendered
    there is one prompt away from the whole cast."""
    assert CONCERN not in build().render_public()


def test_underlying_concern_never_reaches_another_agents_prompt():
    sp = build()
    assert CONCERN not in public_persona("Prose.", sp)


def test_validity_is_never_rendered_anywhere():
    """An authoring/scoring note only. Telling a persona its position is
    'overgeneralised' would collapse the exercise."""
    sp = build()
    assert VALIDITY_NOTE not in sp.render_private()
    assert VALIDITY_NOTE not in sp.render_private(withhold_concerns=False)
    assert VALIDITY_NOTE not in sp.render_public()
    assert VALIDITY_NOTE not in str(structured_payload(sp))


def test_event_payload_strips_both_private_fields():
    payload = structured_payload(build())
    flat = str(payload)
    assert CONCERN not in flat
    assert VALIDITY_NOTE not in flat
    # but keeps what makes the run auditable
    assert "firm" in flat
    assert "stateful external service" in flat


def test_payload_is_json_serialisable():
    import json

    payload = structured_payload(build())
    assert json.loads(json.dumps(payload)) == payload


def test_structured_payload_of_none_is_none():
    assert structured_payload(None) is None


# --------------------------------------------------------------------------
# The private rendering: it must contain the things it exists for
# --------------------------------------------------------------------------


def test_private_render_carries_the_concern_with_its_withholding_instruction():
    out = build().render_private(withhold_concerns=True)
    assert CONCERN in out
    assert "Do not volunteer" in out


def test_withhold_concerns_false_renders_it_as_freely_sayable():
    out = build().render_private(withhold_concerns=False)
    assert CONCERN in out
    assert "Do not volunteer" not in out
    assert "freely" in out


def test_firmness_and_evidence_that_shifts_are_rendered_together():
    """Firmness alone is a wall. Paired with an exit condition it is a
    conviction that is defended but still falsifiable — the behaviour the
    premise validation actually rewarded."""
    out = build().render_private()
    assert "[firm]" in out
    assert "What would change your mind: an embedded index" in out


def test_formative_lesson_is_rendered_not_just_the_event():
    out = build().render_private()
    assert "Every extra service costs you users" in out


def test_defended_position_with_no_exit_condition_is_named_as_such():
    """An unfalsifiable position is a wall. Saying so beats the two alternatives:
    silent stonewalling, or the model inventing a condition it was never given."""
    sp = build(
        viewpoints=[
            Viewpoint(position="Security signs off first", firmness="firm",
                      evidence_that_shifts=[])
        ]
    )
    out = sp.render_private()
    assert "have not named anything that would change your mind" in out
    assert "do not invent a condition" in out


def test_negotiable_position_with_no_exit_condition_is_left_alone():
    """A negotiable position shifts on a good argument, so it needs no exit
    condition and the warning would be noise."""
    sp = build(viewpoints=[Viewpoint(position="Ship in Q3", firmness="negotiable")])
    assert "have not named anything" not in sp.render_private()


def test_holding_rule_forbids_fake_position_changes():
    out = build().render_private()
    assert "Never claim to have changed your mind while restating the same position" in out


def test_holding_rule_is_absent_when_there_are_no_viewpoints():
    sp = build(viewpoints=[])
    assert "How to hold those positions" not in sp.render_private()


# --------------------------------------------------------------------------
# The re-tuned dismissal rule (the experiment's explicit ship condition)
# --------------------------------------------------------------------------


def test_retuned_variant_is_retained_verbatim():
    """`retuned` is Phase 6 as first shipped, MEASURED at n=3 as suppressing
    dismissal to the control's rate. Its text is locked because editing it would
    silently invalidate a recorded negative result."""
    out = build().render_private(dismissal_rule="retuned")
    assert "not on your attention" in out
    assert "answer the factual or technical part of it directly" in out
    for forbidden in (
        "Do not repeat a dismissal you have already made",
        "Do not answer a challenge by restating your own position",
        "Never let declining to weigh something be your whole turn",
    ):
        assert forbidden in out, forbidden


def test_blunt_variant_is_arm_b_wording():
    """`blunt` exists to isolate the rule from the rendering: it holds the Phase 6
    rendering fixed and restores the wording that produced Arm B's dismissal rate.
    `off` cannot express this, because it drops the `dismisses` list too."""
    out = build().render_private(dismissal_rule="blunt")
    assert "Ignore the things you consider not your problem" in out
    assert "retrieval answer quality" in out
    # None of the retuned version's engagement clauses.
    assert "not on your attention" not in out


def test_mandatory_variant_requires_declining_rather_than_permitting_it():
    """The candidate re-tune. `retuned` said a persona *may* decline "once,
    briefly" — a permission is satisfiable by silence, and silence is what two of
    three measured runs produced."""
    out = build().render_private(dismissal_rule="mandatory")
    assert "MUST say plainly that it is not yours to weigh" in out
    assert "Every time it comes up" in out
    assert "not optional" in out


def test_mandatory_variant_still_requires_engaging_with_the_substance():
    """The retune must not become `blunt`. Arm C's monologues came from dropping
    the engagement requirement, so it has to survive."""
    out = build().render_private(dismissal_rule="mandatory")
    assert "engage with the substance anyway" in out
    assert "put their point in its strongest form" in out
    assert "not declining to think about it" in out


def test_mandatory_variant_keeps_only_the_prohibition_that_targets_arm_c():
    """Three prohibitions cut to one. The other two were aimed at the dismissal
    itself and are the likeliest suppressors; only the whole-turn one targets Arm
    C's measured failure (turns spent entirely declining)."""
    out = build().render_private(dismissal_rule="mandatory")
    assert "never let declining be your whole turn" in out
    assert "Do not repeat a dismissal" not in out
    assert "Do not answer a challenge by restating" not in out


def test_every_variant_names_the_dismissed_items():
    for variant in ("mandatory", "retuned", "blunt"):
        out = build().render_private(dismissal_rule=variant)
        assert "retrieval answer quality" in out, variant


def test_off_variant_drops_the_rule_and_the_list():
    out = build().render_private(dismissal_rule="off")
    assert "What you do not weigh" not in out
    assert "retrieval answer quality" not in out


def test_booleans_are_still_accepted_for_backward_compatibility():
    """Configs and stored snapshots written against the previous boolean field must
    keep working: True meant "render the rule", False meant "no rule, no list"."""
    from matrix_studio.personas import normalise_dismissal_rule

    assert normalise_dismissal_rule(True) == "mandatory"
    assert normalise_dismissal_rule(False) == "off"
    assert build().render_private(dismissal_rule=True) == build().render_private(
        dismissal_rule="mandatory"
    )
    assert build().render_private(dismissal_rule=False) == build().render_private(
        dismissal_rule="off"
    )


def test_unknown_variant_is_rejected_not_defaulted():
    """A typo'd variant silently falling back to the default would make a
    measurement arm quietly test the wrong wording — the exact class of error this
    re-tune exists to correct."""
    from matrix_studio.personas import normalise_dismissal_rule

    with pytest.raises(ValueError):
        normalise_dismissal_rule("gentle")
    with pytest.raises(ValueError):
        build().render_private(dismissal_rule="gentle")


def test_the_three_variants_are_genuinely_different_texts():
    """Guard against two variants collapsing into the same experiment."""
    texts = {v: build().render_private(dismissal_rule=v) for v in ("mandatory", "retuned", "blunt")}
    assert len(set(texts.values())) == 3


def test_no_dismissal_rule_when_nothing_is_dismissed():
    sp = build(preferences=PersonaPreferences(optimises_for=["speed"]))
    assert "What you do not weigh" not in sp.render_private()


# --------------------------------------------------------------------------
# Off means untouched
# --------------------------------------------------------------------------


def test_disabled_returns_the_prose_unchanged():
    prose = "Pragmatic head of distribution. Wary of anything that complicates deployment."
    assert effective_persona(prose, build(), enabled=False) == prose
    assert public_persona(prose, build(), enabled=False) == prose


def test_no_structured_data_returns_the_prose_unchanged():
    prose = "Pragmatic head of distribution."
    assert effective_persona(prose, None) == prose
    assert public_persona(prose, None) == prose


def test_empty_structured_block_is_treated_as_absent():
    """A cast member declaring `"structured": {}` must get byte-identical
    prompts to one that omits the key."""
    assert parse_structured({}) is None
    assert parse_structured(None) is None
    assert parse_structured("nonsense") is None
    assert parse_structured({"role": "", "viewpoints": []}) is None


def test_effective_persona_appends_rather_than_replaces():
    prose = "Pragmatic head of distribution."
    out = effective_persona(prose, build())
    assert out.startswith(prose)
    assert len(out) > len(prose)


def test_empty_prose_does_not_leave_leading_blank_lines():
    out = effective_persona("", build())
    assert not out.startswith("\n")


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_unknown_firmness_is_rejected_not_silently_downgraded():
    """A typo'd firmness silently becoming `negotiable` would quietly remove the
    defence this feature exists to add."""
    with pytest.raises(ValidationError):
        Viewpoint(position="x", firmness="non_negotiable")
    with pytest.raises(ValidationError):
        Viewpoint(position="x", firmness="VERY FIRM")


def test_every_declared_firmness_level_is_accepted():
    for level in FIRMNESS_LEVELS:
        assert Viewpoint(position="x", firmness=level).firmness == level


def test_is_defended_only_for_firm_and_above():
    assert not Viewpoint(position="x", firmness="negotiable").is_defended
    assert Viewpoint(position="x", firmness="firm").is_defended
    assert Viewpoint(position="x", firmness="non-negotiable").is_defended
    assert Viewpoint(position="x", firmness="requires-escalation").is_defended


def test_requires_escalation_gives_an_alternative_to_agreeing():
    """The one firmness level that hands a persona something honest to do other
    than agree or repeat itself — the corner Arm C's personas got stuck in."""
    sp = build(
        viewpoints=[Viewpoint(position="Security signs off first", firmness="requires-escalation")]
    )
    out = sp.render_private()
    assert "[requires-escalation]" in out
    assert "do not have the authority to concede" in out
    assert "take it further" in out


def test_parse_structured_raises_on_bad_contents():
    """Invalid contents are NOT swallowed: silently dropping a persona's
    convictions looks exactly like the feature not working."""
    with pytest.raises(ValidationError):
        parse_structured({"viewpoints": [{"position": "x", "firmness": "rock solid"}]})


def test_parse_structured_ignores_unknown_keys():
    sp = parse_structured({"role": "Cost", "not_a_field": 1})
    assert sp is not None and sp.role == "Cost"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_persona_config_defaults_to_off():
    cfg = PersonaConfig()
    assert cfg.enabled is False
    assert cfg.withhold_concerns is True
    assert cfg.dismissal_rule == "mandatory"


def test_persona_config_coerces_and_validates_the_rule_variant():
    assert PersonaConfig(dismissal_rule=True).dismissal_rule == "mandatory"
    assert PersonaConfig(dismissal_rule=False).dismissal_rule == "off"
    assert PersonaConfig(dismissal_rule="blunt").dismissal_rule == "blunt"
    with pytest.raises(ValidationError):
        PersonaConfig(dismissal_rule="gentle")


def test_persona_config_from_missing_or_invalid_block():
    assert PersonaConfig.from_config(None).enabled is False
    assert PersonaConfig.from_config({}).enabled is False
    assert PersonaConfig.from_config({"personas": "yes"}).enabled is False


def test_persona_config_ignores_unknown_keys():
    cfg = PersonaConfig.from_config({"personas": {"enabled": True, "bogus": 3}})
    assert cfg.enabled is True


# --------------------------------------------------------------------------
# AgentState integration
# --------------------------------------------------------------------------


def test_agent_state_structured_defaults_to_none_so_old_snapshots_parse():
    agent = AgentState(name="Dana", persona="Prose.")
    assert agent.structured is None
    # A stored pre-Phase-6 snapshot has no `structured` key at all.
    assert AgentState.model_validate({"name": "Dana", "persona": "P"}).structured is None


def test_agent_state_round_trips_structured_through_json():
    agent = AgentState(name="Dana", persona="Prose.", structured=build())
    restored = AgentState.model_validate_json(agent.model_dump_json())
    assert restored.structured is not None
    assert restored.structured.viewpoints[0].firmness == "firm"
    # The private fields survive a snapshot round-trip — they must, or a resumed
    # run would lose the concern it was withholding.
    assert restored.structured.viewpoints[0].underlying_concern == CONCERN
    assert restored.structured.viewpoints[0].validity == VALIDITY_NOTE


def test_public_summary_is_one_line():
    """It rides the moderator prompt on every turn, once per cast member."""
    assert "\n" not in build().render_public()
