# Phase 6 dismissal rule — re-tune, pre-registered

**Status:** PRE-REGISTRATION. Written and committed **before** any candidate wording
exists and before any run. Nothing below has been measured yet.

## Why this is pre-registered

The previous round of this work went wrong in a specific way: Arm D run 1 produced a
dismissal rate of 0.200 with well-shaped dismissals, and that was read as *"the
retune working as designed — fewer, better-formed dismissals"*. Three runs later the
mean was 0.067 with two runs at zero, and the reading was simply wrong.

The failure was not the measurement. It was deciding what counted as success **after**
seeing output. A criterion chosen once the transcripts are on screen is not a
criterion, it is a description. So this file exists first, and its git timestamp is
the evidence that it did.

## The problem being fixed

`dismisses` was the premise validation's highest-value field: it raised the dismissal
rate from 0.067 (control, no `dismisses` authored) to 0.333 (Arm B). Phase 6 ships it
behind a re-tuned rule and measures **0.067 across three runs** — the control's rate,
with two of three runs containing no dismissal of any kind.

Diagnosis (`docs/PHASE6-STRUCTURED-PERSONAS.md` §*The one callable finding*): the
rule contains **one** clause instructing the persona to decline and **six** telling it
to engage or constraining how it declines, three of them outright prohibitions aimed
at the dismissal rather than at the evasion. The single permission is also the
paragraph's weakest phrasing — *"say once, briefly"* reads as a concession.

Both failure modes are now measured, which is what makes the target concrete:

| Rule | Result |
|---|---|
| Blunt (*"Ignore the things you consider not your problem"*) — Arm C | Parallel monologues, talking-past **4**/5 |
| Re-tuned (Phase 6 as shipped) — Arm D | Dismissal suppressed to **0.067**, two runs at zero |

The target sits between them, and Arm D run 1 shows it is reachable: rate 0.200 with
dismissals attached to real engagement. The task is making that **typical** rather
than one run in three.

## Success criterion — both conditions, or it has not worked

1. **Dismissal rate ≥ 0.30 mean across 3 repeat runs, with no single run at 0.000.**
   Reference points: Arm B 0.355, control 0.067. The no-zero-run clause is separate
   on purpose — a mean can be reached by one strong run and two empty ones, which is
   the exact shape that misled the last round.
2. **Talking-past ≤ 2 on the blind judge**, mean across the same 3 runs. Arm C bought
   a high dismissal rate at 4/5, so a fix that reintroduces monologues is not a fix.
   Arm A and Arm D both scored 2, so this is "no worse than the control".

Dismissal rate is measured with the hand-verified `DISMISSAL` patterns
(`docs/labels/dismissal-labels.json`), **plus** a broad idiom sweep on any run
scoring 0.000 — because a zero is the claim most likely to be an instrument artifact,
and that check is what confirmed Arm D's zeros were real.

If a candidate meets (1) and fails (2), or vice versa, it is recorded as a failure and
the wording changes. Splitting the difference after the fact is what this document
exists to prevent.

## Method

**`dismissal_rule` becomes a named variant** rather than a boolean, because isolating
the rule from the rendering requires emitting *different wordings*:

| value | renders |
|---|---|
| `"mandatory"` | the new candidate wording (intended new default) |
| `"retuned"` | exactly what Phase 6 shipped — kept so the negative result stays reproducible |
| `"blunt"` | Arm B's wording, with Phase 6's structured rendering |
| `"off"` | no rule and no `dismisses` list (today's `false`) |

Backward compatible: `True → "mandatory"`, `False → "off"`.

**`"blunt"` is the load-bearing variant.** Arm D changed two things at once — prose
became data-rendered output *and* the rule was retuned — so the suppression is
confounded. `"blunt"` holds the rendering fixed and restores Arm B's wording, which
`dismissal_rule: false` structurally cannot express because it drops the `dismisses`
list too.

**Screen, then confirm**, so most of the cost only spends if something works:

1. One run per candidate wording (~$0.07 each). Any candidate producing zero
   dismissals at n=1 is discarded without further spend.
2. n=3 on the survivor, and n=3 on `"blunt"` for the isolation question (~$0.40).

## Two outcomes worth naming in advance

- **`"blunt"` recovers Arm B's rate** → the retune was the whole problem, and the
  wording fix is the answer.
- **`"blunt"` is also suppressed** → the rendering is implicated, not the wording, and
  the fix is somewhere else entirely. That is the more interesting result and the
  worse one, because it would mean rendering convictions from data damages the
  behaviour regardless of what the rule says.

Either way the answer is recorded here. This file will be updated with results below
this line, and the text above it will not be edited.

---

## Results

*(empty — nothing run yet)*
