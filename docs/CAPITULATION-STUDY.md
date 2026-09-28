# Settled or folded? — pre-registration

**Status:** pre-registered 2026-09-28, before any rate below was computed for these runs.

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

## Result

*(Not yet run.)*
