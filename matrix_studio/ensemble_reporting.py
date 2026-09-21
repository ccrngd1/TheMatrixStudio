# SPDX-License-Identifier: Apache-2.0
"""Turning a finished ensemble into its stored report.

`ensemble.py` is the aggregation library — it knows nothing about ensembles as stored
objects. `ensemble_spec.py` plans the fan-out. This module is the seam between them and the
database: it decides *when* a report may be generated, who generates it, and what shape gets
stored.

Three rules, each of which exists because breaking it produces a confident wrong answer.

**Never report over a running cell.** `report_ready` means every declared member has reached
a terminal status. A conclusion counted as absent from a run that simply had not reached it
yet is docs/ENSEMBLE-CONVERSATIONS.md §3.4's censoring, and it reads as a dissent.

**Never pool the cells.** Every count is per cell, and the tier is computed within a cell
against that cell's own denominator. §4: the same 5/9 is a strong method-dependent finding or
a coin flip depending on how it splits, and a flat number cannot tell them apart.

**Never report on one run.** Two usable extractions is the minimum, and below it the report
is refused with a recorded reason rather than produced. A single conversation summarised under
an "ensemble report" heading is the most misleading artefact this system could emit: it looks
like corroborated evidence and is one sample.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence

from matrix_studio import ensemble, ensemble_spec

logger = logging.getLogger(__name__)

#: Below this many usable extractions there is nothing an ensemble can say that a single run
#: could not, so the report is refused rather than written. See the module docstring.
MIN_USABLE_RUNS = 2


def _claims_by_cell(
    views_by_cell: Dict[str, List[ensemble.RunView]]
) -> List[Dict[str, Any]]:
    """Every demand and refusal, tiered within each cell against that cell's denominator.

    This is the §4 table: one row per claim, one column per cell. A claim that is
    `unanimous` in one cell and `absent` in another is the interesting case — a
    method-dependent finding — and it is indistinguishable from noise in a pooled count.

    Uses `ensemble._normalise`, deliberately and with its limits accepted: it only collapses
    case, punctuation and filler, so the same demand phrased differently counts twice and
    agreement is UNDER-reported. That is the safe direction (over-merging would delete a
    dissent) and it is the stage-1.5 item in the backlog. A count here is a floor, not a
    measurement.
    """
    # key -> cell -> set of run names that held it, plus one readable text per key.
    held: Dict[str, Dict[str, set]] = defaultdict(lambda: defaultdict(set))
    text_of: Dict[str, str] = {}
    kind_of: Dict[str, str] = {}

    for label, views in views_by_cell.items():
        for view in views:
            for persona in (view.positions.get("personas") or []):
                for kind, field in (("demand", "demands"), ("refusal", "refusals")):
                    for raw in persona.get(field) or []:
                        key = ensemble._normalise(str(raw))
                        if not key:
                            continue
                        held[key][label].add(view.name)
                        text_of.setdefault(key, str(raw))
                        kind_of.setdefault(key, kind)

    denominators = {
        label: sum(1 for v in views if v.positions)
        for label, views in views_by_cell.items()
    }

    rows = []
    for key, by_cell in held.items():
        per_cell = {}
        for label, denominator in denominators.items():
            if denominator <= 0:
                # A cell that produced no usable extraction has no opinion, which is not the
                # same as 0 of N. Recorded as null so a reader cannot mistake an empty cell
                # for a cell that rejected the claim.
                per_cell[label] = None
                continue
            count = len(by_cell.get(label, ()))
            per_cell[label] = {
                "held": count,
                "of": denominator,
                "tier": ensemble_spec.tier(count, denominator),
                "runs": sorted(by_cell.get(label, ())),
            }
        rows.append({
            "claim": text_of[key],
            "kind": kind_of[key],
            "per_cell": per_cell,
        })

    # Most-corroborated first, then alphabetically so the order is stable between runs of
    # the report over the same data.
    def _weight(row):
        return sum(
            (c or {}).get("held", 0) for c in row["per_cell"].values()
        )

    rows.sort(key=lambda r: (-_weight(r), r["claim"]))
    return rows


def _cell_of(members: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """run_id -> cell label, from the parent's declared membership.

    From the parent, not from each run's own `ensemble_cell`: the parent is authoritative
    about what it asked for, and a member row that was never written has no field to read.
    """
    return {m["run_id"]: (m.get("cell") or "?") for m in members if m.get("run_id")}


async def build(
    db,
    ensemble_id: str,
    *,
    call: Optional[Any] = None,
    synthesise: bool = True,
) -> Dict[str, Any]:
    """The report for one ensemble. Reads and model calls only — stores nothing.

    Separate from `generate` so the expensive part is testable without the claim, the status
    writes, or a database that can be written to.

    Raises `ValueError` when a report would be misleading rather than merely thin: a cell
    still running, or fewer than `MIN_USABLE_RUNS` usable extractions.
    """
    parent = await db.get_ensemble(ensemble_id)
    if parent is None:
        raise ValueError(f"ensemble {ensemble_id!r} does not exist")

    members = await db.list_ensemble_members(ensemble_id)
    if not members:
        raise ValueError(f"ensemble {ensemble_id!r} declares no members")

    unsettled = [
        m["run_id"] for m in members
        if m["run"] is not None and m["run"].get("status") not in _TERMINAL_RUN_STATUSES
    ]
    if unsettled:
        raise ValueError(
            f"{len(unsettled)} member(s) of ensemble {ensemble_id} are still running. "
            "Reporting now would count a conclusion as absent from a run that has not "
            "reached it yet, which reads as a dissent."
        )

    cell_of = _cell_of(members)
    present = [m["run_id"] for m in members if m["run"] is not None]
    missing = [
        {"run_id": m["run_id"], "cell": m["cell"], "index": m["index"]}
        for m in members if m["run"] is None
    ]

    views = await ensemble.collect(db, present)

    # One extraction per run. Sequential rather than gathered: this runs inside a member's
    # final Lambda alongside whatever else that invocation is doing, and N concurrent
    # extractions is the same first-turn spike the fan-out staggers against.
    cost = 0.0
    for view in views:
        view.positions = await ensemble.extract_positions(view, call=call)
        cost += float(view.positions.get("_cost_usd") or 0.0)

    usable = [v for v in views if v.positions]
    if len(usable) < MIN_USABLE_RUNS:
        raise ValueError(
            f"Only {len(usable)} of {len(members)} member(s) of ensemble {ensemble_id} "
            f"produced a usable extraction; {MIN_USABLE_RUNS} is the minimum. A single "
            "conversation under an ensemble heading looks like corroborated evidence and "
            "is one sample."
        )

    views_by_cell: Dict[str, List[ensemble.RunView]] = defaultdict(list)
    for view in views:
        views_by_cell[cell_of.get(view.run_id, "?")].append(view)
    # Every DECLARED cell appears, including one whose runs all failed. The spec is stored on
    # the parent for exactly this: a cell that produced nothing is a result, and recomputing
    # the cell list from the members that exist would silently drop it.
    for declared_cell in _spec_of(parent):
        label = declared_cell.get("label")
        if label:
            views_by_cell.setdefault(str(label), [])

    synthesis = {"content": "", "cost_usd": 0.0}
    if synthesise and usable:
        synthesis = await ensemble.synthesise(usable, call=call)
        cost += float(synthesis.get("cost_usd") or 0.0)

    cells = [
        {
            "cell": label,
            "runs": [
                {"run_id": v.run_id, "name": v.name, "metrics": v.metrics,
                 "usable": bool(v.positions)}
                for v in sorted(views_by_cell[label], key=lambda v: v.name)
            ],
            "usable": sum(1 for v in views_by_cell[label] if v.positions),
            "declared": sum(1 for m in members if (m.get("cell") or "?") == label),
        }
        for label in sorted(views_by_cell)
    ]
    widest_cell = max((c["usable"] for c in cells), default=0)

    return {
        "generated_at": int(time.time()),
        "ensemble_id": ensemble_id,
        "cells": cells,
        "missing_members": missing,
        # Per-cell, per-claim tiers. The §4 table.
        "claims": _claims_by_cell(views_by_cell),
        "per_persona": ensemble.per_persona(usable),
        "agreements": ensemble.agreements_and_dissents(usable),
        "synthesis": synthesis.get("content") or "",
        "cost_usd": round(cost, 4),
        # Carried in the artefact, not only in a doc. A reader who sees "1 of 5" needs to
        # know the matcher under-merges before concluding a persona changed their mind, and
        # they will be reading this in a UI that does not link to §8.2.
        "caveats": [
            "Claim counts are a FLOOR. Claims are matched by a crude text key, so the same "
            "demand phrased differently counts twice and agreement is under-reported.",
            f"The largest cell has {widest_cell} usable run(s). That supports "
            "unanimous / split / rare and nothing finer — a difference between two "
            "non-unanimous tiers is not a finding.",
        ],
    }


_TERMINAL_RUN_STATUSES = frozenset(
    {"complete", "failed", "stopped", "capped", "interrupted"}
)


def _spec_of(parent: Dict[str, Any]) -> List[Dict[str, Any]]:
    import json

    try:
        parsed = json.loads(parent.get("spec_json") or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


async def generate(
    db,
    ensemble_id: str,
    *,
    force: bool = False,
    call: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Claim, build and store the report. None when this caller did not win the claim.

    Never raises for an ordinary failure: the report is an additive artefact and the runs it
    describes are already paid for and intact. A failure records `report_error` on the parent
    so the state is distinguishable from "no report yet", and a caller can retry with
    `force=True`.
    """
    if not await db.claim_ensemble_report(ensemble_id, force=force):
        logger.info(
            "Ensemble %s report is already claimed; leaving it to the claim holder",
            ensemble_id,
        )
        return None

    try:
        report = await build(db, ensemble_id, call=call)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ensemble %s report failed", ensemble_id)
        try:
            await db.update_ensemble(ensemble_id, report_error=str(exc)[:900])
        except Exception:  # noqa: BLE001
            logger.exception("Could not record the report failure on ensemble %s", ensemble_id)
        return None

    await db.update_ensemble(
        ensemble_id,
        report=report,
        report_cost_usd=report.get("cost_usd"),
        # Cleared, so a retry that succeeds does not leave a stale error beside a good
        # report.
        report_error="",
        status="complete",
        completed_at=int(time.time()),
    )
    logger.info(
        "Ensemble %s report stored: %d cells, %d claims, $%.4f",
        ensemble_id, len(report.get("cells") or []), len(report.get("claims") or []),
        report.get("cost_usd") or 0.0,
    )
    return report


async def maybe_report_for_member(db, run: Dict[str, Any]) -> None:
    """Generate the ensemble's report if this run was its last outstanding member.

    Called from `orchestration.finalise`, which is the same place and for the same reason as
    the per-run auto-summary: on the deployed path a run's last act happens inside a Step
    Functions state, and a background asyncio task there dies when the handler returns.

    Best-effort throughout. A run must never fail because the ensemble report did — the
    conversation is the product and the report is commentary on it.
    """
    ensemble_id = run.get("ensemble_id")
    if not ensemble_id:
        return
    try:
        members = await db.list_ensemble_members(str(ensemble_id))
        outstanding = [
            m["run_id"] for m in members
            if m["run"] is not None
            and m["run"].get("status") not in _TERMINAL_RUN_STATUSES
        ]
        if outstanding:
            logger.info(
                "Ensemble %s still has %d member(s) running; not reporting yet",
                ensemble_id, len(outstanding),
            )
            return
        await generate(db, str(ensemble_id))
    except Exception:  # noqa: BLE001
        logger.exception(
            "Ensemble %s report attempt failed after member %s finished; the run itself is "
            "unaffected", ensemble_id, run.get("id"),
        )
