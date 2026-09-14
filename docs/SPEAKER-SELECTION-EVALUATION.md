# Next-speaker selection: what is wrong, and how to find out what fixes it

Opened 2026-09-14. **Pre-registered: the metrics and decision criteria below are committed
before any arm has been run**, which is this project's convention after the Phase 6 dismissal
work — the previous round's error was choosing what counted as success after seeing output.

## 1. What selection does today

`matrix_studio/engine/simulator.py:_select_speaker`. One LLM call per turn:

- **prompt**: every persona's *public* description, the last **10** messages, the last
  speaker's name, and "Choose naturally based on conversation flow".
- **model**: `speaker_selection` role — Haiku 4.5 by default, `temperature=0.3`, 120 tokens.
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
3. **The fallback has a cast-position bias and is silent.** `candidates[0]` is always the same
   person for a given last speaker, and nothing logs that it fired — so we cannot currently
   tell whether any part of the skew above *is* the fallback rather than the model.
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

## 9. Two defects found by reading the code, worth fixing regardless

Neither is a quality question, and both are now in `docs/BACKLOG.md`:

1. **A programming error in the selection call is indistinguishable from a choice.** While
   building the harness I called `_select_next_speaker(agents, topic, …)` with the first two
   arguments swapped. `list(agents.keys())` failed on a string, the function's broad
   `except Exception` caught it, and it returned a fallback speaker — **no model call, no
   error surfaced, a speaker chosen anyway.** In a run that would appear as the moderator
   making an odd but plausible pick, every turn, for ever.
2. **The fallback is silent and cast-position biased.** `candidates[0]` is deterministic, and
   nothing records that it fired — so the skew measured in §2 cannot currently be attributed
   between the model and the fallback. Intervention F plus a log line fixes both, and should
   land before any further arm is measured, because otherwise a fallback firing looks like a
   decision.
