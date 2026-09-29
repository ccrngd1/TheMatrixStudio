# Evidence lean, second comparison — scored by lean — pre-registration

**Status:** pre-registered 2026-09-29, before any run. Criteria not to be edited after the runs start;
a result section is appended afterwards.

## Why a second one

`docs/EVIDENCE-LEAN.md` met its lean criterion and missed its best-guess one. Its three-pass
re-analysis then showed the best-guess share is noise at this size (one run scored 0.00 and 1.00 across
passes of the same transcript) while the lean held up (on 8 of 9 passes, off 4 of 9). That re-analysis
was after the fact and on the runs it was suggested by. This comparison tests the lean on fresh runs, with
the measure chosen in advance.

## Design

Two arms identical except `personas.evidence_lean` — the same definitions as the first comparison
(the operator's six-persona renewal brief, 40 turns, deployment default models, structured personas and
cognition on). **Three new runs per arm, launched together.** Each run is scored by
`scripts/analyse_evidence_lean.py --criteria 2 --repeats 3` as committed with this document: three
analyst passes of the unchanged Stage 1 evidence-plan prompt.

## Criteria — decided before running

- **Primary — the run ends with a lean.** A run "states a lean" when a majority of its three passes give
  a conditional recommendation that states one (does not contain "not stated").
  **Success: on ≥ 2 of 3 runs, and on − off ≥ 1.**
- **Guardrail 1** — mean standing dissenters in the automatic summary: on ≥ off − 1.
- **Guardrail 2** — median words per message within ±25% of off.
- **Guardrail 3** — on-arm total cost ≤ 1.15 × off.
- **Reported, not decisive:** lean passes per arm (of 9), and the pooled best-guess share, which is
  known to be noisy.
- **Decision.** Primary and all guardrails met → recommend making `evidence_lean` the default (the
  operator decides). Primary met, a guardrail missed → stays opt-in, and which. Primary missed → stays
  opt-in; with the first comparison, that would be two results pointing different ways.

**What n = 3 can say.** Whether the lean difference seen after the fact recurs on fresh runs. A 3–1 or
3–0 split at n = 3 is weak evidence on its own; its weight comes from agreeing, or not, with the first.
