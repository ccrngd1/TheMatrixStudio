# A best guess and a current lean with every evidence request — pre-registration

**Status:** pre-registered 2026-09-28, before any run. The criteria below are not to be edited after the
runs start; a result section is appended afterwards.

## Why

Owner-reported: 40-turn conversations end in "I'd want to see evidence before deciding", which leaves
the reader with the question they started with. Stage 1 (`evidence_plan` in the summary,
`scripts/measure_evidence_plan.py`) measured 8 stored runs of 34–47 turns: 43 evidence requests, and
for every one the transcript named the data needed and the result that would move the persona — the
holding rule's "what would change your mind" list makes them say it. What was missing:

| column | stated |
|---|---|
| data, who asked, decision it unlocks, result that moves them | 1.00 |
| **what they expect it to show (best guess)** | **0.58** |
| cheapest way to get it | 0.44 |

and a current lean could be stated in only **2 of 8** runs. A request for evidence gave the room
nothing to act on in the meantime.

## The intervention

`personas.evidence_lean: true` appends one clause to the holding rule
(`personas.EVIDENCE_LEAN_RULE`): when a persona says it needs evidence before deciding, it MUST say in
the same message what it expects the evidence to show and which way that makes it lean today; a guess
is not evidence, and the position still moves only when something on its list turns up. Worded as a
required utterance because the dismissal retune found that a permission goes to zero through the
persona renderer (`docs/PHASE6-DISMISSAL-RETUNE.md`). Off by default.

## Design

Two arms, identical except for `personas.evidence_lean`: the operator's six-persona renewal brief
(private definitions, the same one the cite-inline default and Stage 1 runs used), 40 turns, deployment
default models, structured personas and cognition on. **Three runs per arm.** Launched together.

Scored by `scripts/analyse_evidence_lean.py`, written and committed with this document. The primary
metrics are the Stage 1 scorer applied per run, unchanged: the analyst (a model, labelled as such)
extracts the evidence plan with every unsupplied column written as `not stated`.

## Criteria — decided before running

- **Primary 1 — best guesses.** Pooled share of evidence requests whose best guess is stated.
  **Success: on ≥ 0.80 and on − off ≥ 0.20.**
- **Primary 2 — a current lean.** Runs whose conditional recommendation states a lean (does not
  contain "not stated"). **Success: at least 2 of the 3 on runs.**
- **Guardrail 1 — no new route to capitulation.** Mean standing dissenters in each run's automatic
  summary: on ≥ off − 1. Pushing toward a lean is pressure toward resolution, which Phase 6 exists to
  resist; a room that stops objecting has not been improved.
- **Guardrail 2 — it does not take over the speech.** Median words per message within ±25% of off.
- **Guardrail 3 — cost.** On-arm total cost ≤ 1.15 × off.
- **Decision.** Both primaries and all guardrails met → recommend making it the default (the operator
  decides). A primary met and a guardrail missed → keep it opt-in and say which. Both primaries
  missed → keep it off; the wording does not work.

**What n = 3 can say.** Whether the wording moves these rates a lot or not. It cannot estimate them
precisely, and guardrail 1 is a coarse proxy for capitulation — the study's `docs/CAPITULATION-STUDY.md`
showed the deterministic lists recall 0.41 on this brief, so a folded/settled reading of the on arm's
position changes is worth doing by hand if the primaries pass, and is not part of the verdict.

## Result — 2026-09-28

Runs: off `a2b3b412` `5c34a226` `4a086d55` ; on `640bb8f9` `a38103f1` `6191e1df` . All six complete at 40 turns, launched together after this document was
committed (`6c4c51e`). Scored by `scripts/analyse_evidence_lean.py` as committed.

| | off | on |
|---|---|---|
| evidence requests (analyst-extracted) | 11 | 14 |
| **best guess stated** | **0.36** (4/11) | **0.57** (8/14) |
| **runs stating a current lean** | **1 of 3** | **2 of 3** |
| mean standing dissenters | 2.0 | 3.3 |
| median words per message | 137 | 145 |
| total cost | $3.33 | $3.43 |

- **Primary 1: MISSED.** The on arm rose by 0.21 (clearing the +0.20 gap) but stayed under 0.80.
- **Primary 2: met** — 2 of 3.
- **Guardrails 1–3: met.** Standing dissent went *up*, speech length +6%, cost +3%.
- **Decision, as pre-registered:** the default is not changed. The rule did not specify the case of one
  primary met and one missed; read conservatively it is "keep opt-in", which is what is done. That gap
  in the decision rule is recorded rather than filled after the fact.

**Not part of the verdict — read afterwards.** A regex diagnostic written after scoring found no
compliance in either arm; reading the on-arm transcripts showed the regex was wrong, not the rule.
Personas wrote the requested form in their own words — an expected figure, then "so today I'm
against" — and in one run two personas converged on a concrete test with thresholds on both sides
("over X moves me, under Y does not"), which is the owner's bar for a run that ends in something
usable. Of the on arm's 6 requests without a guess, 4 were of one kind: whether a case, statute, regulator
action or harm **exists at all** — where the persona's honest guess is "it won't turn up" and it states
its position instead of the guess. A wording that asked for the guess *even
when the expected answer is "none exists"* is the obvious next variant; it would need its own
pre-registration. The off-arm baseline (0.36) is also below Stage 1's 0.58 on stored runs, so the
Stage 1 figure is not a stable baseline at n = 3.

**Caveat added 2026-09-29.** Re-scoring the same three off-arm runs a day later
(`docs/MODERATOR-ASSUMPTIONS.md`) gave best guess 0.50 and a lean in 2 of 3, against 0.36 and 1 of 3
here. The analyst that extracts the evidence plan is not stable on identical transcripts, so the
differences above (0.57 vs 0.36; 2 of 3 vs 1 of 3) are within its noise. The verdict is unchanged — the
flag stays opt-in — but it should not be read as evidence the wording works. Scoring each run several
times, or a deterministic measure, would be needed before running this comparison again.
