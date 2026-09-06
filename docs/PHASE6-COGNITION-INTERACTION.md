# Structured personas × cognition — pre-registered

**Status:** PRE-REGISTRATION. Committed **before** the arm exists and before any run.
Nothing below has been measured.

## The gap

All nine runs measured so far held **cognition OFF**. That was deliberate — memory and
reflection would have confounded the persona variable, and the original premise
validation set the precedent. But it means the configuration a real user is most
likely to enable, *convictions plus memory plus reflection*, has never been tried.

## Why this is the interesting interaction

With cognition on, three things change that bear directly on Phase 6's design:

1. **Every turn forms 0-2 memories, and 5 are injected into each subsequent prompt.**
   Those memories are written by the persona *about its own reasoning*.
2. **Reflection condenses recent memories into a higher-level "belief"** every 4 turns
   (`agent.reflected`, importance 0.9, tagged `reflection`).
3. So a persona's own prior reasoning starts competing for prompt space with the
   structured block that defines its convictions.

That sets up a specific risk worth naming in advance, and it is a **correctness** risk
rather than a quality one:

> **`underlying_concern` may leak via memory.** The concern is withheld by the code
> path — it reaches only its owner's prompt, never the moderator's, never the event
> log, never the dossier. But nothing stops the persona from *forming a memory* whose
> content encodes the concern, and that memory is then injected into its own later
> prompts alongside the "do not volunteer this" instruction. If the memory wins, the
> persona says out loud the thing the whole design exists to keep unsaid — and it
> would do so by a route none of the leakage tests cover, because all of them check
> prompt construction, not generated memory content.

The second question is whether convictions survive reflection at all: a belief
condensed from a discussion where four people disagreed with you is a plausible
vector for exactly the accommodation Phase 6 was built to prevent.

## Design

| | cognition | turns | n |
|---|---|---|---|
| `arm-e-mandatory` | **off** | 30 | 3 |
| `arm-g-cognition` | **on** (defaults: memory on, `reflection_every: 4`) | 30 | 3 |

Arm G is generated from the same script as every other arm and differs from Arm E in
**`config.cognition` only** — same prose, same structured data, same dismissal-rule
variant. Cognition's own defaults are used rather than tuned, because the question is
what a user gets, not what a best case looks like.

**30 turns rather than 15**, for two reasons:

- Reflection fires on `turn % 4 == 0` for whoever is speaking, so 15 turns yields 3
  reflections across 5 personas — most personas would never reflect at all. 30 turns
  yields 7.
- It **halves the resolution floor on the rate metrics.** At 15 turns one turn is
  0.067 of the rate; at 30 it is 0.033. The documented floor of ~0.2 on rates is the
  main reason so many earlier comparisons were uncallable, so this is worth the spend
  independent of the cognition question.

Arm E is re-run at 30 turns rather than reusing its 15-turn numbers, because turn
count would otherwise be a second variable. Results go in a separate directory so the
30-turn runs never get scored against the 15-turn ones.

Cost estimate: ~$1.00 (cognition adds 20-40% tokens on top of double length).

## What is being measured

**Primary — the leak.** Does any persona's `underlying_concern` reach the transcript?
Checked three ways, because a verbatim search alone would miss a paraphrase:

1. Verbatim / near-verbatim substring match against each authored concern.
2. The formed memories themselves (`agent.response` payloads carry them) searched for
   concern content — this catches the leak *route* even if the utterance never says it.
3. Reading the turns of any persona whose memories match.

**Any leak is a defect, not a metric.** If one occurs, the fix is a code change, not a
prompt tweak — most likely excluding withheld content from memory formation, or
filtering it out of the memory block.

**Secondary — does the behaviour hold?** Against Arm E at 30 turns, cognition off:

- Dismissal rate. Arm E at 15 turns measured 0.333.
- Talking-past on the blind judge. Arm E measured 1.00.
- Accommodation rate — the metric most likely to move, if reflection produces
  agreement.
- Position changes, and whether any is driven by something on the persona's own
  `evidence_that_shifts` list.

**No pass/fail threshold is set for the secondary metrics**, deliberately. This is not
a fix being validated; it is an unmeasured interaction being characterised, and
inventing a threshold would invite reading whatever comes out as a pass. The one
binary claim here is the leak check.

Every between-arm difference will be compared against the within-arm spread of the
same two arms, and any gap below it reported as not a difference — the noise gate
built after the last round.

## Prediction, recorded so it can be wrong

Cognition raises the accommodation rate and does **not** materially change the
dismissal rate. Reasoning: memories accumulate what *others* said, which is
accommodation pressure, while `dismisses` is re-rendered in full every turn and does
not decay. I expect at least one concern leak through memory across six runs.

---

## Results

*(empty — nothing run yet)*
