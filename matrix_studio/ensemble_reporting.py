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

**Claims are clustered before they are counted.** The first live 5-replicate ensemble put 128
of 128 claims in their own group under `ensemble._normalise`: every count read 1/5, every
persona reported zero invariant demands, while the synthesis over the same extractions found
four conclusions held in 5 of 5. The numeric half of the report contradicted the prose half and
looked the more authoritative of the two. `ensemble.cluster_claims` now assigns canonical
labels first, biased against merging, checked arithmetically by `apply_clusters`, and every row
carries the phrasings that were grouped so a merge can be disputed. `clustered` in the result
says which mode produced the counts, because the fallback's numbers are noise rather than a
rougher version of the same thing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence

from matrix_studio import ensemble, ensemble_spec

logger = logging.getLogger(__name__)

#: Below this many usable extractions there is nothing an ensemble can say that a single run
#: could not, so the report is refused rather than written. See the module docstring.
MIN_USABLE_RUNS = 2


def _harvest_claims(
    views_by_cell: Dict[str, List[ensemble.RunView]]
) -> List[Dict[str, Any]]:
    """Every demand and refusal as a flat list, in a stable order.

    Flat and stable because the clustering call is indexed by position: a reordering between
    building the prompt and applying the answer would silently attach labels to the wrong
    claims, which no arithmetic check would catch.
    """
    out: List[Dict[str, Any]] = []
    for label in sorted(views_by_cell):
        for view in sorted(views_by_cell[label], key=lambda v: v.name):
            for persona in (view.positions.get("personas") or []):
                for kind, field in (("demand", "demands"), ("refusal", "refusals")):
                    for raw in persona.get(field) or []:
                        text = str(raw).strip()
                        if text:
                            out.append({
                                "kind": kind, "text": text, "cell": label,
                                "run": view.name, "persona": persona.get("name"),
                            })
            # `unresolved` belongs to the RUN, not to a persona, and is harvested here so it is
            # clustered in the same pass — one model call per kind either way. Without it the
            # unresolved tally kept its own text matching and every question counted 1, which
            # made "the same question was left open in all five runs" unsayable.
            #
            # Excluded from the claim table by `_claims_by_cell`: an open question is not a
            # position anybody held.
            for raw in view.positions.get("unresolved") or []:
                text = str(raw).strip()
                if text:
                    out.append({
                        "kind": "unresolved", "text": text, "cell": label,
                        "run": view.name, "persona": None,
                    })
            # Conclusions belong to the RUN too, and are clustered in the same pass — which is
            # what lets "exclude California" in one run and "launch without CA" in another count
            # as the same conclusion. Clustering partitions by kind, so a conclusion can never be
            # merged with a demand that happens to share its words.
            for raw in view.positions.get("conclusions") or []:
                text = str(raw).strip()
                if text:
                    out.append({
                        "kind": "conclusion", "text": text, "cell": label,
                        "run": view.name, "persona": None,
                    })
    return out


def _claims_by_cell(
    views_by_cell: Dict[str, List[ensemble.RunView]],
    keys: Optional[Dict[int, Any]] = None,
    kinds: Sequence[str] = ("demand", "refusal"),
) -> List[Dict[str, Any]]:
    """Every demand and refusal, tiered within each cell against that cell's denominator.

    This is the §4 table: one row per claim, one column per cell. A claim that is
    `unanimous` in one cell and `absent` in another is the interesting case — a
    method-dependent finding — and it is indistinguishable from noise in a pooled count.

    `keys` maps a harvested claim's index to `(cluster key, label)` from
    `ensemble.apply_clusters`. Without it, claims are keyed by `ensemble._normalise`, which
    only collapses case, punctuation and filler — measured on the first live ensemble to put
    128 of 128 claims in their own group, i.e. to produce no signal whatsoever. The fallback
    exists so a failed clustering call still yields a report; `clustered` in the result says
    which happened, and the caveats change to match.

    Each row carries its `variants`: the distinct phrasings that were grouped, with the runs
    they came from. That is what makes a merge auditable — a reader who thinks two claims were
    wrongly combined can see it and say so, which is not possible from a count alone.
    """
    # Demands and refusals by default. An open question is not a position anybody held, so it has
    # no place in a table of who required what — even though it IS clustered, in the same pass.
    #
    # `kinds=("conclusion",)` builds the conclusions table with the SAME per-cell arithmetic, so the
    # two cannot disagree about denominators, tiers or what an empty cell means. A second copy of
    # this function for conclusions is exactly how they would drift.
    claims = [
        (i, c) for i, c in enumerate(_harvest_claims(views_by_cell))
        if c["kind"] in kinds
    ]

    held: Dict[Any, Dict[str, set]] = defaultdict(lambda: defaultdict(set))
    label_of: Dict[Any, str] = {}
    kind_of: Dict[Any, str] = {}
    variants: Dict[Any, Dict[str, set]] = defaultdict(lambda: defaultdict(set))

    for i, claim in claims:
        # Indexed by the claim's position in the FULL harvest, because that is the index space
        # the clustering was numbered in. Re-enumerating the filtered list here would attach
        # every label to the wrong claim, and no arithmetic check would catch it.
        if keys is not None:
            key, label = keys[i]
        else:
            key = ensemble._normalise(claim["text"])
            label = claim["text"]
            if not key:
                continue
        held[key][claim["cell"]].add(claim["run"])
        label_of.setdefault(key, label)
        kind_of.setdefault(key, claim["kind"])
        variants[key][claim["text"]].add(claim["run"])

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
            "claim": label_of[key],
            "kind": kind_of[key],
            "per_cell": per_cell,
            "variants": [
                {"text": text, "runs": sorted(runs)}
                for text, runs in sorted(variants[key].items())
            ],
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
    cluster: bool = True,
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

    # One extraction per run, CONCURRENTLY.
    #
    # These were sequential while the report ran inside a member's final Lambda, where N
    # simultaneous extractions would have spiked alongside whatever else that invocation was
    # doing. The report now has its own function, so that reason is gone — and wall-clock
    # became the binding constraint instead: measured end-to-end at 12–19 minutes against a
    # Lambda hard ceiling of 15. Each extraction reads a whole 40-turn transcript and they are
    # completely independent, so running them in sequence was spending minutes to save nothing.
    #
    # Bedrock headroom is not the limit here: 5 concurrent extractions are a fraction of the
    # 3M input tokens/min quota, and a full 12-member ensemble is bounded by `MAX_MEMBERS`.
    cost = 0.0
    extractions = await asyncio.gather(*(
        ensemble.extract_positions(view, call=call) for view in views
    ))
    for view, positions in zip(views, extractions):
        view.positions = positions
        cost += float(positions.get("_cost_usd") or 0.0)

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

    # Stage 1.5: one call to give the claims canonical labels, so the counts mean something.
    # Before this, `_normalise` put 128 of 128 claims in their own group on the first live
    # ensemble — the numeric half of the report carried no signal while the synthesis found
    # four conclusions in 5 of 5. Failing SOFT on purpose: a report with honest-but-crude
    # counts beats no report, and `clustered` records which one a reader is looking at.
    harvested = _harvest_claims(views_by_cell)
    keys: Optional[Dict[int, Any]] = None
    canonical: Optional[ensemble.Canonical] = None
    cluster_note: Optional[str] = None
    if cluster and len(harvested) >= 2:
        # The persona goes to the model too: over-merging showed up as two participants' distinct
        # claims in one run being combined, and a name is the cheapest signal that they are two
        # requirements rather than one person restating themselves across runs.
        pairs = [(c["kind"], c["text"], c["persona"] or "") for c in harvested]
        clusters = await ensemble.cluster_claims(pairs, call=call)
        if clusters:
            cost += float(clusters[0].get("_cost_usd") or 0.0)
            try:
                keys = ensemble.apply_clusters(pairs, clusters)
                # Keyed by `(kind, text)` so the per-persona and unresolved sections can look a
                # claim up without knowing its harvest position. First occurrence wins where the
                # same sentence appears twice — `apply_clusters` guarantees each POSITION is
                # assigned once, not that identical texts land in the same cluster.
                canonical = {}
                for i, claim in enumerate(pairs):
                    canonical.setdefault((claim[0], claim[1]), keys[i])
            except ensemble.ClusteringRejected as exc:
                # Rejected rather than partially trusted. A clustering that dropped claims
                # would under-count exactly like the bug it replaces, while looking fixed.
                logger.warning(
                    "Rejected the claim clustering for ensemble %s (%s); falling back to text "
                    "matching, which under-reports agreement.", ensemble_id, exc,
                )
                keys = None
                canonical = None
                cluster_note = f"Clustering was rejected as unsound ({exc})."
        else:
            cluster_note = "The clustering call returned nothing usable."

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
        "claims": _claims_by_cell(views_by_cell, keys),
        # What the runs CONCLUDED, clustered across runs and counted per cell — the answer to "so
        # what did the room decide, and how consistently?". Same arithmetic as the claim table:
        # per cell, never pooled, and a conclusion reached in one run is `rare`, not a finding.
        # Empty when no run concluded anything, which is a result and is reported as one.
        "conclusions": _claims_by_cell(views_by_cell, keys, kinds=("conclusion",)),
        # Which way the counts were produced. Not a detail: the two modes differ by more than
        # precision — text matching measured 128 of 128 claims as unique on the first live
        # ensemble, so its counts are not a worse version of the clustered ones, they are
        # noise. A reader has to know which they are looking at.
        "clustered": keys is not None,
        # Both take the SAME canonical labels as the claim table. They did not, briefly, and
        # the result was a table reporting a unanimous finding beside a persona section
        # reporting zero invariant demands — computed from identical extractions.
        "per_persona": ensemble.per_persona(usable, canonical),
        "agreements": ensemble.agreements_and_dissents(usable, canonical),
        "synthesis": synthesis.get("content") or "",
        "cost_usd": round(cost, 4),
        # Carried in the artefact, not only in a doc: whoever reads a count in a UI has no
        # link to §8.2.
        "caveats": [
            (
                "Claims, refusals and open questions were grouped into canonical labels by a "
                "model, biased against merging. Each row lists the phrasings that were grouped "
                "and the runs they came from — check them before trusting a count."
                if keys is not None else
                "Claim counts are NOT RELIABLE. Claims fell back to crude text matching, so "
                "the same demand phrased differently counts separately and agreement is "
                "badly under-reported — measured at 128 of 128 claims counting as unique on "
                "a 5-run ensemble. Read the synthesis instead."
                + (f" {cluster_note}" if cluster_note else "")
            ),
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
    await _record_report_spend(db, ensemble_id, float(report.get("cost_usd") or 0.0))
    logger.info(
        "Ensemble %s report stored: %d cells, %d claims, $%.4f",
        ensemble_id, len(report.get("cells") or []), len(report.get("claims") or []),
        report.get("cost_usd") or 0.0,
    )
    return report


async def _record_report_spend(db, ensemble_id: str, cost: float) -> None:
    """Add the report's cost to the owner's monthly total.

    Without this the report is UNMETERED. Every member conversation is an ordinary run and its
    spend is recorded by `execute_slice`, so the fan-out itself counts against the cap — but
    the report is one extraction per member plus a 20k-token synthesis, charged nowhere. A
    12-member ensemble could therefore spend real money that the cap never sees, and the cap's
    whole job is to refuse the NEXT thing.

    Never raises, for the reason `orchestration.record_spend` does not: a missed increment
    delays the cap rather than breaking the report that was already paid for and stored.
    """
    if cost <= 0:
        return
    try:
        parent = await db.get_ensemble(ensemble_id)
        owner = (parent or {}).get("owner_sub")
        if not owner:
            logger.warning(
                "Ensemble %s has no owner on its row, so $%.4f of report spend is "
                "unattributed; the monthly total now under-reports.", ensemble_id, cost,
            )
            return
        await db.add_user_spend(cost, owner_sub=str(owner))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not record $%.4f of ensemble %s report spend (%s). The monthly total "
            "now UNDER-reports, so the cap will refuse later than it should.",
            cost, ensemble_id, exc,
        )


def report_function_name() -> str:
    """The dedicated report Lambda's name, or "" when there is none.

    Empty is the local case, not a misconfiguration — one long-lived uvicorn process can await
    the report in a background task. Its presence is what selects between dispatching and
    running inline, exactly as `TURN_LOOP_ARN` does for the turn loop.
    """
    import os

    return os.environ.get("ENSEMBLE_REPORT_FUNCTION", "")


async def dispatch(db, ensemble_id: str, *, force: bool = False) -> bool:
    """Ask the report Lambda to build this ensemble's report. True if it was dispatched.

    Asynchronous (`InvocationType="Event"`), so the caller returns immediately. That is the
    point: the caller is a member run's `finalise`, and a 15-minute report must not be able to
    time out the state that writes a run's terminal status.

    Returns False when there is no report function configured, which tells the caller to do the
    work itself.
    """
    name = report_function_name()
    if not name:
        return False

    import asyncio as _asyncio
    import json
    import os

    parent = await db.get_ensemble(ensemble_id)
    owner = (parent or {}).get("owner_sub")
    if not owner:
        logger.warning(
            "Ensemble %s has no owner on its row, so no report can be dispatched — the "
            "function assumes the tenant role per request and there is no identity to assume "
            "it for.", ensemble_id,
        )
        return False

    def _invoke() -> None:
        import boto3

        boto3.client("lambda", region_name=os.environ.get("AWS_REGION")).invoke(
            FunctionName=name,
            # Event, not RequestResponse: a synchronous invoke would make the caller wait the
            # full report and reintroduce exactly the timeout this move exists to remove.
            InvocationType="Event",
            Payload=json.dumps(
                {"ensemble_id": ensemble_id, "owner_sub": str(owner), "force": bool(force)}
            ).encode(),
        )

    await _asyncio.to_thread(_invoke)
    logger.info("Dispatched the report for ensemble %s to %s", ensemble_id, name)
    return True


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
        # Hand it to the dedicated function where there is one, and only do the work here when
        # there is not (local, tests). The claim is taken by whoever actually builds it, not by
        # the dispatcher — dispatching then claiming would mean a dropped invoke leaves a claim
        # held by nobody.
        if await dispatch(db, str(ensemble_id)):
            return
        await generate(db, str(ensemble_id))
    except Exception:  # noqa: BLE001
        logger.exception(
            "Ensemble %s report attempt failed after member %s finished; the run itself is "
            "unaffected", ensemble_id, run.get("id"),
        )
