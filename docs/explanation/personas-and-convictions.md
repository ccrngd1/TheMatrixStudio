# Personas and convictions

This page explains structured personas: why they exist, what each part is for, why the underlying
concern is stated plainly by default and hidden only on request, and what the dismissal rule and the
evidence-lean rule were measured to do. For how to write one, see [`../how-to/`](../how-to/). For the field list, see
[`../reference/`](../reference/).

## The problem: goals are easy to satisfy

A plain persona has a description and some goals. Goals turned out to be the wrong thing to hold a
discussion together. A persona with a goal can be talked into any plan that satisfies it, so the
default ending of a multi-agent discussion was everyone settling politely on the first synthesis
anyone proposed. In the control arm of the experiment that started this work, two turns in three
ended in some form of "that's fair, I could live with that" (accommodation rate 0.667)
([`../project/PHASE6-STRUCTURED-PERSONAS.md`](../project/PHASE6-STRUCTURED-PERSONAS.md)).

The missing axis was *what I believe and will not give up*. The project's short version:
"Convictions are defended; goals are traded."

## Where the design came from

An external specification for a stakeholder review panel proposed modelling personas as
structured data. Its own first requirement was to try to falsify the idea on a real brief before
building it, and the project did: three arms, 15 turns each, cognition off, one run per arm
([`../project/PHASE5-PREMISE-VALIDATION.md`](../project/PHASE5-PREMISE-VALIDATION.md)).

| | prose control | structured | structured + source excerpts always in context |
|---|---|---|---|
| accommodation rate (lower is less harmonising) | 0.667 | 0.400 | 0.267 |
| dismissal rate (`dismisses` firing) | 0.067 | 0.333 | 0.467 |
| talking past each other, blind judge (0–5, lower is better) | 2 | 1 | 4 |
| distinct positions, blind judge | 5 | 5 | 5 |

The verdict was to build the structured version and not the third arm, whose personas became
"broken records reciting their own corpus". The document is equally clear about what was not
shown: all three arms had the same number of distinct positions and the same specificity. Structure
changed *how* the personas argued, but did not produce more distinct positions or more specific
content. With n = 1 per arm, the smaller
differences were "well within what one re-run could reverse", and later repeats showed that most of
them were.

## What a structured persona holds

The prose description stays. Prose carries voice and manner; structure carries commitments
([`../../matrix_studio/personas.py`](../../matrix_studio/personas.py)).

- **Background, with formative events and the lesson each one taught.** "The lesson is the
  load-bearing half; the event alone is colour." A position with a history is harder to drop than a
  bare assertion.
- **Preferences.** What the persona optimises for, what it is persuaded by, and what it
  **dismisses**: the things it declines to weigh. `dismisses` was the field that did the most work
  in the experiment above.
- **Viewpoints.** Each has a position, how the persona came to it, a **firmness**, and
  **what would change its mind**. These last two are always rendered together.
- **An underlying concern** behind each position: stated plainly by default, or kept back with
  hidden agendas on (below).
- **A validity note**, for the operator only (below).

### Firmness and the holding rule

Firmness has four levels. Each one tells the persona what a legitimate move looks like:

| firmness | may move when |
|---|---|
| `negotiable` | a good argument is made; the persona says what persuaded it |
| `firm` | something on its "what would change your mind" list actually turns up in the conversation |
| `non-negotiable` | the same rule as `firm`; the rendered rule does not distinguish the two |
| `requires-escalation` | never by agreement: the persona lacks the authority to concede, so it says it will take the question further, and to whom |

The holding rule rendered into the prompt also says: "Never claim to have changed your mind while
restating the same position." That clause targets the cheap way a model can satisfy both "be
agreeable" and "hold your position" at once.

Two smaller decisions are worth knowing because they show the project's habits:

- **A defended position with no exit condition is named as such.** The persona is told: "You have
  not named anything that would change your mind on this. If you are pressed, say that — do not
  invent a condition you do not have." Silence would let the model either stonewall or make one up,
  and saying it puts the authoring gap where the operator will see it.
- **An unknown firmness is rejected, not downgraded.** A typo that quietly became `negotiable`
  would remove the defence the field exists to provide. The block is parsed even when the feature is
  off, so a typo fails at once rather than on the day someone turns it on.

`requires-escalation` was kept, rather than folded into `non-negotiable`, because it is "the one
firmness level giving a persona something honest to do other than agree or repeat itself". It is
also hard to provoke. It fired for the first time in two of three cognition-on runs and none of three
without. A later study built to provoke it failed its own first step (one of three runs in each arm),
and pooled with the earlier runs gave three of six against one of six, no evidence of a cognition
effect ([`../project/PHASE6-COGNITION-INTERACTION.md`](../project/PHASE6-COGNITION-INTERACTION.md);
[`../studies/ESCALATION-STUDY.md`](../studies/ESCALATION-STUDY.md)). In that study the holder never folded in any of
six runs; the room mostly worked around it rather than overruling it.

## The underlying concern

Each position can carry the real worry behind it. In our invented brief, Wren the finance lead
holds "No change to the release cadence without a costed rollback plan", and the concern behind it
is that the last outage landed on her budget.

### Stated plainly, by default

Since 2026-10-02 a new run has Wren **say** it. The owner's decision: "Personas shouldn't guard
their concerns or objections; they should be laid out plainly when they are known." So the
concern is rendered under its own position ("What you are really worried about: ..."), and the
holding rule gains one line: when a position bears on the discussion, the persona **must** say the
worry behind it as it makes the position, openly and in its own words. It holds the concern the
way it holds the position: others may argue with it, and it moves on the same terms
([`../../matrix_studio/personas.py`](../../matrix_studio/personas.py), `CONCERN_STATED_RULE`).
It is worded as a requirement rather than a permission, for the reason the dismissal rule below
gives: through this renderer a permission tends to be satisfied by silence.

In this mode the concern is part of what the persona argues, so it is shown: the dossier's
Convictions tab has a CONCERN line under the position, the `persona.structured` event carries it,
and an export lists it with the cast. It still never goes into another persona's prompt or the
moderator's. The room hears it because the persona says it.

**This wording is not measured.** The project normally measures a prompt change before turning it
on, and this one was the owner's call instead. Every study in [`../studies/`](../studies/) and
[`../project/`](../project/) `PHASE6-*` ran with concerns withheld, so their results (dismissal
rates, talking past each other, position changes, the evidence lean) do not automatically carry
over to runs that state them. The study definitions in `examples/validation/` and
`examples/escalation/` pin `withhold_concerns: true`, so re-running one reproduces the condition
its numbers came from.

### Hidden agendas: withheld, as an option

Withholding is now opt-in: tick **Hidden agendas** on the Cast step, or set
`config.personas.withhold_concerns: true`. It is useful for practising a negotiation or an
interview, where finding out what someone actually needs is the skill. Wren then states the
position and does not volunteer the worry. She says it only if someone asks why she holds the
position, or presses past her surface argument.

That was the original design. The source specification said drawing out the real concern behind a
stated position is the skill the panel exercises, and "a concern volunteered on turn 1 cannot be
drawn out" ([`../project/PHASE6-STRUCTURED-PERSONAS.md`](../project/PHASE6-STRUCTURED-PERSONAS.md),
"Two decisions that carry most of the weight"). Every run created before 2026-10-02 withheld, and it
keeps withholding when resumed, branched or started fresh: its stored config either says so or, from
before the setting was written out, says nothing, which the engine reads as withheld.

#### Why a withheld concern is never shown in a run's views

Withholding is enforced by the code path, not by asking politely:

- The concern goes only into its **own persona's** prompt, immediately followed by the instruction
  not to volunteer it. That text is unchanged from Phase 6.
- The **moderator** gets a public one-line summary: role and what the persona optimises for. The
  moderator's prompt is the one place every persona appears at once, so the private block there
  would put every concern one prompt away from the whole cast. (The moderator's prompt is the same
  in both modes.)
- The concern is **stripped** from the `persona.structured` event, from the dossier API and from
  the cast in every export. The dossier renders a concern only when the run says it was stated
  plainly, so even a server that sent a withheld one would show nothing
  ([`../../frontend/src/components/Dossier.tsx`](../../frontend/src/components/Dossier.tsx);
  [`../../matrix_studio/export.py`](../../matrix_studio/export.py)).

Why so strict about the UI in particular? Because, in the design's words, "an operator who could
read the withheld concern off a panel in the browser has been handed the answer the conversation was
supposed to produce". The dossier shows a "Withheld concern — Hidden" note instead. It says
*Hidden* rather than *not drawn out* because nothing detects a reveal
([`../MOBILE-UI.md`](../MOBILE-UI.md) §4.4).

### The analysis reads the concerns in both modes

The post-run summary is given each persona's authored concerns as analyst-only context, whether or
not the run withheld them, and reports on each one: whether it came up in the conversation (yes,
partly or no, with a short quote or turn when it did) and whether anyone addressed it. On a withheld
run that is the reveal, and every surface labels it "Underlying concerns (hidden during the run)":
the summary panel, the decision brief and the exports. A run with no concerns authored gets no such
section, and its summary prompt is exactly what it was before
([`../../matrix_studio/analysis.py`](../../matrix_studio/analysis.py)).

Before the run ends, the concern of a withheld run is visible in one place only: the new-run form,
where you write it, and where a copied setup brings it back for editing. That is authoring, not a
view of the run.

### What was measured, and what was not

All of this was measured with concerns **withheld**:

- **It holds.** A 15-turn run had zero verbatim leaks. The riskier route was memory: with cognition
  on, a persona writes memories about its own reasoning, and those memories go back into its prompt.
  That was pre-registered as a likely leak. All 100 memories and reflections across three runs were
  read; none carried a concern. Memories referred to the persona's *condition*, never the worry
  behind it ([`../project/PHASE6-COGNITION-INTERACTION.md`](../project/PHASE6-COGNITION-INTERACTION.md)).
- **The reveal was never tested.** In the 15-turn run nobody asked any persona why it held its
  position, so there was nothing to reveal. The design document draws the uncomfortable conclusion:
  "nothing in a run creates pressure to ask a stakeholder why. Left alone, `underlying_concern` may
  be inert in practice — carried, never surfaced."

That last finding is part of why the default changed: a withheld concern shaped what a persona
pushed for, and that is observed, but whether a run ever surfaced it depended on someone asking. How
the plain mode behaves is not yet measured.

## The validity note

`validity` is a note for the operator: is this position sound, outdated, misapplied or
overgeneralised? It reaches no prompt at all, public or private. Telling a persona its own position
is outdated would collapse the exercise.

It exists for calibration. If the firmest positions were also the soundest, you could "win" any
panel by conceding to whoever pushed hardest. A test asserts that the shipped example stays
calibrated, with firmness and soundness deliberately not lined up
([`../project/PHASE6-STRUCTURED-PERSONAS.md`](../project/PHASE6-STRUCTURED-PERSONAS.md)). The cast-drafting tool asks
for the same property: at least one firmly held position should be questionable
([`../../matrix_studio/persona_wizard.py`](../../matrix_studio/persona_wizard.py)).

## The dismissal rule

`dismisses` was the most useful field and the hardest to word. Both ways of getting it wrong are
measured ([`../project/PHASE6-DISMISSAL-RETUNE.md`](../project/PHASE6-DISMISSAL-RETUNE.md)):

| wording | delivered as | dismissal rate (3 runs, 15 turns) | talking past |
|---|---|---|---|
| "Ignore the things you consider not your problem" | hand-written prose | 0.400, 0.333, 0.333 | 1.67 |
| the same words | rendered from the structured fields | 0.000, 0.000, 0.000 | 1.00 |
| "say once, briefly, that it is not yours to weigh" plus three prohibitions (first shipped) | rendered | 0.200, 0.000, 0.000 | 1.67 |
| "you MUST say plainly … every time it comes up … not optional" (`mandatory`, the default) | rendered | 0.400, 0.400, 0.200 | 1.00 |

The success criterion was committed before any candidate wording existed: a mean of at least 0.30
with no run at zero, and talking past no worse than 2. Only `mandatory` met both.

Two things make this more than a tuning result:

- **Declining is not disengaging.** The shipped wording requires the persona to say plainly,
  every time, that a concern is not its to weigh, and then, in the same turn, to answer the
  substance anyway and put the other side's point in its strongest form. It keeps one prohibition
  from the first version: "never let declining be your whole turn". The other two prohibitions
  (do not repeat a dismissal, do not answer by restating your position) were dropped as the likeliest
  suppressors ([`../../matrix_studio/personas.py`](../../matrix_studio/personas.py),
  `_rule_mandatory`). Engaging with the substance is what keeps dismissal from turning into the
  monologues of the third arm above. (The README's Phase 6 section still describes the earlier
  wording's prohibitions.)
- **The general lesson: "A rendered instruction must require an utterance, not license an
  omission."** The exact words that worked in hand-written prose produced *nothing* through the
  renderer. In prose they sat inside a block of conduct imperatives that supplied force the sentence
  lacked on its own. Rendered alone, a permission reads as optional and the model defaults to
  silence. The evidence-lean rule below was written to this lesson.

The pre-registration has its own history. The earlier round had read a single good run as "the
retune working as designed"; three runs later the mean was the control's rate. The document exists
because "a criterion chosen once the transcripts are on screen is not a criterion, it is a
description".

## The evidence lean

Long runs kept ending with "I'd want to see evidence before deciding". A first measurement over 8
stored runs found that personas already named the data they needed and the result that would move
them (all 43 requests), because the holding rule makes them. What was missing was a best guess
(stated for 0.58 of requests) and a current lean (statable in 2 of 8 runs). A request for evidence
left the room with nothing to act on meanwhile ([`../studies/EVIDENCE-LEAN.md`](../studies/EVIDENCE-LEAN.md)).

The rule asks for exactly that and nothing more. When a persona says it needs evidence, it must say
in the same message what it expects the evidence to show and which way that makes it lean today.
The rule also says outright that "a guess is not evidence: your position still moves only when
something on your list actually turns up". That clause is there because pushing a room toward a lean
is pressure toward resolution, which is the pressure the holding rule exists to resist.

What it was measured to do, three runs per arm each time:

| study | brief | runs ending with a stated lean, on vs off | notes |
|---|---|---|---|
| [`EVIDENCE-LEAN.md`](../studies/EVIDENCE-LEAN.md) | first brief, 40 turns | 2 of 3 vs 1 of 3 | the best-guess criterion missed; the default was not changed |
| [`EVIDENCE-LEAN-2.md`](../studies/EVIDENCE-LEAN-2.md) | same brief, fresh runs, measure fixed in advance | 3 of 3 vs 0 of 3 (9 of 9 passes vs 1 of 9) | all guardrails met; default turned on |
| [`EVIDENCE-LEAN-3.md`](../studies/EVIDENCE-LEAN-3.md) | a different kind of brief, 36 turns | 3 of 3 vs 1 of 3 | guardrails met; standing dissent rose |

The first study also showed that the best-guess share is too noisy to use at this size: one run
scored 0.00 on one analyst pass and 1.00 on another.

The obvious risk was that personas would talk themselves into agreement on their own guesses. A
dedicated check judged the final ten turns of twelve stored runs, under shuffled labels, for
folding: dropping or softening a firm position without its stated condition being met. The judge
was the assistant that built the feature, labelled as such, and the arms were recognisable in
practice. It found one fold with the rule on and none with it off, within the pre-registered
allowance. The document still records the direction rather than
waving it through. Both doubtful cases were the same persona, the one whose conditions nobody in the
room could produce, and in the fold it gave ground for a reason on neither condition while claiming
it was on its list ([`../studies/EVIDENCE-LEAN-FOLDING.md`](../studies/EVIDENCE-LEAN-FOLDING.md)). That observation
is why position-shift flags exist (see [Reading the results](reading-the-results.md)).

The settled-or-folded study found a related pattern: personas mostly moved on *another* persona's
condition, not their own ([`../studies/CAPITULATION-STUDY.md`](../studies/CAPITULATION-STUDY.md)). One rater, small
numbers, a direction rather than an estimate.

## What is still not established

The honest summary in the README is that "the dismissal rule is measured and fixed; the rest of the
behavioural case is not established". In particular:

- **Distinct positions are unstable when convictions are rendered from data.** Hand-written prose
  scored 5, 5, 5; the rendered arms scored 5, 3, 3 and 5, 5, 3 and 2, 2, 5. The judge was later
  shown to be stable on this count (spread 0 over five judgements on each of four transcripts), so
  the runs themselves differ ([`../studies/JUDGE-VARIANCE.md`](../studies/JUDGE-VARIANCE.md)). It remains the one
  signal pointing at a real cost of rendering convictions from data.
- **Divergence, accommodation, citation rate and turn length differences** between prose and
  rendered personas are below the noise floor at n = 3
  ([`../project/PHASE6-STRUCTURED-PERSONAS.md`](../project/PHASE6-STRUCTURED-PERSONAS.md)).

What is tested and sound is the schema and its honesty properties: withholding with zero leaks,
per-persona scoping, convictions surviving a fork, invalid firmness rejected. All of it was measured
with concerns withheld, the default until 2026-10-02.

This is why structured personas are off by default on the server, while the launch form turns them
on whenever a cast member has convictions. See [Why the defaults are what they are](why-the-defaults.md).

## Where personas come from

You can write personas by hand, start from the archetype library, or ask the drafting tool for a
cast from a one-line brief. The drafting tool is authoring help: it fills the form and you edit and
submit, and "nothing it returns is evidence about anything"
([`../../matrix_studio/persona_wizard.py`](../../matrix_studio/persona_wizard.py)). The archetypes
are labelled "not yet qualified" because none has been put through an ensemble
([`../../matrix_studio/persona_packs.py`](../../matrix_studio/persona_packs.py)).

## Personas never carry a real person's name

A persona is simulated, and nothing it says is a statement by anybody. Two rules keep that visible
(the owner's decision of 2026-10-02).

**A real, well-known person's name is replaced.** You can ground a persona in somebody's public
statements; you cannot give it their name. A line attributed to "Jeff Bezos" in a transcript, an
export or a brief reads as a quote from Jeff Bezos, and nothing printed beside it undoes that. So a
persona or consultant named after a real public figure is renamed, before the run exists, to a
playful and clearly fictional sound-alike ("Jeff Bezos" becomes "Geoff Beesoh", "Werner Vogels"
becomes "Verner Fogles"), and the same replacement is made wherever that person's full name appears
in the cast's descriptions, the topic, the assumptions and the scheduled messages, so the prompt never
says "You are Jeff Bezos" under a fictional name. Documents are left alone: they are evidence, and a
quoted passage has to match its source. Stored runs are never renamed.

Detection is deterministic first: a curated list of 243 well-known people, matched on full names and
common variants regardless of case and accents
([`../../matrix_studio/public_figures.json`](../../matrix_studio/public_figures.json)). A full name
not on the list gets one cheap model check, cached, which also suggests a parody; the suggestion is
checked, not trusted. Both halves are conservative on purpose. A single first name ("Ruth", "Jeff")
never triggers, an ordinary name many people share ("Mike Johnson") is not treated as famous, and only
a confident verdict counts. A false positive renames somebody's invented character and tells them they
typed a famous name; a false negative is a name the list can gain
([`../../matrix_studio/real_names.py`](../../matrix_studio/real_names.py)).

**Every persona name says it is simulated.** Wherever a persona's or consultant's name is shown, a
small robot glyph sits before it, which a screen reader reads as "simulated persona"; exports and
briefs write "(bot) Ruth". The marker is display only: the stored name is unchanged and it never
reaches a prompt, so the personas do not address each other as "(bot) Ruth". An operator's injected
message carries no marker, because it is a real person speaking.

## Related

- [How a run works](how-a-run-works.md)
- [Reading the results](reading-the-results.md)
- The record: [`../project/PHASE5-PREMISE-VALIDATION.md`](../project/PHASE5-PREMISE-VALIDATION.md),
  [`../project/PHASE6-STRUCTURED-PERSONAS.md`](../project/PHASE6-STRUCTURED-PERSONAS.md),
  [`../project/PHASE6-DISMISSAL-RETUNE.md`](../project/PHASE6-DISMISSAL-RETUNE.md),
  [`../project/PHASE6-COGNITION-INTERACTION.md`](../project/PHASE6-COGNITION-INTERACTION.md)
