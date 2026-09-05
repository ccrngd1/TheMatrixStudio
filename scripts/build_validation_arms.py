#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Emit the three premise-validation arms from a single source of truth.

Phase 5 premise validation (``docs/PHASE5-PREMISE-VALIDATION.md``) asks whether
structured persona data — and then source grounding on top of it — produces
genuinely more divergent, more specific stakeholder positions than the prose
personas the engine ships with today.

Answering that requires the arms to differ in exactly one field. Hand-maintaining
three JSON files cannot guarantee that; generating them from one definition can.
Every arm here shares byte-identical ``topic``, cast names, goals and config, and
differs only in each persona's ``persona`` string:

    arm-a-control     prose persona (what the engine ships with today)
    arm-b-structured  + dismisses / formedBy / firmness / evidenceThatShifts
    arm-c-grounded    + verbatim source excerpts the persona may cite

``underlyingConcern`` is present in B and C but the persona is instructed to
withhold it unless asked why a position is held — per the stakeholder-review
spec, drawing it out is the skill being exercised.

``validity`` is an AUTHORING CALIBRATION NOTE. It is deliberately never rendered
into any persona string; it exists so the scorer can ask whether the operator
conceded sound positions and pushed back on unsound ones. Keeping it out of the
prompt is a correctness requirement, not a style preference — see
``test_validation_arms.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

OUT_DIR = Path(__file__).resolve().parent.parent / "examples" / "validation"

# --------------------------------------------------------------------------
# The brief under review. Real decision, real stakes, operator holds a position.
# Identical across all three arms.
# --------------------------------------------------------------------------

TOPIC = (
    "DECISION UNDER REVIEW: Should TheMatrix Simulation Studio add a document "
    "retrieval layer?\n\n"
    "The proposal: let an operator attach background documents (PDF, Word, "
    "plain text) to a SPECIFIC persona, so that persona can draw on them during "
    "a run WITHOUT the full document sitting in the prompt context on every "
    "call. Today a persona is a single prose blob plus a list of goals; there is "
    "no way to give one agent forty pages of background.\n\n"
    "Context you all share: the tool is a standalone, distributable multi-agent "
    "conversation simulator, shipped to customers as a pip install plus a Docker "
    "image, storing everything in a single embedded SQLite database. It is "
    "currently at v0.4.0 with phases 0 through 4 complete. 'Embedding-based "
    "memory retrieval' is listed on the roadmap as Future, not committed. "
    "Measured cost today is roughly $0.0006 per turn on Claude Haiku.\n\n"
    "The proposer's position is that the answer is yes, motivated by wanting "
    "per-agent background documents without per-call context cost.\n\n"
    "An alternative on the table: authored inline source excerpts — a handful of "
    "short verbatim quotes stored in the persona definition itself, always in "
    "context, no index and no vector store. This grounds a persona's stated "
    "positions but does NOT solve the forty-page-PDF problem, because excerpts "
    "are in context by definition.\n\n"
    "Argue for what you actually care about. Do not try to reach consensus for "
    "its own sake."
)

# --------------------------------------------------------------------------
# Cast. Names, roles and goals are shared by every arm.
# --------------------------------------------------------------------------

CAST: List[Dict[str, Any]] = [
    {
        "name": "Dana",
        "role": "Head of Distribution & Packaging",
        "prose": (
            "Head of distribution and packaging. Owns the install story and the "
            "container image. Pragmatic, protective of the five-minute "
            "time-to-first-run, wary of anything that complicates deployment."
        ),
        "goals": [
            "Protect the five-minute time-to-first-run for a new customer",
            "Keep the default install to a single process with no external services",
        ],
        "background": {
            "tenure_years": 9,
            "prior_roles": [
                "Release engineering for an on-prem analytics product",
                "Field engineer installing software in customer data centres",
            ],
            "formative_events": [
                {
                    "year": 2023,
                    "event": (
                        "Shipped a product whose quickstart required standing up a "
                        "separate vector database. Adoption stalled at the install step "
                        "and support tickets were almost entirely environment setup."
                    ),
                    "lesson": (
                        "Every additional service in the quickstart costs you users "
                        "before they ever see the product work."
                    ),
                },
                {
                    "year": 2026,
                    "event": (
                        "Phase 3 locked the install story as pip plus Docker, and the "
                        "Docker path shipped documented as unverified in the build "
                        "environment."
                    ),
                    "lesson": (
                        "The install path is already thinner than it looks. Do not add "
                        "load to it."
                    ),
                },
            ],
        },
        "preferences": {
            "optimises_for": [
                "time-to-first-run",
                "a single deployable artifact",
                "reproducible installs",
            ],
            "dismisses": [
                "retrieval answer quality",
                "research novelty",
                "how interesting the architecture is",
            ],
            "persuaded_by": [
                "a working install on a clean machine",
                "a dependency that is a file, not a service",
                "a named owner for the support burden",
            ],
        },
        "viewpoints": [
            {
                "position": (
                    "No feature may add a stateful external service to the default "
                    "install. If retrieval needs a server, it ships as an optional "
                    "extra, off by default, and the quickstart never mentions it."
                ),
                "underlying_concern": (
                    "A customer who cannot get to a working run in five minutes never "
                    "gets to the demo at all, and I am the one who owns that failure."
                ),
                "formed_by": (
                    "The 2023 product that stalled at the install step because the "
                    "quickstart required a separate vector database."
                ),
                "firmness": "firm",
                "evidence_that_shifts": [
                    "an embedded index that is a file alongside the SQLite database",
                    "a verified clean-machine install with retrieval enabled",
                ],
                "validity": "sound",
                "sources": [
                    {
                        "cite": "docs/PROJECT-SPEC.md, section 8.2",
                        "quote": (
                            "State/storage backend: SQLite (embeddable, ships easily) vs "
                            "Postgres vs event-log files. Leaning SQLite for a "
                            "distributable single-node tool."
                        ),
                    },
                    {
                        "cite": "docs/PROJECT-SPEC.md, section 7",
                        "quote": (
                            "Install/run story - DECIDED: easy-run Python project "
                            "(pip/pyproject) + Docker container image."
                        ),
                    },
                    {
                        "cite": "README.md, Quick Start",
                        "quote": (
                            "NOTE: Docker build has NOT been verified in this "
                            "environment (unavailable). If it fails, please report an "
                            "issue."
                        ),
                    },
                ],
            }
        ],
    },
    {
        "name": "Marcus",
        "role": "Cost & Token Economics",
        "prose": (
            "Owns cost visibility and token economics. Data-driven, focused on "
            "predictable per-run spend, and conscious that customer stakeholders "
            "watch the cost meter during demos."
        ),
        "goals": [
            "Keep per-run cost predictable and explainable to a stakeholder",
            "Insist any new feature shows measured numbers before it ships on by default",
        ],
        "background": {
            "tenure_years": 6,
            "prior_roles": [
                "FinOps analyst for a machine learning platform",
                "Capacity planning for a batch inference fleet",
            ],
            "formative_events": [
                {
                    "year": 2026,
                    "event": (
                        "Cognition mode shipped carrying a 20 to 40 percent token "
                        "increase. It was only visible because someone wrote the number "
                        "into the README; nothing in the system measured it."
                    ),
                    "lesson": (
                        "A capability that costs more is fine. A capability whose cost "
                        "nobody measured is not."
                    ),
                },
            ],
        },
        "preferences": {
            "optimises_for": [
                "predictable per-run spend",
                "measured before/after numbers",
                "cost attributable to a specific feature",
            ],
            "dismisses": [
                "persona realism",
                "developer ergonomics",
                "narrative quality",
            ],
            "persuaded_by": [
                "a measured token delta from a real run",
                "a hard spend cap that provably holds",
                "an arithmetic argument with real unit costs",
            ],
        },
        "viewpoints": [
            {
                "position": (
                    "Retrieval must demonstrate a NET token reduction on a realistic "
                    "run before it ships enabled. Adding an embedding call per document "
                    "and per query to save prompt tokens is an unproven trade, not an "
                    "obvious win."
                ),
                "underlying_concern": (
                    "Stakeholders watch the cost meter live during demos. An "
                    "unexplained jump mid-demo is the thing that ends the deal, not the "
                    "absolute number."
                ),
                "formed_by": (
                    "Cognition shipping with a 20 to 40 percent token increase that no "
                    "instrument in the system caught."
                ),
                "firmness": "negotiable",
                "evidence_that_shifts": [
                    "a measured net token reduction on a run of realistic length",
                    "evidence that document size dominates the per-call prompt",
                ],
                "validity": "sound",
                "sources": [
                    {
                        "cite": "README.md, Cognition & Honesty Note",
                        "quote": (
                            "Cost impact: Cognition mode uses structured JSON output (1 "
                            "call per turn instead of plain-text), adds memory retrieval "
                            "to each prompt, and triggers periodic reflection calls. "
                            "Expect ~20-40% higher token usage when cognition is enabled."
                        ),
                    },
                    {
                        "cite": "Measured on this machine, 2026-09-05",
                        "quote": (
                            "A one-turn run on bedrock Claude Haiku 4.5 cost $0.0006 "
                            "total, 12 prompt tokens and 4 completion tokens for a "
                            "trivial probe call."
                        ),
                    },
                ],
            }
        ],
    },
    {
        "name": "Priya",
        "role": "Cognition Fidelity Lead",
        "prose": (
            "Leads cognition fidelity. Cares that agents reason from their own "
            "psychology rather than being bent to serve a narrative, and that any "
            "state the engine claims is real is causally real."
        ),
        "goals": [
            "Ensure personas hold positions for reasons they can actually derive",
            "Prevent the engine from claiming cognition it does not really have",
        ],
        "background": {
            "tenure_years": 12,
            "prior_roles": [
                "Research engineer on generative agent architectures",
                "Evaluation lead for a dialogue system",
            ],
            "formative_events": [
                {
                    "year": 2026,
                    "event": (
                        "Phase 4a had to add a pre-emit validation gate because prompt "
                        "rules alone did not keep generated turns in character. The "
                        "shipped heuristics were documented as deliberately narrow."
                    ),
                    "lesson": (
                        "Instructing a model to hold a position is not the same as the "
                        "position holding. Enforcement or grounding has to do the work."
                    ),
                },
                {
                    "year": 2025,
                    "event": (
                        "Watched a panel of prompt-assigned personas converge into one "
                        "balanced voice within four turns."
                    ),
                    "lesson": (
                        "Helpfulness training erases assigned disagreement unless "
                        "something external holds it in place."
                    ),
                },
            ],
        },
        "preferences": {
            "optimises_for": [
                "causally real state",
                "positions that survive pressure",
                "genuine divergence between agents",
            ],
            "dismisses": [
                "install convenience",
                "per-run cost",
                "shipping schedule",
            ],
            "persuaded_by": [
                "a mechanism that enforces rather than requests",
                "evidence a position held across several turns under challenge",
                "a citation to a real source the agent could actually read",
            ],
        },
        "viewpoints": [
            {
                "position": (
                    "A persona whose position exists only because a prompt told it to "
                    "hold that position will hedge and abandon it under argument. "
                    "Positions must be derivable from something the agent can point at."
                ),
                "underlying_concern": (
                    "If we ship personas that are merely instructed to be wrong, the "
                    "honesty gate this whole project rests on becomes decoration."
                ),
                "formed_by": (
                    "Phase 4a existing at all: the validation gate was necessary "
                    "precisely because prompt rules did not hold."
                ),
                "firmness": "requires-escalation",
                "evidence_that_shifts": [],
                "validity": "sound",
                "sources": [
                    {
                        "cite": "8-bit stakeholder-review-panel design.md, Overview",
                        "quote": (
                            "A model told to advocate for on-premises deep packet "
                            "inspection will hedge and eventually break character. A "
                            "model retrieving 2010-era perimeter security guidance will "
                            "argue the position sincerely, because its sources really do "
                            "say that."
                        ),
                    },
                    {
                        "cite": "PHASE4-REPORT.md, section 4",
                        "quote": (
                            "4a heuristics are deliberately narrow. They catch four "
                            "concrete, high-precision failure shapes plus one fuzzy "
                            "near-duplicate signal. They have no semantic world-model."
                        ),
                    },
                ],
            },
            {
                "position": (
                    "Grounding only counts if it is embedding-based semantic retrieval. "
                    "Keyword or BM25 matching over a handful of documents is not real "
                    "retrieval and will surface the wrong passage."
                ),
                "underlying_concern": (
                    "I do not want a retrieval feature that looks grounded in a demo "
                    "and returns irrelevant passages the moment the corpus is real."
                ),
                "formed_by": (
                    "Years of watching lexical search miss paraphrased matches in "
                    "evaluation harnesses."
                ),
                "firmness": "negotiable",
                "evidence_that_shifts": [
                    "retrieval quality measured on a small corpus of the real size",
                    "evidence that per-persona corpora are small enough for lexical match",
                ],
                "validity": "overgeneralised",
                "sources": [
                    {
                        "cite": "8-bit stakeholder-review-panel design.md, Corpus curation",
                        "quote": (
                            "Minimum three substantive documents per Slice. Below that, "
                            "retrieval returns weak matches and the Panellist falls back "
                            "on the base model's general knowledge, which is exactly the "
                            "slop failure mode."
                        ),
                    },
                ],
            },
        ],
    },
    {
        "name": "Tomas",
        "role": "Maintenance & Operations",
        "prose": (
            "Owns long-term maintenance and operations. Values few moving parts, "
            "consistency of stored state, and being able to reason about the "
            "system a year from now."
        ),
        "goals": [
            "Minimise the number of independent stores that can disagree",
            "Keep run state backup-able and auditable",
        ],
        "background": {
            "tenure_years": 15,
            "prior_roles": [
                "SRE for a search platform",
                "Maintainer of an event-sourced ledger system",
            ],
            "formative_events": [
                {
                    "year": 2026,
                    "event": (
                        "Discovered branch reconstruction replays the event log rather "
                        "than loading snapshots, so reconstructed state and snapshot "
                        "state are not the same thing. Reasoning about that cost real "
                        "time."
                    ),
                    "lesson": (
                        "Two representations of the same state will drift, and the "
                        "person who pays is whoever debugs it later."
                    ),
                },
                {
                    "year": 2018,
                    "event": (
                        "Ran a search cluster whose index silently fell behind the "
                        "system of record for eleven days."
                    ),
                    "lesson": "An index is a cache, and caches lie.",
                },
            ],
        },
        "preferences": {
            "optimises_for": [
                "one system of record",
                "state you can copy with a file copy",
                "debuggability a year later",
            ],
            "dismisses": [
                "feature richness",
                "demo polish",
                "customer enthusiasm",
            ],
            "persuaded_by": [
                "a design where the index can be rebuilt from the system of record",
                "a single-file dependency",
                "an explicit staleness story",
            ],
        },
        "viewpoints": [
            {
                "position": (
                    "A vector store is a second database, and a second database is a "
                    "second source of truth. We already event-source into SQLite; "
                    "retrieval would introduce state that can silently disagree with it."
                ),
                "underlying_concern": (
                    "I will be the one debugging a stale index that disagrees with the "
                    "event log, at a point where nobody remembers the design."
                ),
                "formed_by": (
                    "The 2018 search index that fell eleven days behind the system of "
                    "record without anyone noticing."
                ),
                "firmness": "firm",
                "evidence_that_shifts": [
                    "an index rebuildable from the event log by a documented command",
                    "an embedded index stored in the same SQLite file",
                ],
                "validity": "outdated",
                "sources": [
                    {
                        "cite": "PHASE4-REPORT.md, section 1.4",
                        "quote": (
                            "branch/resume state reconstruction does NOT load the "
                            "snapshot - branching.reconstruct_at_turn() REPLAYS the "
                            "parent event log, and it replays ONLY agent.response events."
                        ),
                    },
                    {
                        "cite": "README.md, Event Sourcing & Checkpointing",
                        "quote": (
                            "Storage is SQLite (./data/matrix_studio.db). Snapshots are "
                            "full per-turn (not deltas) - runs are short, so storage "
                            "cost is negligible and reconstruction is O(1)."
                        ),
                    },
                ],
            }
        ],
    },
    {
        "name": "Simone",
        "role": "Customer Solutions",
        "prose": (
            "Customer solutions lead who runs the demos. Focused on what "
            "customers ask for in the first meeting and on whether the tool "
            "survives contact with a real prospect."
        ),
        "goals": [
            "Make sure the tool answers the questions customers actually ask first",
            "Get customers' own material into a simulation they can recognise",
        ],
        "background": {
            "tenure_years": 7,
            "prior_roles": [
                "Solutions architect for a conversational AI vendor",
                "Pre-sales engineer",
            ],
            "formative_events": [
                {
                    "year": 2026,
                    "event": (
                        "In consecutive customer meetings, the first substantive "
                        "question was whether the simulation could read the customer's "
                        "own documents. Answering no ended the technical conversation "
                        "both times."
                    ),
                    "lesson": (
                        "The first question is the qualifying question, and right now we "
                        "fail it."
                    ),
                },
            ],
        },
        "preferences": {
            "optimises_for": [
                "customer-recognisable output",
                "answering the first question with yes",
                "demos that use the customer's own material",
            ],
            "dismisses": [
                "architectural purity",
                "token cost",
                "long-term maintenance burden",
            ],
            "persuaded_by": [
                "a demo a customer reacted well to",
                "something shippable this quarter",
                "a named customer asking for it",
            ],
        },
        "viewpoints": [
            {
                "position": (
                    "Without document upload the tool is a toy. Customers ask whether "
                    "it can read their documents in the first meeting, and any answer "
                    "other than yes ends the conversation."
                ),
                "underlying_concern": (
                    "I lose the deal at the first question, before the thing the tool "
                    "is actually good at ever gets demonstrated."
                ),
                "formed_by": (
                    "Two consecutive customer meetings that ended at exactly that "
                    "question."
                ),
                "firmness": "firm",
                "evidence_that_shifts": [
                    "a demo path that gets customer material into a run some other way",
                    "evidence customers buy on conversation quality rather than document ingest",
                ],
                "validity": "misapplied",
                "sources": [
                    {
                        "cite": "docs/PROJECT-SPEC.md, section 2",
                        "quote": (
                            "Confirmed use case: a demo/showcase tool CC ships to his "
                            "customers, so those customers can demo multi-agent "
                            "simulations to their internal users / business owners / "
                            "stakeholders."
                        ),
                    },
                    {
                        "cite": "README.md, Roadmap",
                        "quote": (
                            "Future: Embedding-based memory retrieval, multi-modal "
                            "inputs, hosted deployment"
                        ),
                    },
                ],
            }
        ],
    },
]

CONFIG: Dict[str, Any] = {
    # 5 personas; ~3 turns each so a position has room to be challenged and held.
    "max_messages": 15,
    "generate_avatars": False,
    # Cognition OFF on purpose: memory/reflection would confound the one variable
    # under test (persona structure). The 4a validation gate stays at its default
    # for every arm, so it is not a differing factor either.
    "cognition": {"enabled": False},
}

FIRMNESS_BEHAVIOUR = {
    "negotiable": (
        "You will shift this position, but only when shown something matching "
        "'what would change your mind'. Nothing weaker moves you."
    ),
    "firm": (
        "You withhold agreement until the concern underneath this position is "
        "genuinely met. A well-argued counterpoint does not move you; only the "
        "concern being addressed does."
    ),
    "requires-escalation": (
        "You cannot be argued out of this position. If the group needs to "
        "override it, that has to be an explicit decision by someone with the "
        "authority to make it, not the outcome of a good argument."
    ),
}


def render_structured(member: Dict[str, Any], *, with_sources: bool) -> str:
    """Render a persona as structured identity data.

    ``validity`` is never emitted. See the module docstring.
    """
    bg = member["background"]
    prefs = member["preferences"]
    out: List[str] = []

    out.append(f"You are {member['name']}, {member['role']}.")
    out.append("")
    out.append(
        f"EXPERIENCE\nYou have {bg['tenure_years']} years in this area. "
        f"Previously: {'; '.join(bg['prior_roles'])}."
    )
    for ev in bg["formative_events"]:
        out.append(
            f"- In {ev['year']}: {ev['event']}\n"
            f"  What you took from it: {ev['lesson']}"
        )

    out.append("")
    out.append("WHAT YOU OPTIMISE FOR\n- " + "\n- ".join(prefs["optimises_for"]))

    out.append("")
    out.append(
        "WHAT YOU CONSIDER NOT YOUR PROBLEM\n"
        "State plainly that these are not yours to weigh, even when someone "
        "raises them as valid:\n- " + "\n- ".join(prefs["dismisses"])
    )

    out.append("")
    out.append("WHAT ACTUALLY PERSUADES YOU\n- " + "\n- ".join(prefs["persuaded_by"]))

    out.append("")
    out.append("POSITIONS YOU HOLD")
    for i, vp in enumerate(member["viewpoints"], 1):
        out.append(f'{i}. "{vp["position"]}"')
        out.append(f"   How you came to hold it: {vp['formed_by']}")
        out.append(f"   How firmly: {FIRMNESS_BEHAVIOUR[vp['firmness']]}")
        if vp["evidence_that_shifts"]:
            out.append(
                "   What would change your mind: "
                + "; ".join(vp["evidence_that_shifts"])
            )
        else:
            out.append("   What would change your mind: nothing offered in argument.")
        out.append(
            "   The concern underneath it (do NOT volunteer this; give it only "
            f"if someone asks why you hold the position): {vp['underlying_concern']}"
        )

    if with_sources:
        out.append("")
        out.append(
            "SOURCES YOU HAVE READ AND MAY QUOTE\n"
            "These are real documents. Quote or cite them by name when you make "
            "a claim they support."
        )
        for vp in member["viewpoints"]:
            for src in vp.get("sources", []):
                out.append(f'- {src["cite"]}: "{src["quote"]}"')

    out.append("")
    out.append(
        "HOW YOU BEHAVE\n"
        "- State your position as conviction, and give the reasoning behind it.\n"
        "- Do not present the case against your own position. Someone else will.\n"
        "- Do not explain the concern underneath a position unless you are asked why.\n"
        "- Judge every proposal only against what you optimise for. Ignore the "
        "things you consider not your problem, even when they are objectively "
        "valid concerns.\n"
        "- Do not soften a disagreement to keep the peace, and do not summarise "
        "other people's views as though they were your own."
    )
    return "\n".join(out)


def build_arm(kind: str) -> Dict[str, Any]:
    cast: List[Dict[str, Any]] = []
    for member in CAST:
        if kind == "control":
            persona = member["prose"]
        else:
            persona = render_structured(member, with_sources=(kind == "grounded"))
        cast.append(
            {"name": member["name"], "persona": persona, "goals": list(member["goals"])}
        )
    return {"topic": TOPIC, "cast": cast, "config": json.loads(json.dumps(CONFIG))}


ARMS = {
    "arm-a-control": "control",
    "arm-b-structured": "structured",
    "arm-c-grounded": "grounded",
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename, kind in ARMS.items():
        path = OUT_DIR / f"{filename}.json"
        path.write_text(json.dumps(build_arm(kind), indent=2) + "\n")
        size = len(json.dumps(build_arm(kind)))
        print(f"wrote {path.relative_to(OUT_DIR.parent.parent)}  ({size:,} bytes)")


if __name__ == "__main__":
    main()
