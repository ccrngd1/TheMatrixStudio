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

## Result — 2026-09-29

On arm `8d9916d9`, `2b3e9978`, `2c000a87` (all complete, 40 turns, launched together after `48659af`);
control as declared. Scored by `scripts/analyse_moderator_assumptions.py` as committed; the nine
assumptions were labelled before the effects pass.

| | on | off (control) |
|---|---|---|
| checks / made / rejected by the asks check | 17 / 9 / 8 | — |
| labels of the 9 made | FACT 6, PLAN 2, **DECISION 1** | — |
| runs making at least one | 3 of 3 | — |
| mean standing dissenters | 2.7 | 2.0 |
| median words per message | 148.5 | 137 |
| total cost | $3.63 | $3.33 |

- **Primary 1: MISSED** — 0.67 FACT (6 of 9), and one DECISION: an assumption that ended "…and this
  launch proceeds without that measurement", which asserts part of the answer.
- **Primary 2: met** — it fired in every run; roughly half its proposals were rejected by the verbatim-asks
  check (8 of 17 checks), and it still made the full cap of three in each run.
- **Guardrails: met.** Dissent up, speech +8%, cost +9%.
- **Decision, as pre-registered:** stays opt-in. The next step named here — classifying a proposal
  before it is recorded — is what primary 1 calls for.

**Also found, not part of the verdict:**

- **Two of the nine were verbatim repeats** of an assumption already in force (A3 = A2, in two runs),
  despite the prompt's "do not repeat". A defect in the check, fixed after this result: a proposal whose
  statement matches one in force is now rejected by the engine.
- **The personas rarely used them:** 7 of 9 were never cited by id. An assumption the room does not
  reason from costs a check and changes nothing; whether that is the wording of the block or the moment
  they arrive (mid-argument, in a six-persona room) is not separable at this n.
- **The analyst's evidence-plan scores are unstable on identical transcripts.** The same three control
  runs scored best-guess 0.50 and a lean in 2 of 3 here, and 0.36 and 1 of 3 when `docs/EVIDENCE-LEAN.md`
  scored them the day before. So the reported evidence-plan comparison (on 0.33 vs off 0.50; lean 0 vs 2)
  is within that noise and supports nothing, and the EVIDENCE-LEAN result should be read with the same
  caveat.

## Addendum — the classifier, checked offline (criterion set 2026-09-29, before it ran)

The next step named above was built: after the verbatim-asks check, a second small call labelled the
proposal FACT / PLAN / DECISION / POSITION (the moderator's model, temperature 0), and only a FACT was
recorded. Its bar against the nine labelled assumptions above was committed in `e4e2d10` before it ran:
**reject all three non-facts (two PLAN, one DECISION) and keep at least five of the six FACTs.** The
prompt was not to be edited after the check; a miss is reported, not tuned away. The labels are the
assistant's (see Labelling), which this check inherits.

**Result of the offline check: MISSED.** Non-facts rejected 3 of 3; facts kept **0 of 6**. The classifier
labelled every proposal PLAN, including ones its own prompt lists as FACT ("what a system can do
today"), apparently because each statement sat inside a discussion of a proposed workflow. Shipped as
written it would record nothing on this brief — the feature made inert rather than made safe. As
committed above, the prompt was not re-tuned against these nine; the classifier was **reverted** and
never deployed. A second attempt needs its own labelled proposals from new runs, or it would be fitted
to the labels it is judged by.

## Addendum 2 — fresh proposals for a second classifier attempt (protocol set 2026-09-29, before collection)

**Collection.** `scripts/shadow_assumption_checks.py` runs the moderator's check, unchanged, on stored
40-turn transcripts at every point it would have fired live (after every 4th completed turn), using each
run's own moderator model. Nothing is recorded to the runs and nobody reacts to a proposal. Proposals
that pass the verbatim-asks and repeat checks join a shadow ledger for that run, as they would live; the
cap of 3 is lifted so the set is larger. Source runs, fixed now: the eight Stage 1 runs
(`private/docs/evidence-plan-stage1.json`) and the six EVIDENCE-LEAN runs — none had assumptions on.

**Labels.** Every proposal that passes those checks is labelled FACT / PLAN / DECISION / POSITION by the
assistant under the Labelling definitions above, before any classifier sees it. The SHA-256 of the
labels file is committed before the classifier is run.

**Split.** A proposal's key is `<run8>:<after_turn>`. `int(sha1(key), 16) % 2 == 0` → **working half**
(the classifier prompt may be revised against it, any number of times); otherwise → **held-out half**
(the chosen prompt is run on it exactly once).

**Bar, on the held-out half:** rejects **every** non-fact, and keeps **at least 80%** of the facts. Met →
the classifier goes in (still under the opt-in flag). Missed → it does not, and the miss is recorded.
