# SPDX-License-Identifier: Apache-2.0
"""
Which model serves which role in a run, and why they are not all the same one.

A run makes six kinds of model call, and they differ enough that one setting for all of
them is a compromise in both directions — paying frontier prices to pick a name from a
list, while a cheap model judges whether a turn violated the priority hierarchy.

## The reason this is not merely a cost optimisation

**Sonnet 5 accepts only `temperature=1`.** That is measured, not assumed
(`matrix_studio/lazy_litellm.py` records the incident: every call raised
`UnsupportedParamsError` and the engine wrote the error text into the transcript as the
character's speech). `drop_params=True` now makes such a call succeed by silently
discarding the temperature.

Silently is the problem. Two calls set a low temperature deliberately:

    speaker selection   temperature=0.3   "Lower temperature for more consistent selection"
    validation gate     temperature=0.0   a gate that must give the same verdict twice

Run those on Sonnet 5 and both jump to 1.0 with nothing logged. The gate becomes
non-deterministic — it is the thing deciding whether to regenerate a turn — and selection
becomes streakier.

So the per-role defaults below are a posture rather than a tuning preference: with nothing
configured, the gate and selection get a model that honours their temperature. They do NOT
override a model somebody explicitly asked for — see `resolve` for why that distinction
cost a test failure to get right — and the residual risk is handled by logging the plan.

## The roles

| role | output | temperature | frequency | wants |
|---|---|---|---|---|
| `voice` | ≤2048 tokens, in character | 0.7 | once per turn | the best model — this is the product |
| `speaker_selection` | one name, ≤120 tokens | 0.3 | once per turn | consistency and low cost |
| `validation` | ≤50 tokens, pass/fail | **0.0** | once per turn, plus retries | determinism above all |
| `reflection` | one sentence, ≤120 tokens | 0.7 | every N turns | cheap |
| `summary` | ≤16000 tokens over the whole transcript | 0.3 | **once per run** | strong — a human reads this |
| `aside` | conversational answer | 0.3–0.6 | user-initiated | matches `summary` |
| `naming` | two words, ≤60 tokens | 0.9 | once per run | cheapest available |
| `wizard` | ≤16000 tokens, drafts a cast | 1.0 | once, pre-run | strong — authoring quality |
| `pressure` | ≤300 tokens | 0.7 | experimental | follows the conversation model |
| `stance` | a class and a quoted sentence per persona, ≤3000 tokens | **0.0** | once per summary | the same verdict twice |

Note what the frequency column does to the cost argument. `summary`, `naming` and `wizard`
run **once**, so the model chosen for them barely moves the bill — which is why `summary`
defaults to the strong model rather than a cheap one, despite being the largest single
output. Conversely `speaker_selection` and `validation` run every turn, and their outputs
are 120 and 50 tokens, so a frontier model buys nothing measurable.

## Resolution order

    config["models"][role]   an explicit per-role choice
    config["model"]          the conversation's model — applies to EVERY role
    ROLE_DEFAULTS[role]      the deployment posture, when no model was named at all
    settings.litellm_model   the deployment default

A caller who sets nothing gets the table above: the conversation model for the roles where
quality is the product, and a temperature-honouring model for the gate, selection and
naming. A caller who sets `model` gets **that model everywhere** — the defaults are a
posture, not a veto over what somebody asked for, and quietly substituting a model nobody
named is a worse failure than the temperature one it would avoid. A caller who wants both
sets `models.validation` explicitly.

The temperature risk is therefore made visible rather than prevented: `log_plan` writes
the resolved role→model map once per run, so "why is my gate non-deterministic" is one log
line away instead of buried in this file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Union

logger = logging.getLogger(__name__)

#: The cheap, temperature-honouring model the low-variance roles default to.
#:
#: Named rather than inlined so there is one place to change when a cheaper or newer
#: small model appears, and so a test can assert the temperature-sensitive roles do not
#: silently follow a conversation model that drops temperature.
LOW_VARIANCE_MODEL = "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0"

ROLES = (
    "voice",
    "speaker_selection",
    "validation",
    "reflection",
    "summary",
    "aside",
    "naming",
    "wizard",
    "pressure",
    "stance",
)

#: Roles that do NOT inherit the conversation's model, and why each one does not.
#:
#: Deliberately short. A role belongs here only when inheriting would break something
#: stated elsewhere — not because a cheaper model would do. Anything else follows the
#: conversation model, so "set `model` and everything uses it" stays true for the roles
#: where it is what a caller means.
ROLE_DEFAULTS: Dict[str, str] = {
    # temperature=0.0. A gate that returns a different verdict on the same turn is not a
    # gate, and Sonnet 5 drops the 0.0.
    "validation": LOW_VARIANCE_MODEL,
    # temperature=0.3, described in the code as "lower temperature for more consistent
    # selection". Also the highest-frequency call with the smallest output.
    "speaker_selection": LOW_VARIANCE_MODEL,
    # Two words. There is nothing a larger model can add.
    "naming": LOW_VARIANCE_MODEL,
    # temperature=0.0, for the gate's reason: a regenerated summary re-reads the same closing statements,
    # and a stance that flips on the re-read is noise on the run card (`stance.py`); Sonnet 5 would drop
    # the 0.0. Four classes and a quote over a few short statements is not where a frontier model earns
    # its price, and the quote is checked in code rather than trusted. On four live statements Haiku
    # matched the hand labels 4/4, identically on three repeats, at ~$0.003 a call; Sonnet 5 also 4/4, at
    # ~$0.013. A smoke check, not a measurement (docs/MOBILE-UI.md §6.1).
    "stance": LOW_VARIANCE_MODEL,
}


@dataclass(frozen=True)
class ModelSet:
    """The models a run should use, by role.

    Passed where a plain model string used to be. Every call site resolves through
    `model_for`, which accepts a string, a `ModelSet` or `None` — so a caller that still
    passes a string keeps working and gets that string for every role.
    """

    default: Optional[str] = None
    roles: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: Optional[Mapping[str, Any]]) -> "ModelSet":
        """Build from a run's stored config: `model` plus an optional `models` map.

        An unknown role name is dropped with a warning rather than silently ignored or
        raised on. Silently ignoring it means somebody's typo means their choice never
        applied and nothing said so; raising means a config written against a newer
        version stops an older deployment from running at all.
        """
        config = config or {}
        roles: Dict[str, str] = {}
        raw = config.get("models")
        if isinstance(raw, Mapping):
            for role, model in raw.items():
                role, model = str(role), str(model or "").strip()
                if not model:
                    continue
                if role not in ROLES:
                    logger.warning(
                        "Ignoring model override for unknown role %r (known roles: %s). "
                        "Nothing will use it.", role, ", ".join(ROLES),
                    )
                    continue
                roles[role] = model
        default = config.get("model")
        return cls(default=str(default) if default else None, roles=roles)

    def resolve(self, role: str) -> Optional[str]:
        """This role's model, or None to mean "the settings default".

        **An explicit conversation model applies to every role**, and an earlier version
        of this did not — it let `ROLE_DEFAULTS` override `model` for the
        temperature-sensitive roles, on the grounds that Sonnet 5 silently drops a
        temperature. A test caught it: `test_engine_uses_per_run_config_model` asserts
        that "the engine honours a per-run config model", and quietly not honouring it
        for two of six roles is a worse failure than the one being avoided. Somebody who
        names a model and gets a different one has no way to discover why.

        So the ROLE_DEFAULTS are the DEPLOYMENT's posture — what you get with nothing
        configured — rather than a veto over what a caller asked for. The temperature risk
        is made visible instead, by `log_plan` writing the resolved map once per run.
        """
        if role in self.roles:
            return self.roles[role]
        if self.default:
            return self.default
        return ROLE_DEFAULTS.get(role)

    def log_plan(self) -> None:
        """Log what each role resolved to, once, at the start of a run.

        This is what makes the design safe rather than merely defensible. Two roles set a
        low temperature on purpose and some models discard it, so the combination that
        matters — role, model, temperature — is only inspectable if something writes it
        down. One INFO line per run is cheap; silence is what costs an afternoon.
        """
        plan = self.as_dict()
        distinct = sorted(set(plan.values()))
        if len(distinct) == 1:
            logger.info("Models: %s for every role", distinct[0])
        else:
            logger.info(
                "Models by role: %s",
                ", ".join(f"{role}={model}" for role, model in sorted(plan.items())),
            )

    def as_dict(self) -> Dict[str, Optional[str]]:
        """Every role and what it will actually use. For `/api/models` and for logging.

        Worth exposing: the per-role defaults are the surprising part of this design, and
        an operator who cannot see them will assume `model` applied everywhere.
        """
        from matrix_studio.settings import get_settings

        fallback = get_settings().litellm_model
        return {role: self.resolve(role) or fallback for role in ROLES}


def model_for(model: Union[str, "ModelSet", None], role: str) -> Optional[str]:
    """The model for ``role``, given whatever the caller was handed.

    Accepts a plain string so every existing call site and test keeps working: a string
    means "this model, for everything", which is what it meant before roles existed.
    """
    if role not in ROLES:
        # A typo in a call site, which is a programming error rather than a config one —
        # and one that would otherwise resolve to the settings default and look fine.
        raise ValueError(f"unknown model role {role!r}; known roles: {', '.join(ROLES)}")
    if isinstance(model, ModelSet):
        return model.resolve(role)
    if model:
        return str(model)
    return ROLE_DEFAULTS.get(role)
