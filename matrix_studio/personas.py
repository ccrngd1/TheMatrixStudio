# SPDX-License-Identifier: Apache-2.0
"""Phase 6: structured personas — convictions, not just goals.

## Why this exists

`docs/PHASE5-PREMISE-VALIDATION.md` ran a three-arm experiment and concluded
**"PROCEED with structured personas (Arm B)"**. Arm B was the only arm that
improved on both axes at once: it cut the accommodation rate 40%, raised the
dismissal rate five-fold, produced the lowest cross-speaker similarity *and* the
best engagement score, and was the only arm in which any participant changed
position. That verdict sat unbuilt while Phase 5 went to document retrieval.

The gap it fills: the engine has ``goals``, and goals are *satisfiable*. A
persona with a goal can be talked into a plan that satisfies it. It has no axis
for **what I believe and will not give up** — so the default failure mode of a
multi-agent discussion is everyone converging politely on the first synthesis
anyone proposes. Convictions are defended; goals are traded.

## What is modelled

Straight from the ``stakeholder-review-panel`` spec that motivated the
experiment, with the field names adapted to this codebase's conventions:

- ``background`` — tenure, prior roles, and **formative events** with the lesson
  each one taught. A position with a history behind it is harder to abandon than
  a bare assertion.
- ``preferences`` — ``optimises_for``, ``dismisses``, ``persuaded_by``.
  ``dismisses`` is the field the experiment showed doing the most real work:
  "that's yours to own" is a behaviour the control arm essentially never produced.
- ``viewpoints`` — each with ``position``, ``formed_by``, ``firmness``,
  ``evidence_that_shifts``, and a withheld ``underlying_concern``.

## Two renderings, deliberately

``render_private()`` goes into the speaker's own system prompt.
``render_public()`` is what the *moderator* sees when choosing who speaks next.

They differ because ``underlying_concern`` is **withheld by definition**: per the
source spec, drawing out the real concern behind a stated position is the skill
being exercised. Leaking it into the moderator's persona list would put it one
prompt away from every other participant and destroy that. So the split is a
correctness requirement, not a token optimisation, and
``tests/test_personas.py`` locks it.

## The dismissal rule has two measured failure modes

`dismisses` is the highest-value field and the hardest to word, because both ways
of getting it wrong are now measured:

| Wording | Result |
|---|---|
| **blunt** — *"Ignore the things you consider not your problem"* (Arm C) | Dismissals fire reliably, but the discussion collapses into parallel monologues: talking-past **4**/5, within-speaker similarity up 37% |
| **retuned** — Phase 6 as first shipped | Talking-past back to 2, but dismissal **suppressed to 0.067** across three runs — the *control's* rate — with two runs producing none at all |
| **blunt, rendered** — the same wording through this renderer | **0.000 across three runs.** Arm B's prose gets 0.355 from these exact words |
| **mandatory** — the shipped default | **0.333** dismissal (matching Arm B) with talking-past **1.00**, the best engagement score of any arm |

Those four points give one rule, and it generalises past this field:

> **A rendered instruction must REQUIRE AN UTTERANCE, not license an omission.**

``blunt`` and ``retuned`` both *permit* declining; ``mandatory`` demands it. Arm B
got away with a permission only because its hand-written prose wrapped that
sentence in a block of conduct imperatives, which supplied force the sentence
lacks on its own.

So the rule is a **named variant** (``DISMISSAL_RULES``), not a boolean, and both
failing wordings are retained verbatim so their negative results stay reproducible.
The criterion was pre-registered before any wording existed:
``docs/PHASE6-DISMISSAL-RETUNE.md``.

Pure functions and pydantic models only: no LLM, no database, no state.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

# How firmly a viewpoint is held. Ordered weakest to strongest.
#
# ``requires-escalation`` is not simply "very firm": it says the speaker lacks
# the AUTHORITY to concede, so being overruled produces "I'll have to take this
# further", not agreement. It is included because the validated Arm B cast used
# it, and because it is the one firmness level that gives a persona something
# honest to do other than agree or repeat itself — which is exactly the corner
# Arm C's personas got stuck in.
FIRMNESS_LEVELS = ("negotiable", "firm", "non-negotiable", "requires-escalation")

# Firmness levels that require named evidence before the position may move.
# ``negotiable`` positions are allowed to shift on a good argument alone.
DEFENDED_FIRMNESS = ("firm", "non-negotiable", "requires-escalation")


class FormativeEvent(BaseModel):
    """One thing that happened to this persona, and what it taught them.

    The ``lesson`` is the load-bearing half. An event alone is colour; an event
    plus the generalisation drawn from it is a reason to hold a position when the
    room disagrees.
    """

    type: str = Field(default="FormativeEvent", description="Type discriminator")
    year: Optional[int] = Field(default=None, description="When it happened")
    event: str = Field(description="What happened")
    lesson: str = Field(default="", description="What the persona took from it")


class PersonaBackground(BaseModel):
    """Where this persona's judgment came from."""

    type: str = Field(default="PersonaBackground", description="Type discriminator")
    tenure_years: Optional[int] = Field(default=None, description="Years in this kind of work")
    prior_roles: List[str] = Field(default_factory=list, description="Previous roles")
    formative_events: List[FormativeEvent] = Field(
        default_factory=list, description="Events that shaped their judgment"
    )

    def is_empty(self) -> bool:
        return not (self.tenure_years or self.prior_roles or self.formative_events)


class PersonaPreferences(BaseModel):
    """What this persona weighs, refuses to weigh, and can be moved by.

    ``dismisses`` is the highest-value field here per the premise validation, and
    also the most dangerous: Arm C showed that an unqualified "ignore these"
    instruction turns a persona into a broken record. See the module docstring —
    the rendered rule constrains *priorities*, never *attention*.
    """

    type: str = Field(default="PersonaPreferences", description="Type discriminator")
    optimises_for: List[str] = Field(default_factory=list, description="What they trade everything for")
    dismisses: List[str] = Field(
        default_factory=list, description="Concerns they decline to weigh (not to engage with)"
    )
    persuaded_by: List[str] = Field(default_factory=list, description="What actually moves them")

    def is_empty(self) -> bool:
        return not (self.optimises_for or self.dismisses or self.persuaded_by)


class Viewpoint(BaseModel):
    """One position this persona holds, with its provenance and its exit condition.

    ``firmness`` and ``evidence_that_shifts`` are rendered **together**, always.
    Firmness alone produces a wall; pairing it with what would move the position
    makes it a conviction that is defended but still falsifiable — which is the
    behaviour the experiment actually rewarded (Arm B was the only arm where
    anyone changed position).
    """

    type: str = Field(default="Viewpoint", description="Type discriminator")
    position: str = Field(description="The position, as they would state it out loud")
    underlying_concern: str = Field(
        default="",
        description="The real worry behind it. WITHHELD unless asked — never rendered publicly.",
    )
    formed_by: str = Field(default="", description="The experience that produced this position")
    firmness: str = Field(
        default="negotiable", description="negotiable | firm | non-negotiable"
    )
    evidence_that_shifts: List[str] = Field(
        default_factory=list, description="What would genuinely change their mind"
    )
    # AUTHORING/SCORING NOTE ONLY. Never rendered into any prompt, public or
    # private: telling a persona its own position is "outdated" would collapse
    # the exercise. It exists so an operator can calibrate a panel (are the
    # firmest positions also the soundest? they should not be) and so post-run
    # scoring can ask whether sound positions were conceded and unsound ones
    # pushed back on. `tests/test_personas.py` asserts it never leaks.
    validity: Optional[str] = Field(
        default=None,
        description="Operator calibration note (e.g. sound|outdated|misapplied|overgeneralised). NEVER rendered.",
    )

    @field_validator("firmness")
    @classmethod
    def _check_firmness(cls, v: str) -> str:
        """Reject an unknown firmness rather than silently treating it as weak.

        Silently downgrading a typo'd ``"non_negotiable"`` to ``negotiable``
        would quietly remove the defence this whole feature exists to add.
        """
        if v not in FIRMNESS_LEVELS:
            raise ValueError(f"firmness must be one of {list(FIRMNESS_LEVELS)}, got {v!r}")
        return v

    @property
    def is_defended(self) -> bool:
        return self.firmness in DEFENDED_FIRMNESS


class StructuredPersona(BaseModel):
    """The structured half of a persona definition.

    Additive to the existing ``persona`` prose string, never a replacement: the
    prose carries voice and manner, which structure is bad at, and the structure
    carries commitments, which prose is bad at. A cast member may supply either
    or both.
    """

    type: str = Field(default="StructuredPersona", description="Type discriminator")
    schema_version: str = Field(default="1.0.0", description="Schema version for migration")
    role: str = Field(default="", description="Their job, as the room would describe it")
    background: PersonaBackground = Field(default_factory=PersonaBackground)
    preferences: PersonaPreferences = Field(default_factory=PersonaPreferences)
    viewpoints: List[Viewpoint] = Field(default_factory=list)

    def is_empty(self) -> bool:
        """Whether there is nothing here worth rendering.

        Checked before rendering so a cast member that declares ``structured``
        as an empty object gets byte-identical prompts to one that omits it —
        the same "off means untouched" property ``CognitionConfig`` and
        ``RetrievalConfig`` hold.
        """
        return not (
            self.role
            or self.viewpoints
            or not self.background.is_empty()
            or not self.preferences.is_empty()
        )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render_private(
        self, *, withhold_concerns: bool = True, dismissal_rule: Any = "mandatory"
    ) -> str:
        """The block appended to this persona's OWN system prompt.

        Second person, because it is read by the model as instructions about
        itself. Returns ``""`` when there is nothing to say, so callers can
        concatenate unconditionally.
        """
        if self.is_empty():
            return ""

        parts: List[str] = []

        if self.role:
            parts.append(f"Your role: {self.role}")

        bg = self.background
        if bg.tenure_years or bg.prior_roles:
            bits = []
            if bg.tenure_years:
                bits.append(f"{bg.tenure_years} years in this kind of work")
            if bg.prior_roles:
                bits.append("previously " + "; ".join(bg.prior_roles))
            parts.append("Your background: " + ", ".join(bits) + ".")

        if bg.formative_events:
            lines = []
            for ev in bg.formative_events:
                stamp = f"{ev.year}: " if ev.year else ""
                line = f"- {stamp}{ev.event}"
                if ev.lesson:
                    line += f"\n  What you took from it: {ev.lesson}"
                lines.append(line)
            parts.append("What shaped your judgment:\n" + "\n".join(lines))

        pref = self.preferences
        if pref.optimises_for:
            parts.append(
                "What you optimise for, in order: " + "; ".join(pref.optimises_for) + "."
            )
        if pref.persuaded_by:
            parts.append("What actually moves you: " + "; ".join(pref.persuaded_by) + ".")

        if self.viewpoints:
            lines = []
            for i, vp in enumerate(self.viewpoints, start=1):
                lines.append(f"{i}. [{vp.firmness}] {vp.position}")
                if vp.formed_by:
                    lines.append(f"   How you came to it: {vp.formed_by}")
                if vp.evidence_that_shifts:
                    lines.append(
                        "   What would change your mind: "
                        + "; ".join(vp.evidence_that_shifts)
                    )
                elif vp.is_defended:
                    # A defended position with no named exit condition is an
                    # unfalsifiable wall, and the observed cast has one (a
                    # `requires-escalation` viewpoint with an empty list). Silence
                    # here lets the model either stonewall or invent a condition it
                    # was never given; saying it plainly does neither, and it
                    # surfaces the authoring gap in the transcript where an
                    # operator will actually see it.
                    lines.append(
                        "   You have not named anything that would change your mind on "
                        "this. If you are pressed, say that — do not invent a condition "
                        "you do not have."
                    )
            parts.append("Positions you hold:\n" + "\n".join(lines))
            parts.append(_HOLDING_RULE)

        # The withheld concern. Rendered LAST and with its own instruction, so
        # the "do not volunteer this" framing is the nearest context to the
        # content itself rather than paragraphs away from it.
        concerns = [(i, vp.underlying_concern) for i, vp in enumerate(self.viewpoints, start=1)
                    if vp.underlying_concern]
        if concerns:
            lines = "\n".join(f"{i}. {c}" for i, c in concerns)
            if withhold_concerns:
                parts.append(
                    "What is REALLY behind those positions (numbered to match):\n"
                    f"{lines}\n"
                    "Do not volunteer any of this. State the position, not the worry "
                    "underneath it. Say the real reason only if someone asks you why you "
                    "hold the position, or presses you past your surface argument — and "
                    "then say it plainly, in your own words."
                )
            else:
                parts.append(
                    "Why you hold those positions (numbered to match); you may say "
                    f"any of this freely:\n{lines}"
                )

        if pref.dismisses:
            rule = _dismissal_rule(pref.dismisses, normalise_dismissal_rule(dismissal_rule))
            if rule:
                parts.append(rule)

        return "\n\n" + "\n\n".join(parts)

    def render_public(self) -> str:
        """A one-line summary for the moderator's persona list.

        Carries only what the persona says out loud anyway — role and what it
        optimises for. Never ``underlying_concern`` (withheld by design; see the
        module docstring) and never ``validity`` (an operator's private note).
        Kept to one line because the moderator prompt grows with cast size and
        it runs on every turn.
        """
        bits = []
        if self.role:
            bits.append(self.role)
        if self.preferences.optimises_for:
            bits.append("cares most about " + ", ".join(self.preferences.optimises_for))
        return "; ".join(bits)


# The conviction rule. Every clause here targets an observed failure:
#
# - "not preferences to be traded" / "do not soften": the 0.667 accommodation
#   rate in the control arm — personas converging on whatever synthesis is on
#   the table.
# - "only moves if something on its list shows up": the experiment found ZERO
#   position changes driven by new evidence in any arm. Naming the exit
#   condition is what makes an evidence-driven change possible at all.
# - "never claim to have changed your mind while restating the same position":
#   the cheap way a model satisfies both "be agreeable" and "hold your position"
#   at once, which reads as agreement and measures as agreement while conceding
#   nothing.
_HOLDING_RULE = (
    "How to hold those positions:\n"
    "- They are convictions, not preferences to be traded for agreement. Do not drop "
    "one because the room is leaning the other way, and do not soften it into "
    "something everyone can accept.\n"
    "- A [firm] or [non-negotiable] position moves only if something on its "
    "\"what would change your mind\" list actually turns up in the conversation. If "
    "that happens, say what changed and move — that is not a loss.\n"
    "- A [negotiable] position can shift on a good argument alone. Say what "
    "persuaded you.\n"
    "- A [requires-escalation] position is one you do not have the authority to "
    "concede. If the room decides against it, do not agree — say plainly that you "
    "will have to take it further, and say who to.\n"
    "- Never claim to have changed your mind while restating the same position."
)


# Dismissal-rule variants. This is a named choice rather than on/off because the
# two failure modes are measured and sit on either side of the target, and
# separating "the wording is wrong" from "the rendering is wrong" requires
# emitting different wordings against the same structured data:
#
#   blunt     Arm C / Arm B's wording. Dismissal rate 0.467 (C) but the discussion
#             collapsed into parallel monologues, talking-past 4/5.
#   retuned   Phase 6 as first shipped. Talking-past back to 2, but dismissal
#             suppressed to 0.067 across three runs — the CONTROL's rate — with two
#             runs containing none at all.
#   mandatory The SHIPPED default. Measured at n=3: dismissal 0.333 (matching Arm B's
#             0.355) with talking-past 1.00 — the best engagement score of any arm.
#             Pre-registered criterion, both conditions passed:
#             docs/PHASE6-DISMISSAL-RETUNE.md.
#
# `retuned` and `blunt` are kept verbatim so both negative results stay reproducible;
# tests lock their text for the same reason.
#
# THE GENERAL LESSON, measured across four data points and worth applying to any
# rendered persona instruction that wants a VISIBLE behaviour:
#
#   The instruction must REQUIRE AN UTTERANCE, not license an omission.
#
#     blunt     "Ignore the things you consider not your problem"   -> 0.000 rendered
#     retuned   "say once, briefly, that it is not yours to weigh"  -> 0.067
#     mandatory "you MUST say plainly ... every time ... not optional" -> 0.333
#
# Arm B got away with `blunt` at 0.355 only because its hand-written prose put that
# sentence inside a five-item HOW YOU BEHAVE block of conduct imperatives, which
# supplied the mandatory force the sentence itself lacks. Rendered without that
# frame, the permission reading wins and the model defaults to silence.
DISMISSAL_RULES = ("mandatory", "retuned", "blunt", "off")

# Accepted for backward compatibility with the boolean field this replaced.
_BOOL_RULES = {True: "mandatory", False: "off"}


def _rule_blunt(items: str) -> str:
    """Arm B / Arm C's wording. RETAINED VERBATIM — do not edit.

    Reliable in Arm B's hand-written prose (0.355) and a total failure through this
    renderer: **0.000 across three runs**, zero dismissal idiom of any form. It is
    a permission to not engage, and rendered without Arm B's surrounding conduct
    imperatives the permission reading wins.

    Kept because it is the isolation arm — it is what proved the rendering is not a
    blanket blocker (``mandatory`` works through it) while Arm B's exact wording is.
    """
    return (
        f"What you do not weigh: {items}.\n"
        "Judge every proposal only against what you optimise for. Ignore the things "
        "you consider not your problem, even when they are objectively valid concerns."
    )


def _rule_retuned(items: str) -> str:
    """Phase 6 as first shipped. RETAINED VERBATIM — do not edit.

    Measured at n = 3 as suppressing dismissal to 0.067 (two runs of three produced
    none). Kept so that result stays reproducible, and locked by a test: editing
    this text would silently invalidate a recorded measurement.

    The diagnosis, for reference: one clause instructs the persona to decline and
    six tell it to engage or constrain how it declines, three of those being
    prohibitions aimed at the dismissal rather than at the evasion. The single
    permission — "say once, briefly" — is also the weakest phrasing here.
    """
    return (
        f"What you do not weigh: {items}.\n"
        "That is a limit on your PRIORITIES, not on your attention. When someone "
        "argues from one of those concerns you must still engage with what they "
        "actually said: answer the factual or technical part of it directly, and put "
        "their point in its strongest form before you set it aside. Then say once, "
        "briefly, that it is not yours to weigh — and move on to what is.\n"
        "Do not repeat a dismissal you have already made. Do not answer a challenge "
        "by restating your own position. Never let declining to weigh something be "
        "your whole turn."
    )


def _rule_mandatory(items: str) -> str:
    """The shipped default. Four changes from ``retuned``, each targeting the
    measured cause, and it passed a pre-registered two-condition criterion at n=3
    (dismissal 0.333 vs Arm B's 0.355; talking-past 1.00, the best of any arm):

    1. **Declining is REQUIRED, not permitted.** ``retuned`` says a persona "may"
       decline once, briefly; a permission is satisfiable by silence, and silence is
       what two of three runs produced. This says "you must say so, every time".
    2. **Three prohibitions cut to one.** Only the whole-turn prohibition targets
       Arm C's actual failure (turns spent entirely declining). The other two —
       don't repeat a dismissal, don't answer by restating your position — were
       aimed at the dismissal itself and are the likeliest suppressors.
    3. **The requirement comes first.** In ``retuned`` it is buried mid-paragraph,
       surrounded by its own restrictions.
    4. **Engaging and declining are separate blocks.** Crammed into one paragraph,
       the engagement clauses read as a hedge on the declining clause.
    """
    return (
        f"What you do not weigh: {items}.\n"
        "When someone argues from one of those concerns, you MUST say plainly that it "
        "is not yours to weigh. Every time it comes up. Naming it is not rudeness and "
        "it is not optional — the others need to know where your remit ends, and "
        "staying silent about it leaves them guessing.\n"
        "Then, in the same turn, engage with the substance anyway: answer the factual "
        "or technical part of what they said, and put their point in its strongest "
        "form. Declining to weigh something is not declining to think about it.\n"
        "The one thing to avoid: never let declining be your whole turn. Say what is "
        "not yours, then say what is."
    )


_RULE_RENDERERS = {
    "blunt": _rule_blunt,
    "retuned": _rule_retuned,
    "mandatory": _rule_mandatory,
}


def _dismissal_rule(dismisses: List[str], variant: str = "mandatory") -> str:
    """Render the named dismissal-rule variant, or ``""`` for ``off``."""
    if variant == "off" or not dismisses:
        return ""
    renderer = _RULE_RENDERERS.get(variant)
    if renderer is None:
        raise ValueError(
            f"dismissal rule variant must be one of {list(DISMISSAL_RULES)}, got {variant!r}"
        )
    return renderer("; ".join(dismisses))


def normalise_dismissal_rule(value: Any) -> str:
    """Coerce a config value to a variant name.

    Accepts the booleans the field used to be so existing run configs and stored
    snapshots keep working: ``True`` means "render the current default rule" and
    ``False`` means "no rule and no list", which is what they meant before.
    """
    if isinstance(value, bool):
        return _BOOL_RULES[value]
    v = str(value)
    if v not in DISMISSAL_RULES:
        raise ValueError(
            f"dismissal_rule must be one of {list(DISMISSAL_RULES)} (or a bool), got {value!r}"
        )
    return v


def parse_structured(raw: Any) -> Optional[StructuredPersona]:
    """Build a :class:`StructuredPersona` from a cast member's ``structured`` key.

    Returns ``None`` for anything that is not a non-empty dict, so a cast entry
    with no structured data, or with ``"structured": {}``, behaves exactly as it
    did before this feature existed. Invalid *contents* are NOT swallowed —
    a bad ``firmness`` raises, because silently dropping a persona's convictions
    would look like the feature simply not working.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    sp = StructuredPersona(**{k: v for k, v in raw.items() if k in StructuredPersona.model_fields})
    return None if sp.is_empty() else sp


def effective_persona(
    prose: str,
    structured: Optional[StructuredPersona],
    *,
    enabled: bool = True,
    withhold_concerns: bool = True,
    dismissal_rule: Any = "mandatory",
) -> str:
    """The persona text for a speaker's own system prompt.

    When ``enabled`` is False, or there is no structured data, this returns
    ``prose`` unchanged — byte-identical to the pre-Phase-6 prompt.
    """
    if not enabled or structured is None:
        return prose
    block = structured.render_private(
        withhold_concerns=withhold_concerns, dismissal_rule=dismissal_rule
    )
    return f"{prose}{block}" if prose else block.lstrip("\n")


def public_persona(
    prose: str, structured: Optional[StructuredPersona], *, enabled: bool = True
) -> str:
    """The persona description shown to the MODERATOR when picking a speaker.

    Adds only the public summary. This is the boundary that keeps a withheld
    concern out of a prompt that every other participant's turn is selected from.
    """
    if not enabled or structured is None:
        return prose
    summary = structured.render_public()
    if not summary:
        return prose
    return f"{prose} ({summary})" if prose else summary


def structured_payload(structured: Optional[StructuredPersona]) -> Optional[Dict[str, Any]]:
    """Serialise for an event payload, minus the operator's private notes.

    ``validity`` and ``underlying_concern`` are stripped: the event log is
    exported and rendered in the UI, and both fields are private to the operator
    by design. What remains is enough to see which convictions a run was seeded
    with.
    """
    if structured is None:
        return None
    data = structured.model_dump(exclude_none=True)
    for vp in data.get("viewpoints", []):
        vp.pop("validity", None)
        vp.pop("underlying_concern", None)
    return data
