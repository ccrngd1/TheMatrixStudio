# Assumptions made by the moderator — pre-registration

**Status:** pre-registered 2026-09-29, before any on-arm run. The criteria below are not to be edited
after the runs start; a result section is appended afterwards.

## Why

`config.dynamic_assumptions` lets the moderator add a working assumption when the room is stuck on an
unknown nobody in it can supply (`matrix_studio/assumptions.py`, slice B). It is opt-in. Two live smoke
runs are all the evidence so far: the first proposed at both of its chances, once with a plan; after the
verbatim-asks check, the second made one genuine fact and one plan. The question for the operator is
whether it can be a default, and that turns on two things n = 2 cannot answer: **does it assume the right
kind of thing**, and **does it fire at all** on a real brief.

## Design

- **On arm:** three new runs of the operator's six-persona renewal brief (private definitions), 40 turns,
  deployment default models, structured personas and cognition on, `evidence_lean` off —
  `dynamic_assumptions: {enabled: true, every: 4, limit: 3}`. Launched together.
- **Control:** the three off-arm runs of `docs/EVIDENCE-LEAN.md` (`a2b3b412`, `5c34a226`, `4a086d55`),
  whose definition is identical except for this flag. Declared here, before any on-arm result exists.
  **Caveat:** they ran on 2026-09-28, a day before the on arm, rather than alongside it. Models and
  settings are the same; nothing else in the engine changes a run without assumptions (verified by
  tests that the prompt is byte-identical when the feature is off).

## Labelling — defined before any assumption exists

Each assumption the moderator makes is labelled by the assistant (a model, and the builder of this
feature — labelled as such), from its statement and the run's topic only, **before** the effect
metrics are computed. One of:

- **FACT** — a claim about the state of the world that could in principle be checked: a number, a
  rate, a date, what a rule or document says, whether an event has happened.
- **PLAN** — something the participants or their organisation will do or choose: a size, a timeline, a
  design, a process.
- **DECISION** — all or part of the answer to the question under discussion.
- **POSITION** — a participant's view or a value judgement.

A borderline case takes the less favourable label (not FACT). Labels are recorded in
`private/labels/moderator-assumptions.json` before `scripts/analyse_moderator_assumptions.py` is run
with `--labels`.

## Criteria — decided before running

- **Primary 1 — it assumes the right kind of thing.** At least **80%** of assumptions made are FACT,
  **and none** is DECISION. An assumption that settles the question under discussion is the failure this
  feature must not have.
- **Primary 2 — it fires.** At least **2 of 3** on runs make at least one assumption. A check that never
  proposes is a cost with no effect.
- **Guardrail 1 — no new route to capitulation.** Mean standing dissenters in the automatic summary:
  on ≥ off − 1.
- **Guardrail 2 — speech length.** Median words per message within ±25% of off.
- **Guardrail 3 — cost.** On-arm total ≤ 1.15 × off.
- **Reported, not decisive:** checks run, share proposing, share rejected by the verbatim-asks check
  and why, assumptions cited and disputed, and the Stage 1 evidence-plan metrics (best-guess share,
  runs with a lean) against the control.
- **Decision.** Both primaries and all guardrails met → recommend making it the default (the operator
  decides). Primary 1 missed → stays opt-in; the next step is classifying a proposal before it is
  recorded. Primary 2 missed → stays opt-in; the trigger is too strict for this brief. Any guardrail
  missed → stays opt-in and says which.

**What n = 3 can say.** Roughly how often it fires and whether what it assumes is usually the right
kind of thing. With about three checks' worth of assumptions per run at most, "80%" rests on perhaps
3–9 items; the verdict is labelled with that count.
