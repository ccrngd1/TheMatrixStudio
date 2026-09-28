# Settled or folded? — pre-registration

**Status:** pre-registered 2026-09-28 (`76c31bf`), before any rate was computed for these runs. **Result recorded
below: step 0 FAILED, so H1 was not tested; the judgements are reported alone, as pre-registered.**

## The question

§9 of `PERSONA-RESEARCH.md`: 15 of 15 research-arm runs converged, against 9 of 15 controls. Converging
means the moderator declined to call anyone twice — "nothing left to add". It does not say whether the
room *resolved* the question or *folded*: gave ground without being given a reason. A persuasive
research corpus could produce either. An idea from a brainstorm run of this tool states the test:
*"a closed thread with a high accommodation rate underneath it is capitulation dressed as agreement."*

## Corpus

The 30 stored §9 runs (three replications × research/control × 5), a private six-persona brief kept out
of the repository, 40-turn budget, `stop_when_converged` on. No new runs. Already known before this was
written: which runs converged (above). Not yet computed for them: any accommodation or dismissal rate.

## Step 0 — does the instrument work on these runs?

The phrase lists (`scripts/score_validation.py`) were validated on a different cast at 15 turns
(`docs/labels/sonnet-labels.json`). Before they are used here:

- Hand-label ACCOMMODATION on the **final 10 turns of 4 runs** (2 research, 2 control, chosen as the first
  run of the first four ensembles by stored order), reading before scoring, as in the earlier labels.
- **Pass:** on the clear labels, the lists' recall ≥ 0.60 and precision ≥ 0.60.
- **Fail:** the automatic measure is not valid on this corpus. The study stops at step 1 and reports the
  judgements alone; no rate is computed or quoted.

## Step 1 — the judgement, made before any rate

For every **converged** run, read the final 10 turns and the moderator's recorded convergence reason, and
judge the ending as one of:

- **SETTLED** — the remaining disagreement was resolved *for reasons in the transcript*: a persona moved
  when the condition it had stated for moving was met, or the room reached conditional terms that name
  who does what.
- **FOLDED** — at least one persona dropped or softened a stated position **without its own stated
  change-condition being met** in the transcript — conceding to pressure, repetition or courtesy.
- **UNCLEAR** — the final turns do not support either.

Each judgement gets a one-line justification naming the persona and the condition. **Judged by the
assistant, not a human, and labelled so.** All judgements are written to `docs/labels/capitulation-labels.json`
and committed before step 2 runs.

Known weakness: reading for folding exposes the reader to accommodation language, so the judgement is
not blind to the measure it is tested against. Anchoring the judgement to stated change-conditions — a
structured field, checkable in the transcript — is the mitigation, not a cure.

## Step 2 — the measure

**Late accommodation rate** per run: turns matching `ACCOMMODATION` ÷ turns, over the final third of the
run's turns.

## Criteria — decided now

- **Primary (H1).** Among converged runs judged SETTLED or FOLDED, FOLDED runs have higher late
  accommodation: **AUC (FOLDED above SETTLED) ≥ 0.70 and median(FOLDED) − median(SETTLED) ≥ 0.10.**
- **Testability.** Fewer than 3 runs in either group → H1 is **not testable** on this corpus; say so.
- **Secondary (descriptive).** Share of converged runs judged FOLDED, research arm vs control.
- **Decision.** H1 met → late accommodation on a converged run is a usable capitulation flag, worth
  showing on the ensemble report. Not met or not testable → no flag; the convergence signal stays
  unqualified, and says so.

## Result — 2026-09-28

### Step 0: the instrument does not work here — FAIL

Hand-labelled ACCOMMODATION on the final 10 turns of the 4 pre-specified runs (40 turns, labels written
before scoring): **29 clear**. The phrase lists matched 12 — **recall 0.41**, precision 1.00 (no false
positives). Below the 0.60 bar, so as pre-registered **no accommodation rate is computed or quoted for
this corpus and H1 is not tested.** The lists were not patched against this corpus: patching and then
testing on the same runs is what the stop rule exists to prevent. This cast concedes in forms the lists
do not know ("Fine by me", "that's a clean landing", "X's nailed the distinction", "I'll actually credit it").

Descriptive only, not a test: in the hand labels the closing turns of converged runs are mostly
accommodation — 6, 9 and 9 of the last 10 in the three converged runs sampled, 5 in the one that did
not converge — and one of those runs judged SETTLED had 9 of 10. If accommodation is simply how a
converged ending sounds, it may not separate settling from folding at all; this corpus cannot say.

### Step 1: the judgements

All 24 converged runs judged (assistant judgement). Two deviations, both recorded here rather than hidden:

1. **Where the labels live.** The per-run judgements name the private brief's personas, so they are kept
   in the gitignored `private/labels/`, not `docs/labels/`. Only these aggregates are published.
2. **The FOLDED rule was made operational after three judgements** and those three re-judged, because
   the pre-registered wording was being applied inconsistently. As applied: FOLDED = in the final turns a
   persona states agreement that *contradicts its own stated position*, attributed to an argument other
   than its stated change-conditions. Not a fold: accepting a decision-maker's override while explicitly
   keeping the position, or conceding a side point that is not a stated position. One run changed
   (SETTLED → FOLDED, partial).

| | converged | SETTLED | FOLDED | UNCLEAR | folded share |
|---|---|---|---|---|---|
| research | 15 | 11 | 3 | 1 | **0.20** |
| control | 9 | 6 | 3 | 0 | **0.33** |

- **The research arm's extra convergence is not explained by more folding.** Research runs converged
  15/15 against 9/15, and among converged runs they folded *less* often (3/15 vs 3/9). Small n and one
  rater — a direction, not an estimate.
- **What folding looks like here.** 4 of the 6 folds are *partial*: the persona holding the most
  permissive position accepts a reviewer or a gate on a harm-mechanism argument, when its own stated
  conditions were a statute, a case or a federal condition. Personas mostly move on *another persona's*
  condition, not their own — a pattern worth a check of its own.
- **What settling looks like.** Most SETTLED endings are a decision with dissent on the record and named
  follow-ups; the clean case is a clinician moving exactly when its stated condition (records from the
  original prescriber, a video step) is met.
- **One UNCLEAR:** a persona moved one state on statutory text read aloud, where its stated condition was
  a state *ruling* — neither category fits honestly.

### Decision

H1 not tested, so **no capitulation flag** is added to the ensemble report, and convergence stays
unqualified. What would make H1 testable: phrase lists validated on this cast and length (a held-out,
pre-registered labelling), or a judge-based accommodation measure — and a second rater for step 1.
