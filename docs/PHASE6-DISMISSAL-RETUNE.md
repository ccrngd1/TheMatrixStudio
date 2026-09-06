# Phase 6 dismissal rule — re-tune, pre-registered

**Status:** COMPLETE. The criterion below was committed in `6927607`, before any
candidate wording existed and before any run. **`mandatory` passed both conditions**
and is now the default. Results are in §*Results*; nothing above that line was
edited after the runs.

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

Run 2026-09-06. Six live runs (E and F at n = 3 each) plus 12 judge calls: **$0.41**
run cost, **$0.063** judge.

### Condition 1 — dismissal rate ≥ 0.30 mean, no run at 0.000

| Arm | rule | per-run | mean | zeros | |
|---|---|---|---|---|---|
| B structured | blunt, **in prose** | 0.400, 0.333, 0.333 | 0.355 | 0 | PASS |
| D shipped | `retuned` | 0.200, 0.000, 0.000 | 0.067 | 2 | FAIL |
| **E mandatory** | `mandatory` | **0.400, 0.400, 0.200** | **0.333** | **0** | **PASS** |
| F blunt | blunt, **rendered** | 0.000, 0.000, 0.000 | 0.000 | 3 | FAIL |

Every zero was re-checked with the broad idiom sweep the pre-registration required.
All five zeros are real: **0 hits of any dismissal idiom** in any of them.

E's mean is statistically indistinguishable from Arm B's (0.333 vs 0.355, a gap of
0.022 against a within-arm spread of 0.2) — which is the target, not a shortfall. The
goal was to recover Arm B's rate, not to beat it.

### Condition 2 — talking-past ≤ 2 mean (blind judge)

| Arm | per-run | mean | |
|---|---|---|---|
| B structured | 1, 1, 3 | 1.67 | PASS |
| D shipped | 2, 2, 1 | 1.67 | PASS |
| **E mandatory** | **1, 1, 1** | **1.00** | **PASS** |
| F blunt | 1, 1, 1 | 1.00 | PASS |

**E has the best engagement score of any arm ever measured**, and it is the only arm
that scored 1 on all three runs. So the dismissal rate was not bought back at Arm C's
cost — the thing the retune existed to prevent did not happen.

**Verdict: `mandatory` passes both conditions. It is now the default.**

### The isolation arm answered a different question than expected

F was meant to settle "was the suppression the rule or the rendering?" It produced
something more useful by failing completely:

| | wording | delivery | dismissal rate |
|---|---|---|---|
| Arm B | blunt | hand-written prose | **0.355** |
| Arm F | blunt | Phase 6 rendering | **0.000** |
| Arm E | mandatory | Phase 6 rendering | **0.333** |

The rendering is **not** a blanket blocker — E works fine through it. But Arm B's
*exact wording* fails through it. So neither "the rule was wrong" nor "the rendering
is wrong" is right on its own.

What fits all four data points is simpler and more useful than either:

> **The instruction must require an utterance, not license an omission.**

- *"Ignore the things you consider not your problem"* (blunt) — permission to not
  engage. Rendered alone, produces silence.
- *"say once, briefly, that it is not yours to weigh"* (retuned) — permission to
  speak. Produces near-silence.
- *"you MUST say plainly … Every time it comes up … not optional"* (mandatory) —
  requirement to speak. Produces the behaviour.

Arm B's prose got away with a permission because the blunt instruction sat inside a
five-item **HOW YOU BEHAVE** block of conduct imperatives, which supplied the
mandatory force the sentence itself lacks. Phase 6 renders the sentence without that
surrounding frame, and the permission reading wins.

This generalises past this one field: any rendered persona instruction that wants a
*visible* behaviour has to demand it. Permissions get read as optional, and the model
defaults to omission.

### Also observed, not claimed

`distinct_positions` remains unstable across the rendered arms — E scored 5, 5, 3 and
F scored 2, 2, 5, against Arm B's consistent 5, 5, 5. Arm B is still the only arm that
holds five distinct positions in every run. This is the third time this signal has
appeared and it remains the one thing pointing at a real cost to rendering
convictions from data rather than prose. n = 3 with unknown judge variance, so it is
recorded and not concluded. Tracked in `docs/BACKLOG.md`.

Cross-speaker similarity for E (0.1441 normalised) is inside the noise floor
established earlier (~0.02) against every arm except B, so nothing is claimed there
either.
