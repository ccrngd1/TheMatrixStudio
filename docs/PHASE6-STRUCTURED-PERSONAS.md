# Phase 6 — Structured Personas

**Status:** BUILT. Not yet measured against a live model.

This phase acts on a verdict that was already reached and then left on the shelf.
`docs/PHASE5-PREMISE-VALIDATION.md` ran a three-arm experiment and concluded
**"PROCEED with structured personas (Arm B)"**; Phase 5 went to document
retrieval instead. Nothing here is a new hypothesis — it is the implementation
of an existing measured recommendation, plus the one change that recommendation
explicitly demanded before shipping.

## The gap being filled

The engine had `goals`. Goals are **satisfiable**: a persona holding one can be
talked into any plan that satisfies it, which is why the control arm's
accommodation rate was 0.667 — two turns in three ended in some form of "that's
fair, I could live with that."

There was no axis for *what I believe and will not give up*. Convictions are
defended; goals are traded. That is the whole of what this adds.

## What was built

`matrix_studio/personas.py` — pure models and rendering, no LLM, no database:

| Field | What it is for |
|---|---|
| `background.formative_events[].lesson` | A position with a history is harder to abandon than a bare assertion. The **lesson** is the load-bearing half; the event alone is colour. |
| `preferences.optimises_for` | What the persona trades everything else for. |
| `preferences.dismisses` | What it declines to *weigh*. Highest-value field per the experiment (dismissal rate 0.067 → 0.333) and the most dangerous — see the retune below. |
| `preferences.persuaded_by` | What actually moves it. |
| `viewpoints[].position` | The claim, as it would be said out loud. |
| `viewpoints[].firmness` | `negotiable` \| `firm` \| `non-negotiable` \| `requires-escalation`. |
| `viewpoints[].evidence_that_shifts` | The exit condition. Rendered **always alongside** firmness. |
| `viewpoints[].formed_by` | Which experience produced this position. |
| `viewpoints[].underlying_concern` | The real worry. **Withheld** — see below. |
| `viewpoints[].validity` | Operator calibration note. **Never rendered anywhere.** |

Config is `config.personas` (`enabled`, `withhold_concerns`, `dismissal_rule`),
parsed by `PersonaConfig.from_config`. It is deliberately **not** part of
`CognitionConfig` — the same call `RetrievalConfig` makes, and here it is
stronger: the premise validation ran with cognition **off in all three arms**, so
cognition-off is the configuration the evidence actually covers.

Off by default. With `enabled` false, a `structured` block on a cast member is
ignored and prompts are byte-identical to pre-Phase-6 —
`test_off_prompt_is_identical_to_having_no_structured_block` asserts that by
diffing the real prompts, not by inspecting the renderer.

## Two decisions that carry most of the weight

### 1. `underlying_concern` is withheld, and that is enforced by the code path

Per the source `stakeholder-review-panel` spec, drawing out the real concern
behind a stated position is *the skill the panel exercises*. A concern
volunteered on turn 1 cannot be drawn out.

So there are two renderings, and the split is a correctness requirement rather
than a token optimisation:

- `render_private()` → the speaker's **own** system prompt. Carries the concern,
  immediately followed by the instruction not to volunteer it.
- `render_public()` → the **moderator's** persona list. Role and
  `optimises_for` only.

The moderator prompt is the one place every persona's description appears at
once, on every turn. Rendering the private block there would put each persona's
withheld concern one prompt away from the entire cast. Two tests scan *every
prompt in a real run* and assert the concern appears in its owner's prompts and
nowhere else.

`validity` goes further: it reaches no prompt at all, public or private. Telling
a persona its own position is "outdated" would collapse the exercise. It exists
so an operator can calibrate a panel — and `tests/test_examples.py` asserts the
shipped example stays calibrated, i.e. that the firmest positions are *not*
uniformly the soundest. If firmness correlated with correctness, an operator
could win by conceding to whoever pushed hardest.

Both fields are also stripped from the `persona.structured` event and from the
dossier API, because the event log is exported and the dossier is a UI surface.
An operator who could read the withheld concern off a panel in the browser has
been handed the answer the conversation was supposed to produce.

### 2. The dismissal rule is re-tuned, because the naive version was measured failing

The experiment's ship condition was explicit: *"re-tune the dismissal rule before
adding any source block."* Arm C shipped the naive form — judge only against your
own priorities — and the discussion degraded into repetitive parallel monologues:

| | control | Arm C |
|---|---|---|
| Within-speaker similarity | 0.160 | 0.218 (+37%) |
| Cross-speaker similarity | 0.183 | 0.202 (*worse than control*) |
| Talking past each other (0-5) | 2 | **4** |
| Dismissal rate | 0.067 | 0.467 |

At a 0.467 dismissal rate the personas spent nearly half their turns declining to
engage. Each became a broken record reciting its own corpus.

The retune separates two things Arm C conflated:

> That is a limit on your **PRIORITIES**, not on your attention. When someone
> argues from one of those concerns you must still engage with what they actually
> said: answer the factual or technical part of it directly, and put their point
> in its strongest form before you set it aside. Then say once, briefly, that it
> is not yours to weigh — and move on to what is.
>
> Do not repeat a dismissal you have already made. Do not answer a challenge by
> restating your own position. Never let declining to weigh something be your
> whole turn.

The three prohibitions are the three shapes the observed failure took, and each
one is locked by a test. `dismissal_rule: false` reproduces the Arm C
configuration for re-measurement — and takes the `dismisses` list with it, so
that configuration cannot be reached by accident.

## Smaller decisions worth recording

**`requires-escalation` was kept, not narrowed away.** The validated Arm B cast
used a fourth firmness value the obvious three-level schema has no room for. It
is not "very firm": it says the speaker lacks the *authority* to concede, so
being overruled produces "I'll have to take this further", not agreement. That
makes it the one firmness level giving a persona something honest to do other
than agree or repeat itself — precisely the corner Arm C's personas got stuck in.
Narrowing it to `non-negotiable` would have quietly dropped a behaviour the
experiment already exercised.

**A defended position with no exit condition is named as such.** Reading a real
rendered prompt turned up a `requires-escalation` viewpoint with an empty
`evidence_that_shifts` — an unfalsifiable wall. Silence there lets the model
either stonewall or invent a condition it was never given, so the render says:

> You have not named anything that would change your mind on this. If you are
> pressed, say that — do not invent a condition you do not have.

That also surfaces the authoring gap in the transcript, where an operator will
actually see it. Negotiable positions get no such line: they shift on a good
argument, so the warning would be noise.

**An unknown `firmness` is rejected, not downgraded.** A typo'd
`"non_negotiable"` silently becoming `negotiable` would quietly remove the
defence this whole feature exists to add. At the API boundary that surfaces as a
**422** (`PersonaModel.structured` is typed as the real model, not a loose dict);
from the CLI it raises at run start. The block is parsed **even when the feature
is off**, so a typo surfaces immediately instead of the day someone enables the
flag and wonders why nothing changed.

**Convictions survive a fork.** `reconstruct_at_turn` seeds `structured` from the
stored cast, including the private fields. A branch asking "what if they had held
firm" needs the convictions to exist on the other side of the fork, or it answers
a different question. (This is unrelated to the older, still-open
cognition-state replay gap in `docs/BACKLOG.md` — cast data is stored, so getting
this right was free.)

## Using it

`examples/structured-personas.json` is the validated Arm B cast — five
stakeholders on the retrieval decision — converted to the shipped schema:

```bash
matrix-studio run examples/structured-personas.json
```

```json
{
  "config": { "personas": { "enabled": true } },
  "cast": [{
    "name": "Dana",
    "persona": "Head of distribution and packaging. Pragmatic, protective of...",
    "goals": ["Protect the five-minute time-to-first-run"],
    "structured": {
      "role": "Head of Distribution & Packaging",
      "preferences": {
        "optimises_for": ["time-to-first-run"],
        "dismisses": ["retrieval answer quality", "research novelty"],
        "persuaded_by": ["a working install on a clean machine"]
      },
      "viewpoints": [{
        "position": "No feature may add a stateful external service to the default install",
        "underlying_concern": "I own the failure when a customer never reaches a working run",
        "formed_by": "The 2023 product that stalled at the install step",
        "firmness": "firm",
        "evidence_that_shifts": ["an embedded index that is a file, not a service"],
        "validity": "sound"
      }]
    }
  }]
}
```

`persona` and `structured` are **additive, not alternatives**: prose carries voice
and manner, which structure is bad at; structure carries commitments, which prose
is bad at.

## What is NOT claimed

The premise validation's own negative result stands and is not weakened by
shipping this:

> The judge found **the same number of distinct positions (5) and identical
> specificity (5/5) in all three arms.** Structure changed *how* the personas
> argued — less harmonising, more owning their lane, more citing — but did **not**
> produce more distinct positions or more specific content.

Both caveats there cut both ways (`distinct_positions = 5` with 5 personas is
plausibly a ceiling artifact; specificity was already 5/5 because the brief was
long and specific), so the null is best read as *unmeasured*, not disproven. It
should not be read as validated either.

Also unclaimed: **none of this has been measured against a live model.** The
tests prove the wiring, the scoping and the non-leakage by reading real prompts;
they cannot prove the retuned dismissal rule actually fixes what Arm C broke.
Phase 5 saw live runs contradict mocked expectations three separate times.

## Next: measuring it

The experiment is already reproducible (`scripts/build_validation_arms.py`,
`scripts/score_validation.py --judge`, ~$0.06/arm), so the honest next step is a
fourth arm — **Arm B as shipped** — scored on the same metrics against the same
brief:

- Does the retuned dismissal rule keep the dismissal rate's *benefit* (0.333 vs
  the control's 0.067) without Arm C's talking-past cost?
- Does `requires-escalation` produce escalation rather than agreement when a
  persona is overruled?
- Does the withheld concern actually get *drawn out* — and only when asked?
- With cognition **on**, which the experiment never tested (it was off in all
  three arms) and which is the interaction most likely to surprise.

The single-sample caveat from the original run applies to any such comparison:
`n = 1` per arm on a non-deterministic model, so repeat at several seeds before
treating a number as settled.
