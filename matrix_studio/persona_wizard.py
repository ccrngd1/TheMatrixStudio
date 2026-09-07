# SPDX-License-Identifier: Apache-2.0
"""Draft a cast of structured personas from a one-line brief.

## What this is, and what it deliberately is not

This is **authoring assistance**, not simulation. It runs before a run starts, it
produces a *draft the operator edits*, and nothing it returns is evidence about
anything. That distinction matters here more than in most codebases: this project's
standing invariant is that cognition is produced in-loop and never fabricated
post-hoc, and a persona generator sits on the other side of that line — it is a
tool for writing the input, like a template.

So the output is returned to the form for review and is never used directly to
start a run. The operator sees and edits every field before anything executes.

## Why it generates convictions rather than just prose

Prose personas are easy to write by hand; the Phase 6 fields are not. Getting
`firmness` paired with a credible `evidence_that_shifts`, and a `dismisses` list
that actually differs between personas, is fiddly enough that most operators would
skip it — which means skipping the thing measured to produce the least harmonising
discussion. Generating a first draft is the difference between the feature being
used and being ignored.

## Two calibration properties the prompt enforces

Both come from `docs/PHASE5-PREMISE-VALIDATION.md`, where they were authoring rules
for a hand-built cast:

1. **Firmness must not correlate with correctness.** If the firmest positions are
   also the soundest, an operator can win every panel by conceding to whoever
   pushes hardest, and the exercise teaches nothing. The prompt requires that at
   least one firmly-held position be questionable.
2. **`dismisses` must genuinely differ per persona.** Five people who all weigh the
   same things are one person, and the discussion converges — the failure mode the
   whole structured-persona design exists to prevent.

Neither can be *verified* here (they are judgments about content), so they are
requested explicitly and the operator is the check. What IS verified is the schema:
anything that would not parse as a `StructuredPersona` is dropped rather than
returned half-formed.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import litellm

from matrix_studio.jsonio import extract_json_object
from matrix_studio.personas import FIRMNESS_LEVELS, StructuredPersona

logger = logging.getLogger(__name__)

# A panel smaller than 3 cannot really disagree; larger than 7 and each persona
# gets too few turns for a position to be challenged and held (measured: 5 personas
# over 15 turns gives ~3 turns each, which was too few).
MIN_PERSONAS = 2
MAX_PERSONAS = 7
DEFAULT_PERSONAS = 5


class WizardError(RuntimeError):
    """Raised when no usable cast could be drafted. Carries an operator-readable
    reason, because the fallback is 'write them by hand' and the operator needs to
    know whether to retry or give up."""


def _prompt(brief: str, count: int) -> str:
    return (
        "You design casts of stakeholders for a multi-agent discussion simulator.\n\n"
        f"THE SITUATION:\n{brief}\n\n"
        f"Design exactly {count} stakeholders who would genuinely be in the room for "
        "this, and who would genuinely disagree with each other.\n\n"
        "For each one give:\n"
        "- name: a first name only\n"
        "- role: their job, as the room would describe it\n"
        "- persona: 2-3 sentences of voice and manner — how they talk, what they are "
        "like to be in a meeting with. Not their opinions; those go below.\n"
        "- goals: 1-2 things they are trying to achieve\n"
        "- optimises_for: 2-3 things they trade everything else for\n"
        "- dismisses: 2-3 concerns they decline to WEIGH. These MUST differ "
        "substantially between stakeholders — if several of them weigh the same "
        "things they are the same person and the discussion will converge.\n"
        "- persuaded_by: 1-2 kinds of evidence that actually move them\n"
        "- formative_event: one specific thing that happened to them, with a year, "
        "and the lesson they drew from it. Concrete, not generic.\n"
        "- position: the one thing they will argue for, stated as they would say it\n"
        f"- firmness: one of {list(FIRMNESS_LEVELS)}. 'requires-escalation' means they "
        "lack the AUTHORITY to concede, so being overruled produces 'I'll take this "
        "further', not agreement.\n"
        "- evidence_that_shifts: 1-2 specific things that would genuinely change "
        "their mind on that position. Required unless firmness is negotiable.\n"
        "- underlying_concern: the real worry underneath the position — usually "
        "personal stakes, what it costs THEM if they are wrong. This is deliberately "
        "withheld from the conversation and only comes out if someone asks why.\n\n"
        "TWO RULES ABOUT CALIBRATION:\n"
        "1. Do NOT make the firmly-held positions the correct ones. At least one "
        "stakeholder should hold a firm position that is outdated, overgeneralised or "
        "misapplied — otherwise the operator can win by agreeing with whoever pushes "
        "hardest, and the exercise is worthless.\n"
        "2. Their positions must actually conflict. Not five shades of agreement.\n\n"
        'Respond with ONLY strict JSON, no prose:\n'
        '{"cast": [{"name": "...", "role": "...", "persona": "...", '
        '"goals": ["..."], "optimises_for": ["..."], "dismisses": ["..."], '
        '"persuaded_by": ["..."], "formative_event": {"year": 2024, "event": "...", '
        '"lesson": "..."}, "position": "...", "firmness": "...", '
        '"evidence_that_shifts": ["..."], "underlying_concern": "..."}]}'
    )


def _as_list(value: Any, limit: int = 6) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()][:limit]


def _build_member(raw: Any) -> Optional[Dict[str, Any]]:
    """Turn one generated entry into a cast member, or None if unusable.

    Dropped rather than repaired when the essentials are missing: a persona with no
    name or no voice is not a draft an operator can edit, it is noise to delete.
    """
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    persona = str(raw.get("persona") or "").strip()
    if not name or not persona:
        return None

    firmness = str(raw.get("firmness") or "").strip().lower()
    if firmness not in FIRMNESS_LEVELS:
        # Unknown firmness becomes negotiable rather than rejecting the persona: the
        # safe direction, since a position wrongly negotiable merely gets argued,
        # while one wrongly firm becomes an immovable wall.
        firmness = "negotiable"

    event = raw.get("formative_event")
    formative: List[Dict[str, Any]] = []
    if isinstance(event, dict) and str(event.get("event") or "").strip():
        year = event.get("year")
        try:
            year = int(year) if year is not None else None
        except (TypeError, ValueError):
            year = None
        formative = [{
            "year": year,
            "event": str(event["event"]).strip(),
            "lesson": str(event.get("lesson") or "").strip(),
        }]

    position = str(raw.get("position") or "").strip()
    viewpoints: List[Dict[str, Any]] = []
    if position:
        viewpoints.append({
            "position": position,
            "firmness": firmness,
            "evidence_that_shifts": _as_list(raw.get("evidence_that_shifts"), 4),
            "underlying_concern": str(raw.get("underlying_concern") or "").strip(),
        })

    structured: Dict[str, Any] = {
        "role": str(raw.get("role") or "").strip(),
        "background": {"formative_events": formative},
        "preferences": {
            "optimises_for": _as_list(raw.get("optimises_for")),
            "dismisses": _as_list(raw.get("dismisses")),
            "persuaded_by": _as_list(raw.get("persuaded_by")),
        },
        "viewpoints": viewpoints,
    }

    # Validate against the real model. A draft that would 422 at run creation is
    # worse than no draft: the operator would only find out after filling in a form.
    try:
        StructuredPersona(**structured)
    except Exception as exc:  # noqa: BLE001 - a bad draft entry is dropped, not fatal
        logger.warning("Dropped generated persona %r: %s", name, exc)
        return None

    return {
        "name": name,
        "persona": persona,
        "goals": _as_list(raw.get("goals"), 4),
        "structured": structured,
    }


def _salvage_objects(raw: str) -> List[Any]:
    """Every complete, persona-shaped JSON object inside ``raw``, in order.

    Used when the whole document will not parse — overwhelmingly because the reply
    was truncated mid-object. Scans with a brace counter rather than a regex, since
    the objects nest and a regex cannot match balanced braces. String literals are
    tracked so a brace inside prose ("the {big rewrite}") does not desynchronise it.

    Objects are collected at EVERY depth, not just the top level: the personas live
    inside a ``{"cast": [...]}`` wrapper, so a top-level-only scan finds nothing but
    the wrapper itself. Shape filtering (a name and a persona) is what separates a
    real entry from the wrapper or a nested ``formative_event``.
    """
    out: List[Any] = []
    starts: List[int] = []
    in_string = False
    escaped = False
    for i, ch in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            starts.append(i)
        elif ch == "}" and starts:
            begin = starts.pop()
            try:
                obj = json.loads(raw[begin : i + 1])
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict) and obj.get("name") and obj.get("persona"):
                out.append(obj)
    # Innermost objects close first, so collection order is not document order.
    return sorted(out, key=lambda o: raw.find(json.dumps(o.get("name"))))


async def suggest_cast(
    brief: str,
    count: int = DEFAULT_PERSONAS,
    model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Draft ``count`` structured personas from ``brief``.

    Raises :class:`WizardError` when nothing usable came back, so the caller can
    tell the operator to retry or write the cast by hand — silently returning an
    empty list would look like the feature doing nothing.
    """
    brief = (brief or "").strip()
    if not brief:
        raise WizardError("Describe the situation first — one or two sentences is enough.")
    count = max(MIN_PERSONAS, min(int(count or DEFAULT_PERSONAS), MAX_PERSONAS))

    from matrix_studio.settings import get_settings

    resolved = model or get_settings().litellm_model
    try:
        response = await litellm.acompletion(
            model=resolved,
            messages=[{"role": "user", "content": _prompt(brief, count)}],
            # Higher than the engine default: a cast of near-identical stakeholders is
            # the failure mode here, and low temperature produces exactly that.
            temperature=1.0,
            # Generous, and it has to be. Measured: 4000 truncated a 5-persona draft
            # mid-object, which the strict parse then rejected entirely — a complete
            # failure caused only by a budget. Twelve fields of prose per persona at
            # up to MAX_PERSONAS adds up fast.
            max_tokens=16000,
        )
        raw = response.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator, never a 500
        logger.warning("Persona wizard call failed: %s", exc)
        raise WizardError(f"The model call failed: {exc}") from exc

    data = extract_json_object(raw)
    entries: List[Any]
    if isinstance(data, dict) and isinstance(data.get("cast"), list):
        entries = data["cast"]
    else:
        # Salvage. A truncated response is the expected failure here, not a rare one:
        # the reply is long, and being cut off mid-object makes the whole document
        # unparseable even though the first few personas are complete and perfectly
        # usable. Throwing away four good personas because a fifth was clipped would
        # be a worse outcome than a short cast.
        entries = _salvage_objects(raw)
        if not entries:
            logger.warning("Persona wizard returned unusable output: %r", raw[:200])
            raise WizardError(
                "The model did not return a usable cast. Try rephrasing the brief."
            )
        logger.info("Salvaged %d complete personas from a truncated reply", len(entries))

    members = [m for m in (_build_member(x) for x in entries) if m]
    if not members:
        raise WizardError("No usable personas came back. Try rephrasing the brief.")

    # Duplicate names would collide in the engine's agent dict, silently dropping a
    # persona. Deduplicate here rather than letting that happen at run start.
    seen: set[str] = set()
    unique = []
    for m in members:
        key = m["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(m)
    return unique[:count]
