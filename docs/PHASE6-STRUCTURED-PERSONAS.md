# Phase 6 — Structured Personas

**Status:** BUILT and MEASURED against a live model (Arm D, run 2026-09-06). The
measurement is in §*Measured: Arm D* below and it is **mixed** — one clear win,
one clear regression against Arm B, and two properties that turned out to be
untestable in a 15-turn run.

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

## Measured: Arm D

**Run 2026-09-06.** Cost: **$0.0719** for the 15-turn run, **$0.0207** for the
judge. Model: `bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0`, same as
the original three arms.

Arm D is generated by the same `scripts/build_validation_arms.py` as the others,
so topic, cast names and goals stay byte-identical. It is **the control prose plus
a `structured` block plus `personas.enabled`** — deliberately *not* Arm B's
rendered persona string, because reusing that would measure the hand-written prose
and the engine's rendering at the same time. Arm D therefore differs from Arm A in
exactly the Phase 6 feature, and from Arm B in *how* the same authored content
reaches the model.

That makes Arm D the only arm that legitimately differs in `config`, so
`tests/test_validation_arms.py` enumerates its two permitted extra keys
(`config.personas`, per-member `structured`) by set difference rather than
skipping the arm — a third difference creeping in still fails.

### Deterministic

| Metric | A control | B structured | C grounded | **D shipped** |
|---|---|---|---|---|
| Cross-speaker similarity *(lower = more divergent)* | 0.1829 | **0.1597** | 0.2021 | 0.1895 |
| Within-speaker similarity *(lower = less repetitive)* | 0.1603 | **0.1584** | 0.2177 | 0.1938 |
| Accommodation rate *(lower = less harmonising)* | 0.667 | 0.400 | **0.267** | 0.400 |
| Dismissal rate | 0.067 | 0.333 | 0.467 | 0.133 |
| Citation rate | 0.067 | 0.200 | **0.467** | 0.200 |
| Mean turn length (chars) | 1110 | 962 | 1063 | **1318** |

### Blind judge

Four arms, relabelled `transcript_1..4` and shuffled under seed 7. Arm D drew
`transcript_1`; the judge was told nothing about the hypothesis or which arm was
the control.

| Judge metric | A | B | C | **D** |
|---|---|---|---|---|
| Distinct substantive positions | 5 | 5 | 5 | 5 |
| Positions citing a named source | 1 | 1 | 2 | 1 |
| Participants who changed position | 0 | 1 | 0 | **2** |
| …of those, **driven by new evidence** | 0 | 0 | 0 | **2** |
| Converged to a single view | no | no | no | no |
| Talking past each other *(0-5, lower better)* | 2 | **1** | 4 | 2 |
| Specificity *(0-5, higher better)* | 5 | 5 | 5 | 5 |

### The clear win: evidence-driven position change

**No arm had ever produced one.** Arm B had a single position change with zero
attributable to new evidence; A and C had none at all. Arm D produced **two, both
evidence-driven** — and reading them, they are genuine rather than a judging
artifact:

- **Turn 4, Dana** (`firm`, `evidence_that_shifts: ["an embedded index that is a
  file alongside the SQLite database"]`): *"if the index lives inside the SQLite
  database as a rebuilt artifact, I'll move. But if the answer to 'where does the
  vector store live' is anything other than 'it's a file,' then we're not doing
  this yet."* The exit condition fires on its own terms, and the refusal stays
  attached to it.
- **Turn 11, Marcus** (`negotiable`): *"Priya's right, and I was wrong to defer the
  grounding test until after we measured cost."* A negotiable position shifting on
  argument alone — exactly what the rendered rule permits for that firmness level,
  and it names what persuaded him.

This is the single behaviour `firmness` + `evidence_that_shifts` were added to
produce, and it is the one result here that is unambiguous.

### The retune held: Arm C's failure did not reproduce

Talking-past came back at **2** — equal to the control, versus Arm C's 4 — and
within-speaker similarity at 0.1938 versus C's 0.2177. The parallel-monologue
collapse that made Arm C unshippable did not happen with the same `dismisses`
lists present. Accommodation landed at 0.400, identical to Arm B and well below
the control's 0.667.

### The clear regression: divergence, and a length confound

Arm D is **worse than Arm B on both similarity measures** (cross-speaker 0.1895 vs
0.1597; within-speaker 0.1938 vs 0.1584). Arm B remains the best arm on divergence.

But the comparison is **confounded by turn length**: Arm D's turns are 1318 chars
against Arm B's 962, **37% longer**. These metrics are Jaccard overlap on token
sets, and longer turns mechanically share more vocabulary, so some of the gap is
length rather than convergence. The direction of the confound is known; its size is
not, and it is not separable at n = 1. **This is not resolved.**

Dismissal rate also fell — **0.133 vs Arm B's 0.333** (2 turns vs 5 of 15). Two
readings, not distinguishable from one run:

1. The retune working as designed. It instructs a persona to engage with the
   substance *and* decline only "once, briefly" — fewer, shorter dismissals inside
   longer substantive turns is the predicted shape, and longer turns plus lower
   talking-past is consistent with it.
2. The regex missing dismissals phrased in ways the pattern list does not cover.
   `DISMISSAL` in `scripts/score_validation.py` is an English phrase list and was
   written against the *original* arms' idiom.

Reading the transcript favours (1) — the personas do answer challenges directly
before setting concerns aside — but that is an impression, not a measurement.

### Two properties that could not be tested

**`requires-escalation` never fired, because nothing triggered it.** Priya holds the
only such position, and the room never decided against her — Marcus explicitly
conceded to her at turn 11. A regex sweep for escalation language found zero hits,
which is the *correct* behaviour when you are not overruled. No evidence either
way; it needs a brief engineered so a persona loses.

**The withheld concern was never drawn out, because nobody asked.** Zero
"why do you…" / "what's really behind…" questions across 15 turns. Withholding
itself held perfectly — **zero verbatim leaks** of any persona's
`underlying_concern` — but the reveal path is untested.

That is a finding about the *design*, not just the measurement: nothing in a run
creates pressure to ask a stakeholder why. Left alone, `underlying_concern` may be
inert in practice — carried, never surfaced. Recorded in `docs/BACKLOG.md`.

### Honest verdict

Phase 6 reproduces Arm B's accommodation benefit, avoids Arm C's failure mode, and
is the **first arm to produce evidence-driven position change** — the specific
behaviour it was built for. It does **not** reproduce Arm B's divergence advantage,
and its dismissal rate is a third of Arm B's.

So: ship it on for panels where you want positions defended and genuinely revisable;
Arm B's hand-written prose still wins on raw divergence. And the original run's
caveat applies unchanged — **n = 1 per arm on a non-deterministic model.** The
±differences on similarity here are well inside what one re-run could reverse.

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

That null is unchanged by Arm D, which also scored 5 distinct positions and 5/5
specificity — a fourth arm at the same ceiling is more evidence the metric cannot
express a gain, not evidence there is none.

Also unclaimed: **`requires-escalation` and the concern-reveal path remain
unmeasured** (see §*Measured: Arm D* — neither had a trigger in that run), and the
divergence regression against Arm B is **confounded by turn length** rather than
explained.

## Reproducing

```bash
.venv/bin/python scripts/build_validation_arms.py
.venv/bin/matrix-studio run examples/validation/arm-d-shipped.json \
  -o /tmp/mss_val/arm-d-shipped.json
.venv/bin/python scripts/score_validation.py /tmp/mss_val --judge
```

`score_validation.py` scores Arm D when the file is present and skips it when not,
so the original three-arm comparison stays reproducible on its own.

## Next

What Arm D leaves open, in the order it is worth doing:

1. **Repeat at several seeds.** `n = 1`. The similarity differences against Arm B
   are inside single-run variance, and that comparison is also length-confounded.
2. **Test `requires-escalation` with a brief where a persona loses.** It needs an
   overruling to have anything to do.
3. **Test the concern-reveal path**, which means creating pressure to ask a
   stakeholder *why* — nothing in a run currently does.
4. **Score dismissals by reading, not regex.** The `DISMISSAL` phrase list was
   written against the original arms' idiom and Arm D's drop from 0.333 to 0.133
   may be partly instrument.
5. **Run with cognition ON**, which no arm has ever done — the interaction most
   likely to surprise.
