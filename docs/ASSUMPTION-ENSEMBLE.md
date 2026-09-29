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
