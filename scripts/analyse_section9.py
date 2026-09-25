#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Score §9's comparison exactly as `docs/PERSONA-RESEARCH.md` §9.1 pre-registered it.

§9.1 was committed (9df3c1a) before either arm existed, and this script implements it rather than
re-deciding it. Nothing here chooses a threshold, a matching rule or a denominator — each is a
constant copied from §9.1, with the section it came from next to it. If a result looks wrong, the
remedy is a new pre-registration for a new run, not an edit to a constant here.

Reads only stored data: the two ensembles' parent rows, their members' event logs, and their
reports. Costs nothing and writes nothing.

Usage:
    export AWS_REGION=us-east-1 TABLE_PREFIX=matrix-studio DATA_BUCKET=...
    scripts/analyse_section9.py --owner SUB --control ENSEMBLE_ID --research ENSEMBLE_ID
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix_studio.storage import Database  # noqa: E402

# --------------------------------------------------------------------------- #
# Constants copied from §9.1. Do not tune these against the result.
# --------------------------------------------------------------------------- #

#: §9.1 "Censoring": a run that hits the ceiling is scored AT the ceiling, and counted.
CEILING = 40

#: §9.1 1a: research runs in which some turn saw a controlling authority or the negative.
C1A_MIN_RUNS = 4

#: §9.1 1b's matching rule — the AMENDED version (§9.1 "Amendment 1").
#:
#: The rule as first registered missed the room's own wording for exactly the question §9 is about.
#: Calibrated on two PRIOR ensembles (renewal-cells, renewal-ens), before either §9 arm had a
#: report: the most-repeated open question — "Whether CA's 'treatment of whatever nature' language
#: has been interpreted to reach plan renewal", left open in 5 runs — matched none of the
#: original terms, nor did "practice-act" (hyphenated), "board enforcement history", "legally
#: sufficient" or "statutory breadth". A rule that cannot see the question cannot tell whether it
#: was answered.
#:
#: Word-bounded regexes, so `board` cannot match "onboarding" or "dashboard".
C1B_PATTERNS = tuple(re.compile(p) for p in (
    r"\bstatut(e|es|ory)\b",
    r"\bpractice[- ]acts?\b",
    r"\bboards?\b",
    r"\bregulat(ion|ions|ory)\b",
    r"\blegal(ly)?\b",
    r"\bcase law\b",
    r"\bcontrolling\b",
    r"\binterpret(ed|ation|s)?\b",
    r"\benforcement\b",
    r"\bpracticing licensed medicine\b",
    r"\bregulated product\b",
    r"\blegend\b",
    r"\blaw\b",
    r"\bcitations?\b",
))

#: §9.1: criteria 2 and 3 fail only on a gap larger than HALF the control cell's range.
SPREAD_FRACTION = 0.5

#: §9.1 criterion 4: research must keep at least this share of the control's curated passages.
C4_MIN_SHARE = 0.5

NEGATIVE_TITLE = "No controlling authority found"

#: §9.3 R2's existence rule, verbatim from the pre-registration (dfa0249): a legal question (the
#: amended 1b rule) that asks whether an authority EXISTS. Calibrated on renewal-cells and
#: renewal-ens only — not on §9 run 1, whose texts were read in §9.2.
EXISTENCE_PATTERNS = tuple(re.compile(p) for p in (
    r"\bany\b", r"\bexists?\b", r"\bexistence\b", r"\bunsurveyed\b", r"\bother states\b",
    r"\bsourced\b",
))

#: §9.3 R1: research runs in which a per-query negative reached a prompt, or the run is VOID.
R1_MIN_RUNS = 3

#: §9.3 R2: a PASS needs research to be at least this many runs better than control.
R2_MARGIN = 2


def _payload(event: Dict[str, Any]) -> Dict[str, Any]:
    p = event.get("payload")
    if isinstance(p, str):
        try:
            return json.loads(p)
        except json.JSONDecodeError:
            return {}
    return p or {}


def matches_1b(text: str) -> bool:
    t = text.lower()
    return any(p.search(t) for p in C1B_PATTERNS)


def is_existence(text: str) -> bool:
    t = text.lower()
    return matches_1b(text) and any(p.search(t) for p in EXISTENCE_PATTERNS)


async def score_arm(db: Any, ensemble_id: str) -> Dict[str, Any]:
    parent = await db.get_ensemble(ensemble_id)
    if parent is None:
        raise SystemExit(f"ensemble {ensemble_id} does not exist")
    members = json.loads(parent.get("members_json") or "[]")
    report = json.loads(parent["report_json"]) if parent.get("report_json") else None

    cast_names = {str(c.get("name") or "") for c in json.loads(parent.get("cast_json") or "[]")}
    runs: List[Dict[str, Any]] = []
    for m in members:
        run_id = str(m["run_id"])
        row = await db.get_run(run_id)
        events = await db.get_events(run_id) if row else []
        done = [e for e in events if e["event_type"] == "sim.completed"]
        completion = _payload(done[-1]) if done else {}
        stats = await db.get_run_stats(run_id) if row else {}
        turns = int(stats.get("turn_count") or 0)
        converged = bool(completion.get("converged"))

        # Criterion 4 and 1a are per TURN, from what the prompt was actually given.
        curated_per_turn: List[int] = []
        saw_authority = False
        saw_query_negative = False
        for e in events:
            if e["event_type"] != "document.retrieved":
                continue
            passages = _payload(e).get("passages") or []
            curated_per_turn.append(sum(1 for p in passages if p.get("origin") != "researched"))
            for p in passages:
                if p.get("authority") == "controlling" or str(p.get("title") or "").startswith(
                    NEGATIVE_TITLE
                ):
                    saw_authority = True
                title = str(p.get("title") or "")
                # A query negative's title carries the query, which is never a cast member's name.
                if title.startswith(f"{NEGATIVE_TITLE} — ") and title[len(NEGATIVE_TITLE) + 3:] \
                        not in cast_names:
                    saw_query_negative = True

        runs.append({
            "run_id": run_id,
            "name": (row or {}).get("name"),
            "status": (row or {}).get("status"),
            # §9.1 censoring: a run that did not converge is scored at the ceiling, never
            # dropped. `turns` can read below the ceiling for a run that FAILED, which is why
            # the status is carried and reported alongside it.
            "score_turns": turns if converged else CEILING,
            "turns": turns,
            "converged": converged,
            "converged_at": completion.get("converged_at_turn"),
            "curated_per_turn": (
                statistics.mean(curated_per_turn) if curated_per_turn else None
            ),
            "retrieval_turns": len(curated_per_turn),
            "saw_authority": saw_authority,
            "saw_query_negative": saw_query_negative,
            "cost_usd": float(stats.get("total_cost_usd") or 0.0),
        })

    # The report identifies a run by NAME ("s9-control-base1"), not by run id. Keyed on both,
    # because matching on id alone silently counted every concession as zero — caught by running
    # this against a finished ensemble before scoring §9, where 0 vs 0 would have read as a pass.
    by_id = {r["run_id"]: r for r in runs}
    by_id.update({r["name"]: r for r in runs if r.get("name")})
    for r in runs:
        r["concessions"] = 0
        r["unresolved_1b"] = []
        r["unresolved_existence"] = []

    if report:
        # Concessions: every participant's, per run. From `per_persona`, which lists them
        # individually with the run they came from — so this is a count, not a clustered label.
        for persona in (report.get("per_persona") or {}).values():
            for c in persona.get("concessions") or []:
                if c.get("run") in by_id:
                    by_id[c["run"]]["concessions"] += 1
        # 1b. Open questions are NOT in `claims` — `_claims_by_cell` excludes them, because an
        # open question is not a position anybody held. They live in
        # `agreements.unresolved_by_frequency`, one entry per CLUSTER: a representative raw text
        # (the first run's wording, not a generated label) and every run clustered with it.
        #
        # §9.1 as first written said "the extraction's unresolved list" per run; per-run lists are
        # not persisted, so the rule is applied to each cluster's representative and credited to
        # every run in it. Recorded as an amendment in §9.1, dated, before either arm had a report.
        for item in ((report.get("agreements") or {}).get("unresolved_by_frequency") or []):
            text = str(item.get("claim") or "")
            if matches_1b(text):
                for run in item.get("runs") or []:
                    if run in by_id:
                        by_id[run]["unresolved_1b"].append(text)
            if is_existence(text):
                for run in item.get("runs") or []:
                    if run in by_id:
                        by_id[run]["unresolved_existence"].append(text)

    return {
        "ensemble_id": ensemble_id,
        "status": parent.get("status"),
        "has_report": report is not None,
        "report_error": parent.get("report_error"),
        "research": json.loads(parent["research_json"]) if parent.get("research_json") else None,
        "runs": runs,
    }


def _median(xs: List[float]) -> Optional[float]:
    return statistics.median(xs) if xs else None


def _range(xs: List[float]) -> float:
    return (max(xs) - min(xs)) if xs else 0.0


def verdict(control: Dict[str, Any], research: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    cr, rr = control["runs"], research["runs"]

    # --- 1a -------------------------------------------------------------- #
    n1a = sum(1 for r in rr if r["saw_authority"])
    out.append(
        f"1a  research reached the room   {n1a} of {len(rr)} research runs saw a controlling "
        f"authority or the documented negative  (needs >= {C1A_MIN_RUNS})  "
        + ("PASS" if n1a >= C1A_MIN_RUNS else "FAIL")
    )

    # --- 1b -------------------------------------------------------------- #
    c1b = sum(1 for r in cr if r["unresolved_1b"])
    r1b = sum(1 for r in rr if r["unresolved_1b"])
    out.append(
        f"1b  the question got answered   runs leaving a statute/board question unresolved: "
        f"control {c1b}/{len(cr)}, research {r1b}/{len(rr)}  (needs research < control)  "
        + ("PASS" if r1b < c1b else "FAIL")
    )

    # --- 2 --------------------------------------------------------------- #
    ct = [r["score_turns"] for r in cr]
    rt = [r["score_turns"] for r in rr]
    limit = _median(ct) + SPREAD_FRACTION * _range(ct)
    rm = _median(rt)
    if rm <= _median(ct):
        v2 = "PASS"
    elif rm <= limit:
        v2 = "NO DETECTABLE DIFFERENCE"
    else:
        v2 = "FAIL"
    out.append(
        f"2   convergence not worse       median turn: control {_median(ct)} "
        f"(range {_range(ct)}, {sum(r['converged'] for r in cr)}/{len(cr)} converged), research "
        f"{rm} ({sum(r['converged'] for r in rr)}/{len(rr)} converged)  "
        f"(fails above {limit})  {v2}"
    )

    # --- 3 --------------------------------------------------------------- #
    cc = [r["concessions"] for r in cr]
    rc = [r["concessions"] for r in rr]
    floor = _median(cc) - SPREAD_FRACTION * _range(cc)
    rcm = _median(rc)
    if rcm >= _median(cc):
        v3 = "PASS"
    elif rcm >= floor:
        v3 = "NO DETECTABLE DIFFERENCE"
    else:
        v3 = "FAIL"
    out.append(
        f"3   concessions not fewer       median per run: control {_median(cc)} "
        f"(range {_range(cc)}), research {rcm}  (fails below {floor})  {v3}"
    )

    # --- 4 --------------------------------------------------------------- #
    cp = [r["curated_per_turn"] for r in cr if r["curated_per_turn"] is not None]
    rp = [r["curated_per_turn"] for r in rr if r["curated_per_turn"] is not None]
    cm = statistics.mean(cp) if cp else 0.0
    rm4 = statistics.mean(rp) if rp else 0.0
    share = (rm4 / cm) if cm else 0.0
    out.append(
        f"4   the brief not crowded out   curated passages per turn: control {cm:.2f}, "
        f"research {rm4:.2f} = {share:.0%} of control  (needs >= {C4_MIN_SHARE:.0%})  "
        + ("PASS" if share >= C4_MIN_SHARE else "FAIL")
    )
    return out


def verdict_run2(control: Dict[str, Any], research: Dict[str, Any]) -> List[str]:
    """§9.3, as pre-registered in dfa0249. R1 gates: if it fails the run is VOID, not failed."""
    cr, rr = control["runs"], research["runs"]
    out: List[str] = []
    produced = sum(
        int(s.get("query_negatives") or 0)
        for s in ((research.get("research") or {}).get("scopes") or [])
    )
    n1 = sum(1 for r in rr if r["saw_query_negative"])
    r1 = produced > 0 and n1 >= R1_MIN_RUNS
    out.append(
        f"R1  mechanism                   {produced} per-query negative(s) produced; reached a prompt "
        f"in {n1} of {len(rr)} research runs  (needs >= {R1_MIN_RUNS})  "
        + ("PASS" if r1 else "VOID — the fix was inert; nothing is concluded")
    )
    if not r1:
        return out
    ce = sum(1 for r in cr if r["unresolved_existence"])
    re_ = sum(1 for r in rr if r["unresolved_existence"])
    if re_ <= ce - R2_MARGIN:
        v = "PASS"
    elif re_ > ce:
        v = "FAIL"
    else:
        v = "NO DETECTABLE DIFFERENCE"
    out.append(
        f"R2  existence question settled  runs leaving one open: control {ce}/{len(cr)}, research "
        f"{re_}/{len(rr)}  (PASS needs research <= control - {R2_MARGIN})  {v}"
    )
    # Guardrails 2-4 exactly as §9.1; 1a/1b reported as secondary.
    for line in verdict(control, research):
        if line.startswith(("2 ", "3 ", "4 ")):
            out.append(line)
        else:
            out.append("(secondary) " + line)
    return out


def table(arm: str, scored: Dict[str, Any]) -> None:
    print(f"\n=== {arm}  ensemble {scored['ensemble_id']}  status={scored['status']}  "
          f"report={'yes' if scored['has_report'] else 'NO'}"
          + (f"  report_error={scored['report_error']}" if scored["report_error"] else ""))
    if scored["research"]:
        r = scored["research"]
        print(f"    research: {r.get('status')}  ${r.get('cost_usd')}  "
              f"{sum(s.get('documents', 0) for s in r.get('scopes') or [])} sources, "
              f"{sum(s.get('controlling', 0) for s in r.get('scopes') or [])} controlling")
    print(f"    {'run':<22} {'status':<9} {'turns':>5} {'conv':>5} {'score':>5} "
          f"{'concess':>7} {'curated/turn':>12} {'authority':>9} {'1b':>3}  cost")
    for r in scored["runs"]:
        cpt = f"{r['curated_per_turn']:.2f}" if r["curated_per_turn"] is not None else "—"
        print(f"    {str(r['name'])[:22]:<22} {str(r['status']):<9} {r['turns']:>5} "
              f"{('@' + str(r['converged_at'])) if r['converged'] else 'no':>5} "
              f"{r['score_turns']:>5} {r['concessions']:>7} {cpt:>12} "
              f"{'yes' if r['saw_authority'] else '—':>9} {len(r['unresolved_1b']):>3}  "
              f"${r['cost_usd']:.3f}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", required=True)
    ap.add_argument("--control", required=True)
    ap.add_argument("--research", required=True)
    ap.add_argument("--run", type=int, choices=(1, 2), default=1,
                    help="which pre-registration to score against: 1 = §9.1, 2 = §9.3")
    ap.add_argument("--show-1b", action="store_true",
                    help="print every unresolved item the 1b rule matched, for the hand reading "
                         "§9.1 asks to be reported alongside the rule")
    args = ap.parse_args()

    store = Database(
        table_prefix=os.environ.get("TABLE_PREFIX", "matrix-studio"),
        bucket=os.environ["DATA_BUCKET"],
        region=os.environ["AWS_REGION"],
    )
    await store.connect()
    try:
        db = store.for_owner(args.owner)
        control = await score_arm(db, args.control)
        research = await score_arm(db, args.research)
    finally:
        await store.close()

    table("CONTROL", control)
    table("RESEARCH", research)

    incomplete = [
        r for arm in (control, research) for r in arm["runs"] if r["status"] != "complete"
    ]
    missing_report = [a for a in ("control", "research")
                      if not {"control": control, "research": research}[a]["has_report"]]
    if incomplete or missing_report:
        print("\nNOT SCORED: the §9.1 verdict needs every run complete and both reports built.")
        for r in incomplete:
            print(f"    {r['name']} is {r['status']}")
        for a in missing_report:
            print(f"    the {a} report does not exist yet")
        return 2

    title = "§9.1" if args.run == 1 else "§9.3 (run 2)"
    print(f"\n=== {title} verdict ===")
    for line in (verdict if args.run == 1 else verdict_run2)(control, research):
        print("  " + line)

    if args.show_1b:
        print("\n=== 1b matches, for the hand reading ===")
        for arm, scored in (("control", control), ("research", research)):
            for r in scored["runs"]:
                for t in r["unresolved_1b"]:
                    tag = "EXIST" if t in r["unresolved_existence"] else "  -  "
                    print(f"  {tag} [{arm} {r['name']}] {t}")
    total = sum(r["cost_usd"] for a in (control, research) for r in a["runs"])
    print(f"\nconversation spend ${total:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
