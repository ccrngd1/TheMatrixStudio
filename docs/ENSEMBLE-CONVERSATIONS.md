# Ensemble conversations: running the same brief many times

Opened 2026-09-21. Stage 1 (the aggregator over runs that already exist) is implemented and
committed as `49ad370`. The design below is what the stage-1 evidence argues for, written
before the fan-out was built so the reasoning is on the record rather than reconstructed
afterwards.

> **Progress, 2026-09-21.** **Stage 2a is complete, and 2b's per-cell counts came with it.**
> `ensemble_spec` plans and proves the cells isolated; the parent row, member linkage and
> `POST /api/ensembles` fan out; `ensemble_reporting` builds and stores the report, triggered
> by the last member to finish and guarded by a durable claim. Per-claim tiers are computed
> **within each cell against that cell's own denominator**, which is 2b's substance. **Stage 3
> (UI) is done too**: a run-type selector in the new-run form, an ensemble view, and the claim
> table. §8 carries the status per step. Nothing has yet been run against live Bedrock.

**The short version.** The default ensemble mode should be **N replicates with nothing
varied**, not N runs with different settings. That is counter-intuitive and it is the whole
point of this document. Varying settings across single runs produces differences you cannot
attribute, corrupts the agreement counts, and adds almost nothing to a spread that
same-config resampling already covers. Replicates are also the control that every other axis
has to be defined against, so they cannot be added later. `method` is the one setting worth
varying, as a second labelled cell. Temperature is not available on the persona voices at
all (`models.py:11`), and would not help if it were. Persona instruction jitter is rejected
outright: it manufactures the disagreement it claims to discover.

---

## 1. What question is the ensemble actually answering?

Two different questions, and they need different designs. Conflating them is the failure
this document exists to prevent.

| question | design | what you get |
|---|---|---|
| **"Can I trust this conclusion?"** | N replicates, everything fixed | which findings are robust vs. a coin flip |
| **"Which conversation shape do I prefer?"** | 2+ cells, ≥2 runs each | a comparison between shapes |

The first is the one a user asking for an ensemble is asking. It needs replicates, because
the only way to know whether a finding is robust is to see how often it recurs under
identical conditions. The second is a harness question — closer to
`SPEAKER-SELECTION-EVALUATION.md` than to a product feature.

---

## 2. The evidence that replicates are not wasted

Stage 1 was run over six existing renewal runs, deliberately chosen as **three pairs with
byte-identical configuration within each pair**:

| pair | runs | config |
|---|---|---|
| 1 | `2d2ac45b`, `3abd39b3` | `moderated`, 24 turns, `{}` |
| 2 | `28235cec`, `c8f0c876` | 40 turns, `{fairness: true, stop_when_converged: true}` |
| 3 | `c055b500`, `22972c2b` | `simultaneous`, 8 turns, closing round |

Cost of the aggregation: **$0.2500** for all six.

Four things held across all six runs, including a drug-classification finding and "a checkbox
alone is not enough" (reached by five different mechanisms), and one demand that was raised
in every run and adopted in none.

The decisive result is the **within-pair divergence**, which the synthesis flagged on its
own:

> **The §4826(b) concession.** Fully conceded in `renewal-2`, never produced at all in
> `renewal`, where the practice-act question simply stays unresolved. *"This does not cleanly
> track a setting — renewal-2 and renewal are the same format and turn-count and land
> differently."*

> **Net-new scope.** Five runs exclude it. `renewal-2` builds the records-pull mechanism once
> for both renewal and net-new. *"No setting correlate — renewal-2 shares its format
> and turn-count with renewal, which excludes net-new."*

Two identical configurations, and one expanded the scope of the work while the other could
not settle the legal question at all. That divergence is the brief's genuine ambiguity, and
nobody induced it. A single run would have handed you either answer with equal confidence.

**This is the finding that justifies the feature.** It is also the finding that says the
default mode is replicates.

---

## 3. Why varying settings does not beat re-running

Four independent reasons, in descending order of how much they matter.

### 3.1 The variance is already inside one config

A 24-turn conversation is a path-dependent branching process. Each turn's sampled tokens
change what the next selection call sees, which changes who speaks, which changes the
context of every remaining turn. Changing a setting injects its influence **once, at the
start**. Sampling injects entropy **once per turn, multiplicatively**. §2 is the measurement:
the same-config noise floor already spans scope and the central legal question.

### 3.2 The settings we expose are traffic control, not belief

`fairness`, `stop_when_converged`, `method`, turn count — every one of these decides *who
talks and when*. None changes a persona's prompt, the brief, or the model. The set of
reachable conclusions is fixed by those three things in every arm. Varying the method mostly
reshuffles which already-reachable conclusion surfaces, which is the same job resampling
does, with an extra uncontrolled variable attached.

`method` is the partial exception and §5 treats it as such.

### 3.3 At n=1 per cell, differences are uninterpretable

If two runs differ in both setting and outcome, you cannot attribute the difference. You need
within-condition variance to judge whether a between-condition gap is signal, and one run per
cell never provides it. The original nine-run renewal sweep is exactly this: nine runs, no
defensible causal claim, and the only interpretable evidence in it came from three
same-config pairs found by accident.

### 3.4 Pooling mixed settings corrupts the counts

"7/9 agreed" means something only if the nine runs are exchangeable. A run that ends before
reaching a question produces a **missing datum that reads as a dissent** — censoring, not
disagreement.

And there is one confirmed systematic effect: the indemnification-in-writing blocker tracks
turn count. The 24-turn arms block on it; the 40-turn arms log it as accepted risk and
escalate. That is a bias term. If shorter runs systematically under-resolve, the correct
response is to **not run them**, not to average over them.

Evidence that a setting shifts outcomes is an argument for choosing that setting once. It is
not an argument for ensembling across it.

---

## 4. Combining cells is fine. Flattening the count is not

Running 5 `moderated` + 4 `hybrid` is fine and encouraged. What breaks is one flat agreement
number over the mixed pool. Keep the cell labels and everything works.

Suppose a finding appears in 5 of 9 runs:

| reporting | reading | action |
|---|---|---|
| flat | "5/9 — split, weak" | discount it |
| stratified: 5/5 moderated, 0/4 hybrid | strong, **method-dependent** | trust it; know blind openings kill it |
| stratified: 3/5 moderated, 2/4 hybrid | coin flip in both cells | brief is ambiguous; ask a human |

Same numerator. Three different actions. A flat count cannot distinguish "depends on the
method" from "random", which is the distinction being paid for. Labels are free.

**Rule: run anything, label everything, report stratified.** A headline "5/9" is acceptable
only with the per-cell breakdown beside it.

---

## 5. The axes, ranked

| axis | verdict | why |
|---|---|---|
| **replicates, all fixed** | **required** | the control the others are defined against; cannot be added later |
| **`method`** | yes, second cell | only exposed knob that changes what a persona *sees* when they speak |
| **brief paraphrase** | yes, later | tests prompt sensitivity, which replicates cannot |
| **leave-one-out composition** | yes, later | tests how load-bearing one participant is |
| **turn count / ceiling** | no | mostly censoring; manufactures fake dissents (§3.4) |
| **`fairness`** | no | already measured: Gini 0.458 → 0.075. Settled |
| **temperature** | not possible | §6 |
| **persona instruction jitter** | **rejected** | §7 |

### 5.1 Why `hybrid` is the right method variant

`hybrid` runs N blind opening rounds and then moderated turns. It gets the mechanism that
genuinely adds coverage — blind rounds, so no participant is anchored on whoever spoke first
— and keeps the mechanism that resolves hard questions. Pure `simultaneous` at 8 turns was
the arm with the least room to reach resolution.

**Only `method` and `hybrid_opening_rounds` may differ between the two cells.** Same
definition, personas, model set, ceiling, and fairness. A cell that also changes turn count
reproduces the confounded design of §3.3.

### 5.2 Brief paraphrase, if built

Tests something replicates cannot: whether a conclusion is a property of the *problem* or an
artifact of the wording. If a neutral reword flips whether the work ships, the finding was
never about the domain — and replicates alone would never reveal it.

Two conditions, both load-bearing:

- **Faithful paraphrase only.** Same facts, constraints, question. Shifting emphasis ("focus
  on compliance risk") asks a different question, and a different answer to a different
  question is not a finding.
- **Its own replicates.** One paraphrase vs. one original is §3.3 again.

Practical hazard: LLM-generated paraphrases drift emphasis almost every time. Variants should
be written deliberately and diffed, not generated at launch. Needs a definition-level field
that does not exist yet.

### 5.3 Leave-one-out composition

Drop one participant; keep everyone else byte-identical. Answers **how load-bearing is this
person?** If a standing dissent vanishes when one persona is absent, the objection lives with
that participant rather than the group. If a conclusion survives dropping any single member,
it is robust in a way replicates cannot show.

No persona's character is perturbed. This is the legitimate form of "vary the personas".

---

## 6. Temperature is not an available axis

Not a judgment call — a blocker, verified in the source:

- `matrix_studio/models.py:11` — **"Sonnet 5 accepts only `temperature=1`. That is measured,
  not assumed."**
- The engine sets **no temperature at all** on persona voice calls: zero matches for
  `temperature` under `matrix_studio/engine/`.
- Only the low-variance roles honour temperature — `speaker_selection` at `0.3`, the
  `validation` gate at `0.0` — and `ROLE_DEFAULTS` deliberately keeps those on
  `LOW_VARIANCE_MODEL` so they stay *consistent*. Jittering them makes the moderator and the
  gate unreliable, degrading every run rather than diversifying it.

Were it exposed, it still would not help. Temperature widens the spread around the same
centre; it does not reach new conclusions. Raising it makes each run less coherent while
making its dissents ambiguous — you lose the ability to distinguish "the brief is genuinely
ambiguous" from "that run was noisy", which is the exact discrimination the ensemble exists
to make. And §2 shows there is no shortage of variance at the stock setting.

---

## 7. Why persona instruction jitter is rejected

The personas are the measuring instrument. Perturb them and the tool stops measuring what
this group of experts concludes.

The drug-classification finding held 6/6, which is why it is credible. Had "be more
skeptical" been injected into that persona's prompt in three of those runs, there would be no
way to tell whether the finding is their professional read or the injected adjective. The
signal is contaminated at its source, and no downstream analysis recovers it.

It is also self-fulfilling in the worst way. Injecting "be more contrarian" *manufactures*
disagreement. The result is variety in which every item is worthless, because it was placed
there. The renewal/renewal-2 divergence in §2 is persuasive precisely because nothing induced
it.

The legitimate version of this idea is §5.3 — change *who is in the room*, not *who each
person is*.

---

## 8. Recommended build order

Replicates first, because every other axis is defined relative to them.

1. **Stage 2a — replicate fan-out.** Parent run row, N child runs with identical config,
   stored report. Default N = 5.
   **DONE.** `matrix_studio/ensemble_spec.py` plans and proves the cells isolated; the parent
   row lives at `USER#{sub}` / `ENSEMBLE#{id}` with the spec, base config and full
   membership; `POST /api/ensembles` fans out; `GET /api/ensembles/{id}` returns per-cell
   counts, `report_ready` and the report. `matrix_studio/ensemble_reporting.py` builds and
   stores it, triggered by the last member to finish.
2. **Stage 2b — stratified report.** Per-cell robustness tiers. Cell labels are structural,
   present from the first version even when there is only one cell.
   **DONE for the counts.** `report["claims"]` is one row per demand or refusal with a
   `per_cell` column, each tiered against *that cell's* denominator — so `unanimous` in one
   cell beside `absent` in another stays visible as a method-dependent finding instead of
   averaging to a weak-looking 2/4. A cell that produced no usable extraction reports
   `null`, not `0 of N`: an empty cell has no opinion, and 0-of-N would let a reader
   conclude it rejected the claim. Not done: the synthesis prose is not yet cell-aware.
3. **Stage 2c — second cell.** `hybrid`, 2 opening rounds, per §5.1.
   *Reachable now* — `ensemble_spec.with_hybrid()` and the `cells` body field both work, so
   this is a data choice rather than a code change. That was the point of building cells in
   step 1 even with only one of them.
4. **Stage 3 — UI.** An ensemble is a **run type**, not a speaker method. It owns N, the
   cells, and the report.
   **DONE.** `NewRunForm` has a `Run` selector (*Once* / *Several times*) with the replicate
   count and an opt-in hybrid comparison group; `EnsembleView` lists the groups, opens each
   member as an ordinary run, and renders the report; `ClaimTable` is the §4 table.
   Three UI decisions that are load-bearing rather than cosmetic:
   - **`ClaimTable` has no total column, deliberately.** A pooled number is the one figure
     that destroys the distinction the table exists for, so it is not renderable, and a test
     asserts its absence.
   - **An empty group renders `—`, never `0 of N`.** Same reason as the API's `null`.
   - **The button names the multiplier** (`Run 5 simulations`). Five conversations is five
     times the spend, and `Run simulation` would not say so at the moment it matters.
   The default run type is `single`, pinned by a test: a default that fanned out would be the
   kind of bug nobody reports because they assume they clicked it.
5. Later, each as its own report section: brief paraphrase (§5.2), leave-one-out (§5.3).

### 8.0 What the planner refuses, and why that is code rather than prose

`ensemble_spec.check_isolated` proves two things against the **materialised** configs, not
against what a cell declared: that every member of a cell is byte-identical, and that
nothing differs between cells except keys some cell declared. `plan` calls it before the
parent row is written, so an unattributable ensemble cannot be created.

That is §3.3 made executable. The override allowlist is two keys (`selection.method`,
`selection.hybrid_opening_rounds`); `max_messages`, `selection.fairness`,
`selection.stop_when_converged`, `models.voice` and anything under `personas` are refused
*with the reason from this document in the error message*. A bare "not permitted" is a rule
someone deletes to make their branch pass.

### 8.0b Who generates the report, and when

The trigger is **the last member to reach a terminal status**, from
`orchestration.finalise` — the same place and for the same reason as the per-run
auto-summary. On the deployed path a run's last act happens inside a Step Functions state,
and Lambda freezes the sandbox when the handler returns, so a background task there is
dropped. (That mistake has now been made three times in this project; see the cognition,
decline-streak and closing-round entries.)

**Terminal, not successful.** A failed member will never finish, so waiting for success
would leave an ensemble with one bad run permanently unreportable.

**Guarded by a durable claim, not an existence check.** Members finish concurrently, so two
of them can both observe "everything is settled". Without `claim_ensemble_report` — a
conditional write, the only mutual exclusion available here — both would pay for a full
extraction pass and a 20k-token synthesis, and the loser's result would overwrite the
winner's.

`POST /api/ensembles/{id}/report` exists for the two cases the automatic path cannot cover:
a retry after a recorded failure, and an ensemble whose members finished before the feature
existed. **It will exceed API Gateway's 29 s limit on the deployed stack** — the extraction
pass is one call per member and the synthesis asks for up to 20k output tokens. That is a
known limitation, not a surprise: expect a 504 and poll `GET`, because the work continues
server-side. The automatic path has a Lambda timeout rather than a gateway one, and is the
route meant to be used.

**Refusals are recorded, not raised.** `report_error` on the parent distinguishes "no report
yet" from "refused because a cell was still running" — only one of those is worth retrying.
A run never fails because commentary on it did.

### 8.0c What the deployed path needed that a single run did not

Three defects found by reading the infrastructure before the first live fan-out, not by
running one. All fixed; each would have produced a wrong or stuck state rather than an error.

**The `finalise` Lambda's timeout was 5 minutes.** Its comment described one LLM call over the
transcript, which was true before the ensemble report was added to it — the report is one
extraction per member plus a synthesis asking up to 20k output tokens, so up to 13 calls for a
full fan-out. At 5 minutes it timed out, the state machine retried, and the retry found the
claim already taken and did nothing. The ensemble sat at "building the report" **for ever, with
no error to act on.** Now 15 minutes. It costs nothing on an ordinary run: only the one
`finalise` that owns a report uses the headroom, and it is the machine's last state, so a slow
one holds no turn open.

**A permanent claim stranded the report on any death that skips the failure handler** — a
timeout, an OOM, a deploy mid-build. The stranding was total, because on the automatic path
there is exactly ONE trigger (the last member to finish) and nothing else ever tries again. The
claim is now a **20-minute lease**, deliberately longer than the Lambda's 15-minute timeout so
a live claimant cannot be overtaken while a dead one frees it five minutes later. The UI's
"Build it now" also **forces**, because an unforced call returns `claimed_by_another` for any
held claim including a dead one, and a button that does nothing is the worse failure.

**The report's cost was unmetered.** Member runs record spend through `execute_slice`, so the
fan-out itself counts against the monthly cap; the report was charged nowhere. A 12-member
ensemble could spend real money the cap never saw, and the cap's job is to refuse the NEXT
thing. Now recorded, and a metering failure does not discard the already-paid-for report.

Checked and needing no change: `dynamodb:LeadingKeys` on `USER#{sub}` already covers
`USER#{sub}` / `ENSEMBLE#{id}`, so tenant isolation extends to ensembles for free; and the
state machine's 1-day execution timeout comfortably contains a 15-minute `finalise`.

**Avatars are off by default for an ensemble.** They are generated per run, so the same cast's
faces would be drawn once per conversation — and image spend is not counted in a run's reported
cost, only voice calls are, so it would be both multiplied and invisible. A default rather than
a hardcode: the toggle still works, and turning it on applies to every member equally so the
comparison is unaffected either way.

### 8.1 Before any fan-out launches

- **Bedrock throughput.** 8 concurrent runs making per-turn calls may exceed account TPM on
  <account-id>, surfacing as throttles, retries, or failed slices rather than a clean result.
  Stagger launches; confirm the limits first.
- **Cost.** Pull real per-run spend from the stored renewal runs rather than estimating. The
  40-turn fairness shape is the expensive one.

### 8.2 Honest limits

- **N = 5 buys coarse tiers only** — unanimous / split / rare. Not significance. That is
  enough for "should I trust this finding", and will not support a claim from a 3/5 vs 2/5
  difference.
- **Claim counts are a FLOOR, not a measurement.** `_normalise` is deliberately crude, so the
  same demand phrased differently counts twice and agreement is under-reported. That is the
  safe direction — over-merging would silently delete a dissent — but it means a `rare` tier
  may be an artefact of wording. The report now **carries this caveat in its own body**
  rather than only here, because whoever reads "1 of 5" in a UI has no link to this document.
  Canonical-label clustering is stage 1.5.
- **A report needs two usable runs.** Below `MIN_USABLE_RUNS` it is refused with a recorded
  reason rather than written. One conversation under an "ensemble report" heading is the most
  misleading artefact this system could emit: it looks like corroborated evidence and is one
  sample.
- **`report_ready` does not mean the report exists.** The last member flips its status and
  then generates, in that order, so there is a window where every member is settled and
  `has_report` is still false. A client polls `has_report` / `report_error`, not
  `report_ready`.
- **Synthesis output budget.** `synthesise` defaults to `max_tokens=20000` with
  `max_pairs=8`. Truncated replies come back **empty**, not partial; `finish_reason` is the
  only discriminator, and the empty case is logged explicitly rather than reported as a
  refusal.

---

## 9. Pre-registered success criterion for stage 2a

Recorded before the fan-out is built, so it cannot be fitted afterwards.

Five replicates of one renewal definition, nothing varied. The feature is worth keeping if:

1. **At least one substantive conclusion splits** — appears in some runs and not others.
   Unanimity across 5 identical runs means the brief is unambiguous and the ensemble is
   ceremony for that definition.
2. **At least one conclusion holds 5/5**, so the robustness tiers discriminate rather than
   labelling everything uncertain.
3. **The report names the split without a setting to blame it on.** This is the §2 result and
   the thing no single run can produce.

If (1) fails on a definition, the honest recommendation is a single run for that definition —
and the report should say so rather than presenting five echoes as agreement.

---

## Related

- `docs/SPEAKER-SELECTION-EVALUATION.md` — fairness interventions, the Gini 0.458 → 0.075
  result, and the arm-comparison methodology this document reuses
- `docs/SELECTION-MODEL-DEFAULT.md` — the temperature / `supports_sampling_params` finding
  in §6 above
- `matrix_studio/ensemble.py` — stage 1: `measure`, `extract_positions`, `per_persona`,
  `agreements_and_dissents`, `synthesise`
- `matrix_studio/ensemble_spec.py` — stage 2a: `Cell`, `plan`, `check_isolated`, the override
  allowlist and `tier`
- `matrix_studio/ensemble_reporting.py` — stage 2a/2b: `build`, `generate`, the claim, and
  `maybe_report_for_member`
- `scripts/ensemble_report.py` — stage-1 CLI (`--match`, `--cache`, `--no-synthesis`)
- `tests/test_ensemble_spec.py`, `tests/test_ensemble_storage.py`,
  `tests/test_ensemble_reporting.py`, `tests/test_api_ensembles.py` — 120 tests, mostly
  asserting refusals
