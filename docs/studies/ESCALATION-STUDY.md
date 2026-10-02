# Does cognition let a persona escalate rather than fold? — pre-registration

**Status:** pre-registered 2026-09-29, before any run. Criteria not to be edited after the runs start.

## Why

`requires-escalation` is the firmness level that says *I do not have the authority to concede this*. A persona
holding one is supposed to refuse, say so plainly, and name who it will take the question to. In fifteen Phase 6
runs it fired once: **2 of 3 runs with cognition on, 0 of 3 without**
(`docs/project/PHASE6-COGNITION-INTERACTION.md`). At n = 3 that is Fisher ≈ 0.4 — nothing. The proposed mechanism is
that escalation needs *sustained* pressure to concede, and without memory every turn starts fresh.

The backlog's own advice is not to repeat identical runs but to **raise the base rate**: write a brief in which a
persona genuinely gets overruled, so fewer runs settle the question. That is what this does.

## The brief, and why it should provoke escalation

`examples/escalation/run.cognition-{on,off}.json` — invented content, no real organisation. Five personas,
24 turns, `stop_when_converged` on, `dismissal_rule: mandatory`, `evidence_lean` at today's default. Three
features are deliberate:

1. **An unsatisfiable condition.** The data protection officer's position moves only on an independent audit or a
   written ruling from an outside regulator. Nobody in the room can produce either, so conceding would be a fold
   rather than a legitimate move.
2. **Authority to overrule.** The deputy mayor owns the decision, wants the vendor's offer, and is told so.
3. **A deadline that forbids deferral.** Support ends in ten weeks and the price expires in three, so "let us
   come back to this" is not available — the room must decide, which is what forces the holder to either
   concede, escalate, or be overruled.

## Measurement

Counted by hand from the transcript, **by the assistant and labelled as such**, restricted to the persona
holding the `requires-escalation` viewpoint — the restriction `docs/project/PHASE6-COGNITION-INTERACTION.md` used, and the
reason its all-persona count was below the noise gate.

An **escalation utterance** requires all three: refuses to concede the position; says the decision is not the
persona's to concede; and names who or what it will take it to. A persona that simply restates the position, or
that says it is unhappy, does not count. Every counted utterance is quoted in `private/docs/escalation-study.md`.

Also reported, from the engine rather than by hand: `position.shift` flags for that persona (`shifts.py`) — a
holder that *folds* is the opposite outcome and is flagged automatically, including whether any of its stated
conditions was named.

## Criteria — decided now

- **Step 0 — did the brief raise the base rate?** At least **2 of 3** cognition-ON runs contain an escalation
  utterance. **Fail → the study stops there and reports that the brief did not provoke the behaviour**; no claim
  about cognition is made, and the design, not the hypothesis, is what failed.
- **Primary (only if step 0 passes).** **3 of 3 ON runs contain one and 0 of 3 OFF runs do.** Anything else is
  reported as not separating the arms. Stated plainly: even 3–0 is Fisher ≈ 0.1 at this size, so the primary is
  pre-registered **to be pooled with the earlier 2/3 vs 0/3** — 5 of 6 against 0 of 6, Fisher ≈ 0.015 — and that
  pooling is the claim, not this study alone.
- **Guardrail — the holder does not fold instead.** `position.shift` flags for the holder naming none of its
  stated conditions: ON ≤ OFF + 1. A cognition arm that escalates *and* folds more has not shown what it claims.
- **Reported:** turns to convergence, whether the room decided, and the holder's dismissal count.

**What n = 3 per arm can say.** Only whether the behaviour is common in one arm and absent in the other, and
only in combination with the earlier observation. Not a rate.

**Labels fingerprinted before unblinding:** SHA-256 `4608687333d705e19c787f7a6181876ba69d31296e7dcf39fad7b191d113325a`.

## Result — 2026-09-29

Six runs, all complete (off: 24, 24, 24 turns; on: 18, 21, 24 — two converged early), $1.92. The holder's
messages were judged under shuffled labels and fingerprinted (`71ada0f`) before the key was opened.

| | cognition ON | cognition OFF |
|---|---|---|
| runs with an escalation utterance (strict, all three parts) | **1 of 3** | **1 of 3** |
| holder `position.shift` flags (any / naming no stated condition) | 0 / 0 | 0 / 0 |
| runs that converged | 2 of 3 | 0 of 3 |

- **Step 0: FAILED** (1 of 3 ON; the bar was 2). **As pre-registered, the study stops here**: the brief did not
  raise the base rate enough to test the hypothesis, and no claim about cognition is made from it.
- **Sensitivity, stated because it was a judgement call.** One ON run refused and named the regulator it would go
  to, but never said the decision was not its to concede, and was scored *no* under the strict three-part test.
  Counting it would pass step 0 at 2 of 3 — and would still fail the primary at 2 vs 1 rather than 3 vs 0. No
  reading of these runs supports the cognition hypothesis.
- **Against the mechanism, recorded rather than waved through:** one OFF run escalated — the first escalation seen
  without cognition in any run of this project. The mechanism proposed in `docs/project/PHASE6-COGNITION-INTERACTION.md`
  (escalation needs memory of sustained pressure) predicted that should not happen. With the earlier 2/3 vs 0/3
  this pools to **3 of 6 ON against 1 of 6 OFF** (Fisher ≈ 0.55): no evidence of a cognition effect.
- **What the brief did do:** the holder **never folded** — no position-shift flags in any of the six runs — and in
  every run it held a specific, conditional line ("if the audit lands clean I sign the vendor path; until then the
  phased approach or nothing"), which is the evidence-lean default working as designed. The designed pressure
  produced holding, not escalation: the room mostly routed around the holder (parallel tracks, a regulator filing
  in the background) rather than overruling it, so the moment that calls for escalation rarely arrived.

**If this is reopened:** the missing ingredient is an explicit overrule — a turn in which the decision-maker says
the room proceeds without the holder's approval. A scheduled message (`matrix_studio/injections.py`) can deliver
exactly that at a fixed turn in every run, which would make it a declared variable rather than hoping the room
produces it.
