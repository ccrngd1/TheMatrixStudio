# What one working assumption is worth — pre-registration

**Status:** pre-registered 2026-09-29, before any run. Criteria not to be edited after the runs start.

## Why

A fork with a different assumption is one draw per value, which cannot say whether a difference is the
assumption or chance. `ensemble_spec` now allows `assumptions` as a cell override, so the same brief can be run
several times under each value and counted per cell. This is also the first live use of that override.

## Design

The operator's renewal brief (private definitions; the settings of the EVIDENCE-LEAN runs, with today's defaults —
evidence lean on in both cells). **Two cells of three runs**, identical except one operator assumption about how
often the original prescriber's records can be retrieved in time: **about 85%** in one cell, **about 30%** in the
other. Everything else, including the cast and turn count, is identical, which `ensemble_spec.check_isolated`
verifies before creation. The ensemble report is generated as for any ensemble.

## Criteria — decided now

- **Primary — an attributable effect.** At least one conclusion in the ensemble report recurs in **≥ 2 of 3** runs
  of one cell and in **0 of 3** of the other. Met → the assumption changed what the room concluded, at this n.
  Missed → no attributable effect detected; not evidence of no effect.
- **Reported, not decisive:** each cell's recurring conclusions; how often the assumption was cited and disputed
  per run (`assumptions.usage`); `position.shift` flags crediting it.
- **What n = 3 per cell can say.** Whether one assumption can visibly move a room's conclusions. Not how much, and
  not on any other brief.

## Result — 2026-09-29

Ensemble `311d6cc0`, six members, all complete at 40 turns; report generated. Isolation held: the six stored
member configs are identical apart from `assumptions`, and each cell carries one value. Scored against the
criterion above as committed (`529fec1`).

| | about 85% retrievable | about 30% retrievable |
|---|---|---|
| runs | 3 | 3 |
| clustered conclusions reached in ≥ 2 of 3 runs | 2 | 3 |
| messages citing the assumption by id | 12 | 6 |
| messages flagged as disputing it | 0 | 0 |
| `position.shift` flags / crediting the assumption | 3 / 0 | 3 / 0 |
| flags naming none of the persona's stated conditions | 0 | 2 |
| cost (in-run + summaries) | $3.83 | $3.82 |

Report cost $1.67; ensemble total **$9.33** (forecast $5.63–10.57, typical $8.08).

- **Primary: MET.** One conclusion recurs in 2 of 3 runs of the ~30% cell and 0 of 3 of the ~85% cell: rejecting
  an attestation-only pathway with no clinician review as insufficient. No conclusion splits the other way.
- **Read with its size.** The report clustered 32 distinct conclusions; 28 were reached in a single run only, and
  three others recur in both cells. One 2–0 split among 32 candidates is the weakest form the criterion allows, and
  with that many comparisons a split of that size can arise by chance. It is consistent with the assumption
  mattering — a scarce-records world rejecting a no-records path — and does not establish it.
- **The room reasoned from it without contesting it:** cited in 18 messages, disputed in none, and no announced
  change of position credited it.
- **Machinery, first live use with an `assumptions` override:** cell creation, isolation, delivery of the
  assumption at turn 0 in every member, and the per-cell report all worked as designed. One run in the ~85% cell
  recorded adopting the assumption itself as a conclusion — the room repeating its premise back — which the
  report counts like any other conclusion.

**What n = 3 per cell can say.** That this assumption can plausibly move a conclusion on this brief, at the limit
of what the criterion detects. Not how large the effect is, and nothing about other briefs or other assumptions.
