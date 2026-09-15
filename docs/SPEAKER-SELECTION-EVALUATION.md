# Next-speaker selection: what is wrong, and how to find out what fixes it

Opened 2026-09-14. **Pre-registered: the metrics and decision criteria below are committed
before any arm has been run**, which is this project's convention after the Phase 6 dismissal
work — the previous round's error was choosing what counted as success after seeing output.

## 1. What selection does today

`matrix_studio/engine/simulator.py:_select_speaker`. One LLM call per turn:

- **prompt**: every persona's *public* description, the last **10** messages, the last
  speaker's name, **each persona's turn count and how long since they last spoke, the run's
  length and the resulting fair share** (interventions A+B, adopted 2026-09-15 — §12), and
  "Choose naturally based on conversation flow, but do not let a participant fall far behind
  their share without reason". Before that adoption the last two items were absent, which is
  the `baseline` arm and the configuration every measurement in §2, §8, §10 and §11 was taken
  against.
- **model**: `speaker_selection` role — Haiku 4.5 by default, `temperature=0.3`, and **no
  output cap** since 2026-09-15. It was 120 tokens (50 with cognition off); that cap was the
  binding constraint on the first model sweep and is the subject of
  `docs/SELECTION-MODEL-DEFAULT.md` §6–§7. Every number in §2 and §8 was measured with it
  still in place.
- **output**: `{"speaker": …, "reason": …}` with cognition on; a bare name with it off.
- **resolution**: `_match` scans the cast in order for a name that appears as a substring.
- **fallback**: the first cast member who is not the last speaker.

## 2. Measured behaviour, three runs, 72 turns

| run | cast | turns | turn shares | Gini | all spoke by | longest A-B-A-B chain |
|---|---|---|---|---|---|---|
| supply-bridge-2 | 8 | 24 | 5,4,4,3,3,2,2,1 | 0.23 | turn 11 | 4 |
| renewal-renewal | 6 | 24 | 8,6,5,2,2,1 | 0.35 | turn 10 | **7** |
| renewal-renewal-2 | 6 | 24 | 7,6,3,3,3,2 | 0.24 | turn 11 | 6 |

Three things stand out, and all three are about *distribution* rather than about any single
choice being wrong:

- **Starvation.** In `renewal-renewal` Dr. Jordan spoke **once in 24 turns** against an expected
  four. In `supply-bridge-2` Dr. Emily Chen spoke once in 24 against an expected three. A
  persona who barely speaks is a persona whose knowledge base, convictions and withheld
  concern were all authored for nothing.
- **Dyad lock.** Up to **7 consecutive turns** of two speakers alternating. The conversation
  becomes a duel with an audience, which is exactly what the multi-participant design exists
  to avoid.
- **Slow coverage.** Every participant has spoken only by turn 10–11 of 24 — so between a
  third and a half of a run passes before the cast is even introduced.

What is NOT broken, so an intervention must not regress it:

- **Self-repetition: 0 of 72 turns.** The same speaker never went twice in a row.
- **Recency preference is mild, not pathological**: 29% of picks (19/66) are one of the last
  two speakers. Some of that is correct — an answer to a direct question should come from the
  person addressed.
- **A reason is recorded on 24/24 turns**, and the reasons are specific. One even cites
  silence: *"He's been silent since Riley pushed back on his enforcement-history argument."*

## 3. Why — four mechanisms, in order of how much they explain

1. **The moderator cannot see who is overdue.** It gets the last 10 messages and nothing else:
   no turn counts, no "who has not spoken", no run length. With 6 speakers, 10 messages is
   under two rounds — a persona silent for 12 turns is *invisible* rather than overdue. The
   reason quoted above shows the model reasoning about silence when it can see it; it cannot
   see past the window.
2. **Nothing rewards coverage.** "Choose naturally based on conversation flow" is a locally
   optimal instruction: the most conversationally natural next speaker is usually someone
   already in the exchange. Naturalness compounds into a dyad lock because each individually
   reasonable choice narrows the field.
3. ~~**The fallback has a cast-position bias and is silent.**~~ **Fixed 2026-09-15** — see §9.
   `candidates[0]` was always the same person for a given last speaker and nothing logged that
   it fired, so no part of the skew above could be attributed between the model and the
   fallback. The fallback is now random, logged, and marked on the `speaker.selected` event as
   `selection_fallback`. **Every arm measured from now on can say how often nobody chose**, and
   the numbers in §2 and §8 were taken before that was true.
4. **`_match` resolves by substring scan in cast order.** Two live hazards: with cognition
   **off**, `selected` is the model's whole reply, so the first cast-order name mentioned wins
   rather than the one chosen; and any name that is a substring of another (`Jordan` inside
   `Dr. Jordan`) resolves to whichever comes first in the cast. Neither is visible in the runs
   above — cognition was on and the names are distinct — but both are silent when they happen.

## 4. Candidate interventions

Cheapest first. Each is independently testable, and the numbers in §2 are the baseline.

- **A. Show the moderator the turn counts.** Add one line per persona: turns taken, and how
  many turns since they last spoke. No new call, ~60 extra tokens. Hypothesis: most of the
  starvation is an information problem, not a judgement problem.
- **B. Name the budget.** Tell it the run length and the fair share ("40 turns, 6 speakers,
  so roughly 7 each"). Pairs with A; the model cannot pace what it cannot count.
- **C. A fairness floor, deterministic.** If any persona is more than *N* rounds overdue,
  force them and record that the floor fired. This is the `merge_with_source_floor` argument
  transplanted: a participant who can never win a slot is a participant who is not in the
  conversation. Cost: none, and it is the only option with a guarantee attached.
- **D. Forbid the last speaker explicitly** and make the fallback random rather than
  `candidates[0]`. Cheap; fixes mechanism 3; low expected effect since self-repetition is
  already 0.
- **E. Let the speaker nominate.** The turn already produces structured output; add an
  optional `next_speaker` and use it when it names someone plausible. Turns selection from a
  third-party guess into an in-fiction act ("Sarah, what does the statute say?"), and removes
  one call per turn. Bigger change, and it can be *gamed* by a persona who always nominates
  the same ally — which is itself worth measuring rather than assuming away.
- **F. Strengthen the resolver.** Exact match first, then longest-name-first substring, and
  log every fallback. Correctness, not quality; do it regardless.
- **G. Ask for the BEST next speaker, not the most natural one.** Added 2026-09-15. Attacks
  mechanism 2 directly: "naturally" is the word that makes each locally reasonable choice
  narrow the field. On its own "pick the best" is only emphasis, so the arm states the
  criterion (whose turn adds the most that is not already in the conversation; who has a
  stake in the point just made; who has been challenged and not answered; whose position is
  untested), names the failure to avoid (continuing the last two speakers' exchange), and
  keeps one exception so it cannot break answering a direct question. Free — a longer prompt
  by ~90 tokens, no extra call.

## 5. How to evaluate — two stages, and the first one is nearly free

### Stage 1: offline replay

**Selection can be scored without generating a conversation.** Replay a recorded transcript;
at each turn *i*, give the selector the state it would have had (last 10 messages, last
speaker, plus whatever the arm adds) and record its pick. Score the sequence of picks.

- ~40 Haiku calls of ~120 output tokens per arm per transcript: **cents, not dollars**, and it
  runs in a couple of minutes.
- Deterministic inputs, so arms are compared on identical state — no confound from the
  conversation itself changing.
- **What it cannot measure**: downstream effects. A different speaker at turn 7 changes every
  later turn, and replay holds the transcript fixed. So Stage 1 ranks candidates on
  distribution properties only; it cannot show that a fairer conversation is a *better* one.

Transcripts available: the three in §2 (72 turns), plus the 40-turn Opus run in flight.

### Stage 2: live runs, only for arms that survive Stage 1

Full runs, measuring the same distribution metrics plus the content metrics that need real
generation: does a forced speaker say something responsive, or a non-sequitur that a
human reader would notice? That question is the whole risk of intervention C and cannot be
answered offline.

## 6. Metrics, and what counts as an improvement

Primary, all computed from `agent.response` order:

| metric | now | target |
|---|---|---|
| Gini of turn share | 0.23–0.35 | **≤ 0.15** |
| minimum turns for any persona (24-turn run, 6 cast) | 1 | **≥ 2** |
| longest A-B-A-B chain | 4–7 | **≤ 4** |
| turn by which every persona has spoken (24 turns, 6 cast) | 10–11 | **≤ 8** |

Guardrails — an arm that trips one of these is rejected however good its distribution:

- **self-repetition stays 0**
- **picks-from-the-last-two stays ≥ 15%.** Driving it to zero would break answering a direct
  question, which is worse than a dyad lock.
- **the reason stays specific**: it must name something from the transcript, not a role
  description. Judged by reading 10 sampled reasons per arm; a generic reason means the model
  is satisfying a quota rather than reading the room.

Decision rule, committed now: **an arm wins if it improves Gini and minimum-turns without
tripping a guardrail, across at least two transcripts.** One transcript is not enough — the
`renewal-renewal` Gini moved 0.35 → 0.24 between two runs of the *same definition*, so
run-to-run variance on this metric is at least 0.1 and a single-transcript win proves nothing.

## 7. What is already known to be worth doing regardless

Intervention **F** and the fallback logging in **D** are correctness fixes, not quality
experiments: they make the mechanism honest about what it did. Doing them first also removes
mechanism 3 as a confound from every arm that follows — otherwise a fallback firing silently
looks like the model making a choice.

**Done 2026-09-15:** the fallback half of **D** (random pick, warning, `selection_fallback` on
the event) plus the error-laundering fix. The resolver half of **F** — exact match before
substring, longest name first — is still open; the harness already does it, the engine does not.

---

## 8. Stage 1 results, first pass (2026-09-14)

Three transcripts, Haiku 4.5, `temperature=0.3`, ~24 selections per arm per transcript.
Baseline verified byte-identical to the shipped prompt by `--check-baseline`.

**Closed loop** — participation counts come from the arm's own picks:

| transcript | arm | gini | min | max | dyad | coverage | self-rep | last-two |
|---|---|---|---|---|---|---|---|---|
| renewal-renewal-2 | baseline | 0.31 | 2 | 8 | 6 | 11 | 3 | 8 |
| | counts | **0.17** | 3 | 7 | 4 | 15 | 1 | 5 |
| | counts+budget | **0.15** | 3 | 6 | 4 | 10 | 3 | 7 |
| renewal-renewal | baseline | 0.38 | 1 | 8 | 7 | 10 | 1 | 12 |
| | counts | **0.22** | 2 | 7 | 4 | 10 | 3 | 8 |
| | counts+budget | 0.26 | 1 | 6 | 6 | 10 | 5 | 13 |
| supply-bridge-2 | baseline | 0.26 | **0** | 5 | 3 | never | 3 | 4 |
| | counts | 0.27 | 1 | 6 | 3 | 14 | 7 | 8 |
| | counts+budget | **0.20** | 1 | 5 | 3 | 14 | 7 | 8 |

**Reading it against the criteria in §6:** `counts` improves Gini on two of three transcripts
and never starves anyone to zero; `counts+budget` gets the best Gini twice but is worse than
`counts` once and raises self-repetition. Neither reaches the ≤ 0.15 target reliably, and
neither is a clean two-transcript win under the committed decision rule. **No arm is adopted
yet.** The honest summary is that showing the counts is directionally right and the budget
sentence is unproven.

### The methodological finding, which matters more than the table

The first pass ran **open loop** — participation counts taken from the recorded transcript —
and `counts+budget` scored 15 consecutive identical picks out of 24 on one transcript. That
number was an artefact of my harness, not a property of the arm: an arm told to correct an
imbalance never learned that it had already corrected it, because the recorded counts do not
register the arm's own nominations. It would have read as a catastrophic result for a
reasonable intervention.

So the loop matters, and neither setting is simply correct:

- **open loop** scores every arm on identical state, which is what makes arms comparable — and
  is the right choice for an arm that only *reads* the state.
- **closed loop** feeds the arm's own picks back into the counts, which is the only honest way
  to measure *pacing* — at the cost of the counts and the transcript disagreeing about who has
  been speaking.

`--closed-loop` selects between them and both are reported. The general lesson is the one this
project keeps relearning: a cheap proxy metric has to be validated against the thing it stands
in for before its numbers are used to decide anything.

## 9. Two defects found by reading the code — fixed 2026-09-15, before any further arm

Neither was a quality question. Both are fixed and both are in `docs/BACKLOG.md`.

1. **A programming error in the selection call was indistinguishable from a choice.** While
   building the harness I called `_select_next_speaker(agents, topic, …)` with the first two
   arguments swapped. `list(agents.keys())` failed on a string, the function's broad
   `except Exception` caught it, and it returned a fallback speaker — **no model call, no
   error surfaced, a speaker chosen anyway.** In a run that would appear as the moderator
   making an odd but plausible pick, every turn, for ever.

   Now: `agents` is type-checked and an empty cast raises, and **only the provider call is
   inside the `try`**. A throttle or an expired credential still degrades — that failure is
   external and the run should continue — but a `TypeError` in our own prompt building,
   parsing or matching stops the run instead of quietly becoming a speaker.

2. **The fallback was silent and cast-position biased.** Now it draws at random from the
   candidates other than the last speaker, logs a warning naming the pick and the cause, and
   marks the `speaker.selected` event with `selection_fallback` (`call_failed` or
   `unresolved`) — present only when nobody chose. The turn-trace route returns it and the
   dossier's why-panel says "drawn at random. Nothing chose them." instead of showing a
   "chosen because" next to a name no model produced.

   The harness's own fallback was changed to match, so an arm is still not flattered by a
   better degradation than the one that ships.

**What this buys the evaluation:** every arm from here on reports `unresolved` alongside its
distribution numbers, so a run of identical picks can be read as either a model preference or a
fallback storm. The §2 and §8 numbers were measured before that was possible, and any arm that
replaces them should be re-measured with the marker present.

Still open, and deliberately not part of that change: **`_match` resolves by substring scan in
cast order** (intervention F). Writing one of the new tests surfaced it — a moderator naming
"Nobody At All" resolved to a cast member called **Bo** — so a wrong resolution is silent in
exactly the way the fallback used to be. It is pinned as a strict `xfail` in
`tests/test_speaker_selection_fallback.py`, which will fail the suite the moment F lands and the
marker becomes a lie.

---

## 10. Stage 1, second pass (2026-09-15): every intervention, three repeats, with a control

The first pass (§8) had no same-protocol baseline and one pass per cell. This one has both,
on Haiku 4.5 — the shipping default — with the output cap removed (see
`docs/SELECTION-MODEL-DEFAULT.md` §7, which is why these numbers supersede §8's rather than
extend them). Six cells × 4 transcripts × 3 repeats, closed loop, 73 replays, ~2,300
selection calls, **~$8.8**.

### Pooled over the four transcripts

| cell | n | gini | sd | range | min turns | replays that starved somebody to 0 | dyad | last-two % | floor fired |
|---|---|---|---|---|---|---|---|---|---|
| baseline (control) | 12 | 0.332 | 0.097 | 0.18–0.45 | 1.08 | **2** | 8.5 | 42.3 | — |
| **baseline+floor (C)** | 12 | **0.178** | 0.073 | 0.07–0.29 | **2.50** | 0 | 5.8 | 26.5 | 65 / 336 turns |
| counts (A) | 13 | 0.224 | 0.032 | 0.18–0.28 | 1.92 | 0 | 4.2 | 36.4 | — |
| counts+budget (A+B) | 12 | 0.223 | 0.043 | 0.15–0.27 | 2.17 | 0 | 4.0 | 40.5 | — |
| best (G) | 12 | 0.289 | 0.069 | 0.20–0.40 | 1.25 | 0 | 4.4 | 42.6 | — |
| best+counts (G+A) | 12 | 0.268 | 0.057 | 0.17–0.35 | 1.75 | 0 | 3.0 | 45.2 | — |

### Per transcript, Gini (mean of 3 repeats)

| cell | renewal-renewal | renewal-renewal-2 | renewal-opus40 | supply-bridge-2 |
|---|---|---|---|---|
| baseline | 0.403 | 0.267 | 0.443 | 0.213 |
| baseline+floor | **0.210** | **0.123** | **0.267** | **0.113** |
| counts | 0.227 | 0.198 | 0.220 | 0.260 ✗ |
| counts+budget | 0.253 | 0.150 | 0.253 | 0.233 ✗ |
| best | 0.377 | 0.250 | 0.330 | 0.200 |
| best+counts | 0.307 | 0.183 | 0.263 | 0.317 ✗ |

### Applying the §6 rule

- **Intervention C (the deterministic floor) wins it, and is the only arm to hit a target.**
  It improves Gini and minimum-turns against the control on **4 of 4** transcripts, with
  non-overlapping repeat ranges on three of them, no guardrail tripped
  (picks-from-the-last-two 26.5%, well above the 15% floor), and it is the **only** cell that
  reaches the coverage target — every participant has spoken by turn **6–8**, against 10–36
  for everything else. It also costs nothing: no prompt tokens, no call, no model.
- **A and A+B pass the rule too** (3 of 4 transcripts each) and are indistinguishable from
  each other — 0.224 vs 0.223. The budget sentence, unproven in §8, remains unproven: it
  buys nothing over the counts alone. `counts` has the tightest spread of any prompt arm
  (sd 0.032), which is its own argument: it is the most *predictable* intervention.
- **G — "pick the BEST speaker" — helps, but least.** 0.332 → 0.289 pooled, better than the
  control on 4 of 4 but marginally on three (overlapping ranges), and min-turns barely moves
  (1.08 → 1.25). Wording alone is a weaker lever than information, which is what §3's
  mechanism 1 predicted.
- **G+A is worse than A alone** (0.268 vs 0.224), and worse than the control on supply-bridge
  (0.317 vs 0.213). **Two objectives dilute the one that works**: told both to maximise
  contribution value *and* shown who is overdue, the model optimises the first and the
  fairness signal weakens. Worth keeping as a finding — the instinct to stack good ideas is
  what this measures against.
- **Nothing else reaches Gini ≤ 0.15 pooled**, though C gets to 0.113–0.123 on two
  transcripts and A+B hit exactly 0.150 on one.

### Two things this pass establishes beyond the ranking

1. **The harness reproduces the real skew.** The baseline arm on the 40-turn Opus transcript
   gives one speaker a mean of **16.0 turns of 40** — the same 16 that Dr. Morgan actually
   took in that run. A replay proxy that reproduces the defect it is measuring is worth
   trusting a little more than one that merely correlates.
2. **The reasons stay specific on every arm** (guardrail 3, ten sampled). Qualitatively, G
   does visibly what it says: where the baseline picked the *over-represented* persona three
   times in five samples, G picked **Casey** — who spoke once in 40 turns of the real run —
   and said why her audit would add something. Its Gini gain is small; its reasoning is not
   worse, and arguably it reads better.

### What C's win does and does not mean

19% of turns (65 of 336) were chosen by the floor rather than by the model. Unlike the random
fallback in §9, that is a designed mechanism rather than a defect — but the caution is the
same: **part of that Gini is the guard, not the selector.** Whether a *forced* speaker says
something responsive or a non-sequitur is exactly what replay cannot see (§5), and it is
intervention C's whole risk. So C is the Stage-2 candidate, not an adoption: one live 40-turn
run against `renewal-renewal-opus`, read by a human, with `floor_fired` recorded per turn.

A cheap belt-and-braces option worth testing at the same time: **C on top of A**. The floor
guarantees nobody starves; the counts let the model avoid *needing* the floor, which should
reduce how often it fires and therefore how often a turn is forced.

---

## 11. The same six arms on Sonnet 5 (2026-09-15): the ranking does not transfer

Identical protocol to §10 — six cells × 4 transcripts × 3 repeats, closed loop, no output cap
— with `speaker_selection` on `global.anthropic.claude-sonnet-5`. 73 replays, ~2,050 calls,
**~$21** (2.6× the Haiku pass; see `docs/SELECTION-MODEL-DEFAULT.md` §6 for why the multiplier
is 2.6 and not 2). Note Sonnet discards `temperature=0.3`, so every cell here ran at an
effective temperature of 1 — which is the main reason three repeats is the minimum.

| cell | gini | sd | min turns | replays that starved somebody to 0 | dyad | last-two % |
|---|---|---|---|---|---|---|
| baseline | 0.335 | 0.075 | 0.92 | **4 of 12** | 4.5 | 48.8 |
| baseline+floor (C) | 0.242 | 0.045 | 2.17 | 0 | 3.9 | 32.4 |
| counts (A) | 0.266 | 0.061 | 1.83 | 1 | 4.8 | 31.8 |
| **counts+budget (A+B)** | **0.185** | 0.064 | **2.42** | 0 | 3.8 | 22.3 |
| best (G) | 0.317 | 0.082 | 1.08 | **6 of 13** | 3.5 | 45.0 |
| best+counts (G+A) | 0.263 | 0.049 | 1.75 | 1 | 3.8 | 29.2 |

### Head to head with §10

| cell | Haiku 4.5 | Sonnet 5 |
|---|---|---|
| baseline | 0.332 | 0.335 |
| baseline+floor (C) | **0.178** | 0.242 |
| counts (A) | 0.224 | 0.266 |
| counts+budget (A+B) | 0.223 | **0.185** |
| best (G) | 0.289 | 0.317 |
| best+counts (G+A) | 0.268 | 0.263 |

**Four findings, and the second is the one that changes how this evaluation should be run.**

1. **The baselines are indistinguishable — 0.332 vs 0.335.** With the shipped prompt, a model
   2.6× the price is *exactly as unfair*. This is now measured twice by two different routes
   (`SELECTION-MODEL-DEFAULT.md` §6 and here), and it is the strongest single result in this
   document: the skew is a property of the prompt, not of the selector's capability.
2. **The best intervention is model-dependent, and the ranking inverts.** On Haiku the
   deterministic floor wins (0.178) and the budget sentence adds nothing over the bare counts
   (0.223 vs 0.224). On Sonnet the budget sentence is the winner (0.185) and the floor is
   mid-table (0.242). So "which intervention should we adopt" cannot be answered
   independently of "which model selects" — and an arm validated on the cheap model may be the
   wrong arm for the expensive one. Every future arm needs measuring on the model that will
   actually run it.
3. **The plausible mechanism for that inversion:** B is a *reasoning* instruction ("a fair
   share is roughly N turns each … do not let a participant fall far behind"), and the more
   capable model acts on it. Sonnet+A+B drove picks-from-the-last-two down to 22.3% and
   reached Gini **0.117 with a minimum of 5 turns** on the 40-turn transcript, its best cell
   anywhere. Haiku responded to the bare *information* and largely ignored the instruction.
4. **G is actively harmful on Sonnet.** "Choose the BEST next speaker" scores 0.317 — no
   better than the baseline — and starves somebody to zero in **6 of 13 replays**, worse than
   the baseline's 4. The likely reason is uncomfortable and worth stating: a stronger model
   has firmer opinions about who is *best qualified*, so asked to optimise contribution
   quality it returns to the same authority repeatedly. **Quality-maximising language
   increases inequality on the stronger model.** On Haiku the same wording was mildly helpful
   (0.332 → 0.289), which is exactly the trap in finding 2.

Consistent with §10: stacking G onto A is worse than the best counts-family arm on both
models (0.263 vs 0.185 here; 0.268 vs 0.223 there).

### What this means for adoption

Under the §6 rule, on Sonnet: **A+B wins** (better Gini and minimum-turns than the control on
4 of 4 transcripts, no guardrail tripped, nobody starved), C also passes but is dominated, A
passes weakly (3 of 4, one starvation), **G is rejected** (better on only 2 of 4, and the
worst starvation of any arm), G+A passes but is dominated.

Taking both sweeps together, the arm to carry into Stage 2 is **A+B — counts plus the fair
share** — not because it wins either sweep outright (C wins on Haiku, A+B on Sonnet) but
because it is the only arm that is *near the top on both* (0.223 / 0.185), starves nobody on
either model, and keeps picks-from-the-last-two above the guardrail on both. It also needs no
new mechanism: it is ~90 tokens of prompt. C remains the strongest single-model result and the
only arm that reaches the coverage target, but 19% of its turns are chosen by the guard rather
than the model, which is the thing Stage 2 exists to judge.

**Still nothing adopted.** `ROLE_DEFAULTS` and the engine's selection prompt are unchanged.

### Instrument note

The Sonnet passes drew intermittent `400 Bad Request` from `bedrock-runtime` — 3 occurrences,
one whole pass lost and topped up by hand. Not the prompt: the same prompt ran 20/20 clean on
retry. That is the second sweep in a row where the missing retry (see `docs/BACKLOG.md`) cost
paid work, and on Sonnet a lost pass costs ~$1.70 rather than ~$0.43.

---

## 12. Adopted 2026-09-15: A+B ships, ON by default

`SelectionConfig.fairness`, default **True**. The moderator's prompt now carries the
participation counts and the fair share on every turn of every run.

**Why A+B and not the better single number.** C (the deterministic floor) wins on Haiku
(0.178) and A+B wins on Sonnet (0.185); C is mid-table on Sonnet (0.242) and B is worthless on
Haiku. The ranking of every other intervention **inverts between models** (§11), so choosing
the per-model winner means choosing an arm that silently becomes the wrong one the next time
somebody sets `models.speaker_selection`. A+B is the only arm near the top on both, and the
only one that never starved anybody on either model — 0 of 24 replays against the baseline's 6.
It is also ~90 tokens of prompt with no new mechanism, so it is the cheapest thing to ship and
the cheapest to withdraw.

| | baseline | A+B |
|---|---|---|
| Gini, Haiku 4.5 | 0.332 | **0.223** |
| Gini, Sonnet 5 | 0.335 | **0.185** |
| minimum turns | 1.08 / 0.92 | 2.17 / 2.42 |
| replays starving somebody to zero | 6 of 24 | **0 of 24** |
| cost per 40-turn run (Haiku) | $0.133 | $0.137 |

**Default ON is the unusual part**, and deliberate: every other config block in `state.py`
defaults to the pre-feature behaviour, because those features change what a run *is*. This one
corrects a measured defect, and an opt-in default would have meant almost no run got the fix.
`{"selection": {"fairness": false}}` restores the old prompt — which is also what makes the
comparison re-measurable after shipping, since an intervention with no off switch cannot be
A/B'd again.

**The text is byte-identical to the `counts+budget` arm**, including "last spoke 0 turn(s)
ago" for the persona who just spoke. Two checks enforce that: a unit test that rebuilds the
arm offline, and `--check-baseline`, which now defaults to comparing the engine against
`counts+budget` rather than `baseline` and was run against `64cff65d` (True). Tidying the
phrasing would be a different prompt wearing this document's numbers.

**Wired through every path**, because the deployed stack runs turns via
`orchestration` → `resume_simulation`, not `run_simulation`: a config parsed only in the
fresh-run path is a feature that works on a laptop and is dead in production, which is how
cognition shipped inert in v0.2. Tests assert the orchestrated turn path and both branching
resume calls pass it, and that `RunConfigModel` (which is strict) accepts the block.

**What is still owed.** This is a Stage-1 result applied to production, not a Stage-2 one. The
open question is unchanged and only a live run answers it: does a conversation whose turns are
spread more evenly still *follow itself*? Next step is one 40-turn run on the
`renewal-renewal` definition against `64cff65d` as the control, comparing the distribution
metrics and reading the transcript. C stays a candidate on top of A+B — the counts should mean
the floor rarely needs to fire.

18 tests; six mutants killed (default flipped off, block appended instead of replacing the
closing sentence, off switch ignored, never-spoken rendered as "0 turn(s) ago", budget sentence
dropped, orchestrated path losing the config).
