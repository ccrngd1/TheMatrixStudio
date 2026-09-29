# SPDX-License-Identifier: Apache-2.0
"""What an ensemble is, before any of it runs: cells, replicates, and the one rule.

`ensemble.py` aggregates runs that already exist. This module decides which runs to
create, and it exists mostly to make one class of mistake impossible.

**The rule.** An ensemble is one or more *cells*. Every member of a cell has a
byte-identical config; between cells, only the keys a cell explicitly declared may
differ. `check_isolated` proves that against the materialised configs rather than
trusting the declaration, and `plan` calls it before anything is created.

That check is the whole point of the module. `docs/ENSEMBLE-CONVERSATIONS.md` §3.3
records the failure it prevents: a nine-run renewal sweep that moved method, turn count
and fairness together with one run per cell, and could therefore not attribute a single
difference it found. The only interpretable evidence in it came from three same-config
pairs that happened to exist by accident. Replicates are not a nicety here — they are the
control every other axis is defined against, so a cell of one is refused.

**Why the override allowlist is this short.** §3.2: the settings this system exposes are
traffic control — they decide who talks and when, not what any persona believes. `method`
is the exception, because blind rounds change what a persona has *seen* when they speak,
and that is the only exposed knob that reaches new conclusions rather than reshuffling
reachable ones. Everything else is either measured settled, actively harmful to pool, or
not exposed at all; `_REFUSED` carries the reason per key so the error teaches.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence, Tuple

#: Replicates per cell when the caller does not say. §8 of the design doc; §8.2 records
#: what it buys — coarse tiers (unanimous / split / rare), not significance.
DEFAULT_REPLICATES = 5

#: A cell with fewer than this has no within-cell variance to measure, which is the one
#: thing the ensemble exists to measure. Refused rather than warned.
MIN_REPLICATES = 2

#: Total members across all cells. Not a cost control — `over_monthly_cap` is — but a
#: guard against a typo fanning out a hundred runs before anyone notices.
MAX_MEMBERS = 12

#: Config keys a cell may override, with the reason each one earned its place. Dotted
#: paths into the run config. Anything absent from here is refused by `_check_overrides`.
CELL_OVERRIDES: Dict[str, str] = {
    "selection.method": (
        "The only exposed knob that changes what a persona has SEEN when they speak. "
        "Blind rounds remove the anchoring of hearing whoever spoke first, so they can "
        "reach a conclusion sighted ordering suppresses (§3.2, §5.1)."
    ),
    "selection.hybrid_opening_rounds": (
        "How long the blind opening lasts before hybrid switches to moderated. Only "
        "meaningful alongside method=hybrid."
    ),
    "injections": (
        "An operator message at a fixed turn (matrix_studio/injections.py): what the room has SEEN, "
        "like method, and the only way to attribute its effect — the same message in every "
        "replicate of a cell, against replicates without it. A single branch injection is one draw. "
        "Refused when it speaks as a cast member: words put in a persona's mouth are a persona "
        "instruction, which the personas prefix refuses."
    ),
    "assumptions": (
        "What the room is told to reason from (matrix_studio/assumptions.py). Varying one across "
        "cells measures what that assumption was worth over replicates, which a fork with a "
        "different value — one draw each — cannot."
    ),
}

#: Keys that are tempting, plausible, and wrong — each with the reason, because a bare
#: 'not allowed' invites someone to add it back. Consulted before the allowlist so the
#: specific reason wins over the generic one.
_REFUSED: Dict[str, str] = {
    "max_messages": (
        "Turn count is censoring, not variation: a run that ends before reaching a "
        "question yields a missing datum that reads as a dissent. The indemnification "
        "blocker in the renewal runs tracked turn count exactly this way — that is a "
        "bias term, and the answer is to choose one length, not to average over several "
        "(§3.4)."
    ),
    "selection.fairness": (
        "Already measured: Gini 0.458 -> 0.075. Settled in "
        "docs/SPEAKER-SELECTION-EVALUATION.md, so re-litigating it per cell spends money "
        "to reproduce a known result (§5)."
    ),
    "selection.stop_when_converged": (
        "Changes when a run stops, so it censors like turn count does (§3.4). It is also "
        "still being validated against its own pre-registered criterion; varying it here "
        "would confound that measurement with this one."
    ),
    "models.voice": (
        "Changes the instrument, not the question. A finding that appears only under one "
        "voice model tells you about the model, and pooling the cells hides which."
    ),
}

#: Refused by prefix rather than exact key: no part of a persona's instructions may vary
#: across an ensemble. §7 — the personas are the measuring device. Perturb them and a
#: dissent is an artifact of the perturbation, which manufactures the disagreement it
#: claims to discover. Changing WHO IS IN THE ROOM is the legitimate version (§5.3) and
#: is a cast-level change, not a config override.
_REFUSED_PREFIXES: Dict[str, str] = {
    "personas": (
        "Persona instructions are the measuring instrument, not a variable. Injecting "
        "'be more skeptical' into one cell manufactures the disagreement the ensemble "
        "claims to discover, and no downstream analysis recovers the real signal (§7). "
        "To vary participation, vary the cast (leave-one-out, §5.3)."
    ),
}


class SpecError(ValueError):
    """A spec that would produce an uninterpretable ensemble. Raised before creation."""


@dataclass(frozen=True)
class Cell:
    """One labelled group of replicates.

    `overrides` is a flat mapping of dotted config paths, e.g.
    `{"selection.method": "hybrid"}`. Flat rather than nested so a cell's divergence from
    the base is a readable one-liner in a report, and so `check_isolated` can compare
    declared keys against measured ones without walking two trees.
    """

    label: str
    n: int = DEFAULT_REPLICATES
    overrides: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Member:
    """One run to create. `index` is 1-based within the cell, for names and ordering."""

    cell: str
    index: int
    config: Dict[str, Any]

    def name_for(self, base: str) -> str:
        """A member's run name. Collisions are the storage layer's problem — it already
        disambiguates a duplicate rather than rejecting it."""
        return f"{base}-{self.cell}{self.index}"


def _split(path: str) -> List[str]:
    parts = [p for p in path.split(".") if p]
    if not parts:
        raise SpecError("An override key cannot be empty.")
    return parts


def _check_overrides(overrides: Mapping[str, Any]) -> None:
    for key in overrides:
        if key in _REFUSED:
            raise SpecError(f"{key!r} may not vary across an ensemble. {_REFUSED[key]}")
        head = _split(key)[0]
        if head in _REFUSED_PREFIXES:
            raise SpecError(
                f"{key!r} may not vary across an ensemble. {_REFUSED_PREFIXES[head]}"
            )
        if key not in CELL_OVERRIDES:
            allowed = ", ".join(sorted(CELL_OVERRIDES))
            raise SpecError(
                f"{key!r} is not a cell override. An ensemble varies at most "
                f"{{{allowed}}}; everything else is either measured settled or "
                "censoring. See docs/ENSEMBLE-CONVERSATIONS.md §5 before adding one."
            )


def validate(cells: Sequence[Cell]) -> None:
    """Refuse a spec that cannot produce an interpretable report.

    Checked here rather than at the API boundary so the CLI, a test and the route all get
    the same refusal with the same reason.
    """
    if not cells:
        raise SpecError("An ensemble needs at least one cell.")

    seen: set = set()
    for cell in cells:
        label = (cell.label or "").strip()
        if not label:
            raise SpecError("Every cell needs a label; labels appear in the report.")
        if label != cell.label:
            raise SpecError(f"Cell label {cell.label!r} has leading or trailing space.")
        if label in seen:
            raise SpecError(
                f"Two cells are labelled {label!r}. Labels key the stratified counts, so "
                "duplicates would silently merge two cells into one — which is the "
                "flattening §4 forbids."
            )
        seen.add(label)

        if cell.n < MIN_REPLICATES:
            raise SpecError(
                f"Cell {label!r} asks for {cell.n} run(s). A cell needs at least "
                f"{MIN_REPLICATES}: with one run there is no within-cell variance, and "
                "without that there is no way to tell a real difference from a coin "
                "flip. That is the whole finding of §3.3."
            )
        _check_overrides(cell.overrides)

    total = sum(c.n for c in cells)
    if total > MAX_MEMBERS:
        raise SpecError(
            f"{total} members exceeds the {MAX_MEMBERS}-member guard. Raise "
            "MAX_MEMBERS deliberately if you mean it."
        )


def replicates(n: int = DEFAULT_REPLICATES, label: str = "base") -> List[Cell]:
    """The default ensemble: one cell, nothing varied.

    A named constructor because this is the shape the design doc argues for and the one a
    caller should get by writing the least code. §8: replicates first, because every
    other axis is defined relative to them.
    """
    return [Cell(label=label, n=n)]


def with_hybrid(
    n: int = 3, opening_rounds: int = 2, *, base: int = DEFAULT_REPLICATES
) -> List[Cell]:
    """The two-cell shape of §5.1: replicates plus a blind-opening arm.

    `hybrid` rather than `simultaneous`: it keeps the mechanism that adds coverage (blind
    opening rounds) and the mechanism that resolves hard questions (moderated turns
    after), where pure `simultaneous` at 8 turns was the arm with the least room to reach
    resolution.
    """
    return [
        Cell(label="base", n=base),
        Cell(
            label="hybrid",
            n=n,
            overrides={
                "selection.method": "hybrid",
                "selection.hybrid_opening_rounds": opening_rounds,
            },
        ),
    ]


def apply_overrides(config: Mapping[str, Any], overrides: Mapping[str, Any]) -> Dict[str, Any]:
    """`config` deep-copied with `overrides` applied at their dotted paths.

    Intermediate dicts are created as needed, so `{"selection.method": "hybrid"}` works on
    a config with no `selection` block at all — which is the common case, since selection
    defaults to on and most callers never write it.
    """
    out = copy.deepcopy(dict(config))
    for path, value in overrides.items():
        parts = _split(path)
        cursor: Dict[str, Any] = out
        for part in parts[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                # Replaces a non-dict (including None, which is how every optional block
                # is spelled when absent) rather than raising: the caller asked for a
                # nested key, and refusing here would mean every caller pre-builds the
                # block just to override one field inside it.
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[parts[-1]] = value
    return out


def _flatten(config: Mapping[str, Any], prefix: str = "") -> Dict[str, Any]:
    """Config as dotted leaf paths, for comparing two configs by key."""
    out: Dict[str, Any] = {}
    for key, value in config.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            nested = _flatten(value, f"{path}.")
            if nested:
                out.update(nested)
            else:
                # An empty dict is a leaf for this purpose — it differs from absent, and
                # dropping it would make two unequal configs compare equal.
                out[path] = {}
        else:
            out[path] = value
    return out


def divergent_keys(members: Sequence[Member]) -> Dict[str, set]:
    """Every config key that is not the same across all `members`, mapped to the labels
    that disagree.

    Derived from the materialised configs, not from what a cell declared. That is the
    point: a declaration is a claim, and this is the measurement.
    """
    flat = [(m, _flatten(m.config)) for m in members]
    keys: set = set()
    for _, f in flat:
        keys.update(f)

    out: Dict[str, set] = {}
    _missing = object()
    for key in keys:
        values = [(m, f.get(key, _missing)) for m, f in flat]
        first = values[0][1]
        if any(v != first for _, v in values):
            out[key] = {m.cell for m, _ in values}
    return out


def check_isolated(members: Sequence[Member], cells: Sequence[Cell]) -> None:
    """Prove the two invariants the report's interpretability rests on.

    1. Within a cell, every member config is identical. A cell whose members differ is
       not a cell — its internal spread would mix sampling variance with a config
       difference, and the robustness tiers built on it would be meaningless.
    2. Between cells, nothing differs except the keys some cell declared. This catches a
       config difference nobody intended: a default that resolves differently under one
       override, a mutated shared dict, an override that writes a sibling key.

    Called by `plan`, so an unattributable ensemble cannot be created even by a caller
    who builds `Member`s directly and skips the constructors.
    """
    by_cell: Dict[str, List[Member]] = {}
    for m in members:
        by_cell.setdefault(m.cell, []).append(m)

    for label, group in by_cell.items():
        first = _flatten(group[0].config)
        for other in group[1:]:
            if _flatten(other.config) != first:
                raise SpecError(
                    f"Members of cell {label!r} do not share one config. Replicates must "
                    "be byte-identical or the cell measures two things at once."
                )

    declared = {k for cell in cells for k in cell.overrides}
    measured = divergent_keys(members)
    undeclared = set(measured) - declared
    if undeclared:
        raise SpecError(
            "These config keys differ between cells but no cell declared them: "
            + ", ".join(sorted(undeclared))
            + ". An undeclared difference is exactly the confounding of §3.3 — the "
            "report would attribute a divergence to the declared override when this "
            "caused it."
        )


def plan(base_config: Mapping[str, Any], cells: Sequence[Cell]) -> List[Member]:
    """The runs to create, in creation order, validated and proven isolated.

    `base_config` is the run config the user built in the form — it is the *base*, and a
    cell with no overrides uses it unchanged, which is what makes the default mode
    literally 'the same run, N times'.
    """
    validate(cells)
    members = [
        Member(cell=cell.label, index=i, config=apply_overrides(base_config, cell.overrides))
        for cell in cells
        for i in range(1, cell.n + 1)
    ]
    check_isolated(members, cells)
    return members


def describe(cells: Sequence[Cell]) -> List[Dict[str, Any]]:
    """The spec as stored on the parent row and shown in the report header.

    Stored as data rather than recomputed from the children: a cell whose runs all failed
    still has to appear in the report, and a member's config alone does not say which
    label it belonged to.
    """
    return [
        {
            "label": c.label,
            "n": c.n,
            "overrides": dict(c.overrides),
        }
        for c in cells
    ]


def cells_from(spec: Sequence[Mapping[str, Any]]) -> List[Cell]:
    """Rebuild cells from `describe` output (a stored spec, or an API body)."""
    out: List[Cell] = []
    for raw in spec:
        if not isinstance(raw, Mapping):
            raise SpecError("Each cell must be an object with a label.")
        unknown = set(raw) - {"label", "n", "overrides"}
        if unknown:
            raise SpecError(
                "Unknown cell field(s): " + ", ".join(sorted(unknown))
                + ". A cell is {label, n, overrides}."
            )
        overrides = raw.get("overrides") or {}
        if not isinstance(overrides, Mapping):
            raise SpecError("A cell's `overrides` must be an object of dotted paths.")
        out.append(
            Cell(
                label=str(raw.get("label") or ""),
                n=int(raw.get("n") or DEFAULT_REPLICATES),
                overrides=dict(overrides),
            )
        )
    validate(out)
    return out


def tier(held: int, total: int) -> str:
    """The coarse robustness label for 'held in `held` of `total` runs in one cell'.

    Tiers, not a score, and §8.2 is the reason: five replicates support a coarse label and
    nothing finer. A percentage would invite someone to read 3/5 against 2/5 as a
    difference, which at this N it is not.

    The boundary between `split` and `rare` is **replication, not magnitude**: `rare` means
    exactly one run produced it, so it was never independently reproduced. That is a claim
    N supports at any size. A majority cut here — 3/5 `split` against 2/5 `rare` — would
    reintroduce the very comparison §8.2 says is unsupported, just spelled as a tier name;
    an earlier draft of this function did that, and the test caught it.
    """
    if total <= 0:
        raise ValueError("A tier over zero runs is not defined.")
    if held < 0 or held > total:
        raise ValueError(f"{held} of {total} runs is not a possible count.")
    if held == total:
        return "unanimous"
    if held == 0:
        return "absent"
    if held == 1:
        return "rare"
    return "split"
