# SPDX-License-Identifier: Apache-2.0
"""
Per-role model selection.

A run makes six kinds of model call and they want different things. The design is in
`matrix_studio/models.py`; what is asserted here is the part that is easy to get wrong,
and one of these tests exists because I *did* get it wrong.

**The load-bearing rule: an explicit `model` applies to EVERY role.** The first version let
the per-role defaults override it for the temperature-sensitive roles, reasoning that
Sonnet 5 silently drops a temperature and the validation gate must be deterministic.
`test_engine_uses_per_run_config_model` failed, correctly: quietly not honouring a model
somebody named is a worse failure than the one being avoided, because there is no way to
discover it. The defaults are a posture for when nothing is configured.
"""

import pytest

from matrix_studio.models import (
    LOW_VARIANCE_MODEL,
    ROLE_DEFAULTS,
    ROLES,
    ModelSet,
    model_for,
)

OPUS = "bedrock/global.anthropic.claude-opus-5"


# --------------------------------------------------------------------------- #
# An explicit model wins everywhere
# --------------------------------------------------------------------------- #


class TestAnExplicitModelWinsEverywhere:
    def test_a_conversation_model_applies_to_every_role(self):
        """The rule a test failure taught. Every role, including the pinned ones."""
        resolved = ModelSet.from_config({"model": OPUS}).as_dict()
        assert set(resolved.values()) == {OPUS}, resolved

    def test_including_the_temperature_sensitive_roles(self):
        """Named separately, because these are exactly the ones an earlier version
        withheld — and a test over all roles could pass while these regressed if the
        defaults ever became the same model as the conversation one."""
        ms = ModelSet.from_config({"model": OPUS})
        for role in ROLE_DEFAULTS:
            assert ms.resolve(role) == OPUS, role

    def test_a_per_role_override_beats_the_conversation_model(self):
        ms = ModelSet.from_config({"model": OPUS, "models": {"validation": "x/cheap"}})
        assert ms.resolve("validation") == "x/cheap"
        assert ms.resolve("voice") == OPUS


# --------------------------------------------------------------------------- #
# The deployment posture, when nothing is configured
# --------------------------------------------------------------------------- #


class TestTheDefaultPosture:
    def test_the_gate_and_selection_get_a_temperature_honouring_model(self):
        """Sonnet 5 accepts only temperature=1 and `drop_params` discards the rest, so a
        gate set to 0.0 would silently run at 1.0 — and a gate that answers differently on
        the same turn is not a gate."""
        ms = ModelSet()
        assert ms.resolve("validation") == LOW_VARIANCE_MODEL
        assert ms.resolve("speaker_selection") == LOW_VARIANCE_MODEL

    def test_the_voice_and_summary_follow_the_deployment_default(self):
        """`None` means "whatever the settings say", which is the conversation model."""
        ms = ModelSet()
        assert ms.resolve("voice") is None
        assert ms.resolve("summary") is None

    def test_the_summary_is_NOT_pinned_cheap(self):
        """It runs ONCE per run over the whole transcript and a human reads the result, so
        the frequency argument that makes selection cheap points the other way here.
        Asserted because "make the big output cheap" is the intuitive wrong answer."""
        assert "summary" not in ROLE_DEFAULTS

    def test_every_role_resolves_to_something(self):
        from matrix_studio.settings import get_settings

        plan = ModelSet().as_dict()
        assert set(plan) == set(ROLES)
        assert all(plan.values()), plan
        assert plan["voice"] == get_settings().litellm_model


# --------------------------------------------------------------------------- #
# Bad input
# --------------------------------------------------------------------------- #


class TestBadInput:
    def test_an_unknown_role_in_config_is_dropped_with_a_warning(self, caplog):
        """Not raised: a config written against a newer version must not stop an older
        deployment from running. Not silent either — a typo would otherwise mean somebody's
        choice never applied and nothing said so."""
        with caplog.at_level("WARNING"):
            ms = ModelSet.from_config({"models": {"voise": OPUS}})
        assert ms.roles == {}
        assert "voise" in caplog.text and "unknown role" in caplog.text

    def test_an_empty_model_string_is_ignored(self):
        assert ModelSet.from_config({"models": {"voice": "   "}}).roles == {}

    def test_a_non_mapping_models_value_does_not_raise(self):
        assert ModelSet.from_config({"models": "sonnet"}).roles == {}
        assert ModelSet.from_config({"models": None}).roles == {}

    def test_an_unknown_role_at_a_CALL_SITE_raises(self):
        """The opposite direction from config, deliberately. A typo in a call site is a
        programming error, and returning the settings default would look correct."""
        with pytest.raises(ValueError, match="unknown model role"):
            model_for(OPUS, "voise")


# --------------------------------------------------------------------------- #
# Backwards compatibility
# --------------------------------------------------------------------------- #


class TestAPlainStringStillWorks:
    def test_a_string_means_that_model_for_every_role(self):
        """Every existing call site and test passes a string. It has to keep meaning what
        it meant before roles existed."""
        for role in ROLES:
            assert model_for("some/model", role) == "some/model"

    def test_none_falls_through_to_the_role_default(self):
        assert model_for(None, "validation") == LOW_VARIANCE_MODEL
        assert model_for(None, "voice") is None
