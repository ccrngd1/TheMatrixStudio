# Why the defaults are what they are

This page explains the project's rule for defaults, which features earned a default of "on" and
on what evidence, which were kept off and why, and where the rule bends. For the full list of
settings and their values, see [`../reference/`](../reference/). For how to change one, see
[`../how-to/`](../how-to/).

## The rule

A feature that changes what reaches a persona's prompt is **off on the server until a
pre-registered measurement says it helps**. The rule is not written down in one place, but the code
states it again and again:

- "every change of that kind in this project is supposed to arrive with a measurement behind it"
  (`RetrievalConfig.authority_floor` in [`../../matrix_studio/state.py`](../../matrix_studio/state.py));
- every config block "defaults to the pre-feature behaviour, because those features change what a
  run *is*" ([`../studies/SPEAKER-SELECTION-EVALUATION.md`](../studies/SPEAKER-SELECTION-EVALUATION.md) §12);
- with a feature off, prompts are "byte-identical" to before (`CognitionConfig`, `PersonaConfig`,
  and the assumptions and consultants blocks); for structured personas a test checks this by diffing
  the real prompts ([`../project/PHASE6-STRUCTURED-PERSONAS.md`](../project/PHASE6-STRUCTURED-PERSONAS.md)).

There are two reasons, and they support each other.

**Old runs must stay what they were.** A run's config is stored when it is created. When you resume
or branch it, the engine reads that stored config and fills any missing key from its own defaults.
If a new feature defaulted on in the engine, every old run would silently gain it the next time it
was resumed or forked, and a fork would differ from its parent in more than the change you made. The
inline-citation change states the aim directly: a run created before it "keeps its prompt when
resumed or branched" ([`../studies/CITE-INLINE.md`](../studies/CITE-INLINE.md), operator decision).

**A default needs evidence, and the evidence has to be decided in advance.** The project learned
this from the dismissal rule. One good run was read as "the retune working as designed"; three runs
later the mean was the control's rate. The fix was procedural: write the criterion down, commit it,
then run. "A criterion chosen once the transcripts are on screen is not a criterion, it is a
description" ([`../project/PHASE6-DISMISSAL-RETUNE.md`](../project/PHASE6-DISMISSAL-RETUNE.md)). Most studies on
this page follow that pattern, including several whose result was "keep it off". The earliest
retrieval measurements predate it.

## Three layers of defaults

Defaults live in three places, and they differ on purpose:

1. **The engine** ([`../../matrix_studio/state.py`](../../matrix_studio/state.py)). This is what a
   stored config with a missing key gets, so it protects old runs.
2. **The API request model** ([`../../matrix_studio/api/app.py`](../../matrix_studio/api/app.py)).
   Its values are written into each new run's stored config. A default can change here, for new runs
   only, without touching old ones. Inline citations were switched on this way, and withheld concerns
   switched off.
3. **The launch form** ([`../../frontend/src/views/NewRunForm.tsx`](../../frontend/src/views/NewRunForm.tsx)).
   Convenience defaults for a person setting up a run interactively, sent explicitly so the run
   records what was chosen.

| setting | engine | API (new runs) | launch form |
|---|---|---|---|
| speaker fairness | **on** | on | server default |
| structured personas | off | off | on when any persona has convictions |
| withhold concerns, "hidden agendas" (within structured personas) | **on** | **off** (since 2026-10-02) | off, sent explicitly |
| dismissal rule (within structured personas) | **mandatory** | mandatory | server default |
| evidence lean (within structured personas) | **on** | on | on, sent explicitly |
| retrieval | off | off | on when there are documents, collections, research or consultants |
| inline citations | off | **on** | on, sent explicitly |
| cognition | off | off | **on**, with memory, reflection, dynamic goals and relationships |
| avatars | deployment setting (on) | | on; off for an ensemble |
| turn ceiling | deployment setting (20) | | 10 |
| research, closing round, stop when converged, moderator assumptions | off | off | off |

## The four that earned "on"

### 1. Speaker fairness

The moderator is shown each persona's turn count, how long since each last spoke, and the run's
fair share. Measured offline over 146 replays on four transcripts and two models, the Gini of turn
share fell from 0.332 to 0.223 on the small model and from 0.335 to 0.185 on the large one, and the
number of replays in which somebody got **zero turns** fell from 6 of 24 to 0 of 24. A live 40-turn
run then went from turn shares of 16, 11, 8, 3, 1, 1 to 8, 7, 7, 7, 6, 5 at the same cost
([`../studies/SPEAKER-SELECTION-EVALUATION.md`](../studies/SPEAKER-SELECTION-EVALUATION.md) §10–§13).

Three alternatives were measured and not chosen:

- **A bigger selection model.** With the old prompt, a model 2.6 times the price was exactly as
  unfair (0.332 against 0.335). "The skew is a property of the prompt, not of the selector's
  capability." The small model with the counts beat the large model without them on all four
  transcripts at 39% of the cost ([`../studies/SELECTION-MODEL-DEFAULT.md`](../studies/SELECTION-MODEL-DEFAULT.md) §6).
- **A deterministic floor that forces an overdue persona to speak.** Best on the small model, middle
  of the table on the large one. The ranking of interventions inverted between models, so choosing
  the per-model winner would choose an arm "that silently becomes the wrong one the next time somebody
  sets `models.speaker_selection`". The counts plus the fair share were the only arm near the top on
  both and the only one that starved nobody on either (§12).
- **"Choose the best next speaker."** Harmful on the larger model: it starved somebody to zero in 6
  of 13 replays, because "a stronger model has firmer opinions about who is *best qualified*" (§11).

Fairness is on in the **engine**, an exception to the rule above, and the reason for the exception
is recorded: it corrects a measured defect rather than changing what a run is, and "an opt-in
default would have meant almost no run got the fix". The off switch stays, because "an intervention
with no off switch cannot be A/B'd again" (§12).

Fairness then exposed a different problem. With turns spread evenly, the live run said everything by
turn 25 and spent fifteen turns on "nothing to add". Unfairness had been rationing the cast. A
sentence telling the moderator to prefer an overdue persona *only if they have something specific to
add* cut those closing-language turns from 13 of 40 to 3 of 40 and is on with fairness (§13, §15).

### 2. Withheld concerns (on in the engine; off for new runs since 2026-10-02)

Within structured personas, the underlying concern was withheld unless asked, and still is for every
run created before 2026-10-02 and for any run with hidden agendas on. New runs state it plainly by
the owner's decision; see [Off by decision](#off-by-decision-not-by-measurement-withheld-concerns)
below. The original default came from the design, not a comparison: withholding was the point of the
field, because "a concern volunteered on turn 1 cannot be drawn out". It was then checked. Zero verbatim leaks in a 15-turn run, and none
through memory in a pre-registered check that read all 100 memories and reflections across three
cognition runs ([`../project/PHASE6-STRUCTURED-PERSONAS.md`](../project/PHASE6-STRUCTURED-PERSONAS.md);
[`../project/PHASE6-COGNITION-INTERACTION.md`](../project/PHASE6-COGNITION-INTERACTION.md)). What has not been
measured is the reveal: in the runs studied, nobody asked. See
[Personas and convictions](personas-and-convictions.md).

### 3. The dismissal rule

The `mandatory` wording passed a two-part criterion committed before the wording existed: dismissal
rate 0.400, 0.400 and 0.200 across three runs (mean 0.333, no run at zero, against a target of 0.30)
and the best engagement score of any arm, talking past 1.00
([`../project/PHASE6-DISMISSAL-RETUNE.md`](../project/PHASE6-DISMISSAL-RETUNE.md)). The wording it replaced had
suppressed dismissals to the control's rate. The failing wordings are kept, verbatim, as named
variants so their negative results stay reproducible.

### 4. Evidence lean

A persona that asks for evidence must say what it expects and which way it leans today. The first
comparison met one primary criterion and missed the other, and the default was not changed. The
second, with the measure fixed in advance on fresh runs, met it: runs ending with a stated lean 3 of 3
against 0 of 3, all guardrails met. The operator then turned it on. Two checks followed. A third
comparison, on a different kind of brief, held: 3 of 3 against 1 of 3. A check for folding found no
signal against the default, while recording the one doubtful case honestly
([`../studies/EVIDENCE-LEAN.md`](../studies/EVIDENCE-LEAN.md), [`-2`](../studies/EVIDENCE-LEAN-2.md),
[`-3`](../studies/EVIDENCE-LEAN-3.md), [`-FOLDING`](../studies/EVIDENCE-LEAN-FOLDING.md)).

Note that 2, 3 and 4 live inside structured personas, which are themselves **off** on the server.
The structured-persona schema and its honesty properties are tested and sound, but "the behavioural
case for turning it on is not established", and distinct positions remain unstable when convictions
are rendered from data ([`../project/PHASE6-STRUCTURED-PERSONAS.md`](../project/PHASE6-STRUCTURED-PERSONAS.md);
README, Phase 6). The launch form turns structured personas on whenever you author convictions,
which is the case where the fields have something to do.

## On by decision, not by measurement: inline citations

Inline citations are on for new runs, set in the API request model. Both pre-registered comparisons
missed their primary criterion. The first missed because the off arm cited more than the cap that had
been set for it; the second because the on arm reached 0.26 of messages against a required gap of
0.40. The operator turned the rule on after the first comparison, "recorded as a decision, not as a
result". The second comparison's pre-registered outcome was to *recommend reverting* to off, which
goes to the operator; at the time of writing the default is still on
([`../studies/CITE-INLINE.md`](../studies/CITE-INLINE.md)).

The way it was switched on is worth noticing. The engine default stayed off, and only the request
model changed, "so a run created before the change … keeps its prompt when resumed or branched".
That is the rule's second reason working as intended.

## Off by decision, not by measurement: withheld concerns

On 2026-10-02 the owner decided that "Personas shouldn't guard their concerns or objections; they
should be laid out plainly when they are known." New runs now state each underlying concern openly,
as part of its position, and withholding is an opt-in "hidden agendas" toggle on the Cast step,
useful for practising a negotiation or an interview.

It was switched the way inline citations were, in the other direction: the request model defaults
`withhold_concerns` to false and writes it into each new run's config, and the engine default stays
true. So a run created before the change, whose config has no value, keeps withholding when resumed
or branched, and "start fresh" from it carries `true` across. The rule's second reason is kept.

Its first reason is not: the plain wording was not measured before it was turned on. Every study in
[`../studies/`](../studies/) and [`../project/`](../project/) `PHASE6-*` ran with concerns withheld,
so their results do not automatically carry over to runs that state them. The study definitions in
`examples/` pin `withhold_concerns: true` so that re-running one reproduces its condition.

The post-run summary reads the concerns in both modes. On a withheld run it is the reveal, labelled
"hidden during the run" ([Personas and convictions](personas-and-convictions.md)).

## Convenience defaults in the launch form

The form adds defaults for interactive use that the engine does not have:

- **Cognition on, with all its sub-features.** "A run without cognition cannot answer 'why did it say
  that?' — the dossier and the why-trace are both empty — and that introspection is the point of the
  tool." The engine stays off so command-line and scripted runs are unchanged, and the 20–40% extra
  token cost is "a deliberate, visible trade".
- **Structured personas and retrieval switched on only when there is something for them to do.**
  Enabling a feature nobody configured "would cost tokens for an empty prompt block". Research,
  bound collections and consultants all force retrieval on, because a corpus nobody queries is
  "complete, and invisible".
- **Avatars on**, matching the deployment default, **except for ensembles**, where faces would be
  drawn once per member.
- **A 10-turn ceiling**, raised to a higher ceiling when "stop when converged" is ticked, because a
  10-turn budget would cut a converging run off long before it converged.

Each of these comes with its reason in a comment beside it in
[`NewRunForm.tsx`](../../frontend/src/views/NewRunForm.tsx).

## Kept off, and why

| feature | why it is off | record |
|---|---|---|
| Stop when converged | The pre-registered rule said one premature stop rejects it, and an offline replay produced one (turn 17 of a 40-turn run whose argument ran to the end). The explanation that replay protocol was at fault was chosen after seeing which protocol favoured the feature, "so it does not get to decide". Live runs since then, all on one definition, stopped cleanly (one at turn 32 of 40, 23% cheaper); the record asks for a run on a different, harder definition before the default changes | [`../studies/SPEAKER-SELECTION-EVALUATION.md`](../studies/SPEAKER-SELECTION-EVALUATION.md) §14–§16 |
| Query-tuning knobs (`term_limit`, `score_ratio`) | Measured harmful: recall@5 on engine-shaped queries fell from 0.509 to 0.339 | [`../project/PHASE5-RETRIEVAL-MEASUREMENT.md`](../project/PHASE5-RETRIEVAL-MEASUREMENT.md) |
| Moderator-made assumptions | Two-thirds of what it assumed were facts (6 of 9), and one asserted part of the answer under discussion. Three attempts at a classifier to filter proposals each missed their held-out bar | [`../studies/MODERATOR-ASSUMPTIONS.md`](../studies/MODERATOR-ASSUMPTIONS.md) |
| Pre-conversation research | Three comparisons: it reached the room and did not settle the question it was built for; recorded as not useful for that brief and harmless. It also searches the open web and costs minutes and money on every run, so the form leaves it as a per-run choice | [`../PERSONA-RESEARCH.md`](../PERSONA-RESEARCH.md) §9 |
| Standing query, authority floor | Both change what reaches a prompt and were only measured inside the research comparisons; the form sets the authority floor to 1 when research is on | `RetrievalConfig` in [`state.py`](../../matrix_studio/state.py) |
| Capitulation flag on ensembles | Not built: the instrument it would rest on failed its validation step (recall 0.41) | [`../studies/CAPITULATION-STUDY.md`](../studies/CAPITULATION-STUDY.md) |
| A larger speaker-selection model | Measured: no fairness gain at 2.6 times the price | [`../studies/SELECTION-MODEL-DEFAULT.md`](../studies/SELECTION-MODEL-DEFAULT.md) |
| "No source in front of me" disclosure | Built and wording chosen by reading output, at n = 2–3 turns per arm; the record calls it directional and lists it as off without a further reason | [`../project/PHASE5-RETRIEVAL-MEASUREMENT.md`](../project/PHASE5-RETRIEVAL-MEASUREMENT.md) 5g |
| Adaptive pressure | Experimental; its real-model behaviour was never benchmarked | [`../../PHASE4-REPORT.md`](../../PHASE4-REPORT.md) §4 |
| Closing round, pending threads | Off under the general rule (threads are off so cognition runs keep their earlier output schema); no measurement deciding either default is recorded | `SelectionConfig`, `CognitionConfig` |

Some defaults were set by measurement without being prompt features: vector retrieval over lexical,
the 0.15 off-topic guard, 1,024-dimension embeddings, and a small, temperature-honouring model for
speaker selection, validation and stance (see [Evidence](evidence-and-retrieval.md) and
[`../../matrix_studio/models.py`](../../matrix_studio/models.py)). The validation gate is on by
default as a deployment setting; it checks output rather than adding to the prompt, and its live
behaviour was not benchmarked when it shipped.

## Where the rule bends

Three defaults reach old runs, and it helps to know which:

- **Fairness** is on in the engine, so a run created before 15 September 2026 without a `selection`
  block gets the fairness prompt if resumed or branched. That was deliberate (see "Speaker fairness"
  above).
- **The dismissal rule** was once a true/false setting. `true` now means the `mandatory` wording,
  and so does a missing value, so an old run that used the first-shipped wording renders the new one
  if resumed. The retune document records the `true` mapping as the backward-compatible choice
  ([`../project/PHASE6-DISMISSAL-RETUNE.md`](../project/PHASE6-DISMISSAL-RETUNE.md), "Method").
- **Evidence lean** was turned on in the engine as well as in the request model. A run with
  structured personas created before the setting was added, on 28 September 2026, has no value
  stored for it, so it will get the evidence-lean clause if resumed or branched. The commit that
  turned it on set both defaults on purpose, but inline citations were switched on the other way,
  request model only, specifically to avoid this effect, and no record explains why the two differ.

## Why not simply turn everything on?

Because the measurements say several of these features do nothing measurable, or harm, on the briefs
tested, and some were measured on one brief only. Each one also costs tokens on every turn. And
because a feature on by default stops being something you can measure: "an intervention with no off
switch cannot be A/B'd again after it ships" ([`../../matrix_studio/state.py`](../../matrix_studio/state.py),
`SelectionConfig`).

## Related

- [How a run works](how-a-run-works.md)
- [Personas and convictions](personas-and-convictions.md)
- [Evidence](evidence-and-retrieval.md)
