# What Matrix Studio is for, and what it is not

This page is background. It explains what a run of Matrix Studio is, what it can and cannot
tell you, and why the project treats one run as an anecdote. For steps, see
[`../how-to/`](../how-to/). For the settings themselves, see [`../reference/`](../reference/).

## What a run is

You give Matrix Studio a topic and a cast. Each member of the cast is a persona: a name, a
paragraph of description, some goals, and optionally a set of convictions. A language model
plays every persona in turn. Another model call decides who speaks next. When the run ends you
get a transcript, a structured summary written by an analyst model, and a stance for each
persona.

Everything in that list is model output. The personas are text you wrote, or text a drafting
tool wrote for you to edit. Nothing in the repository checks a persona against a real person.

So a run is one sample of how a model, playing characters you described, argues a brief under
one set of settings. That is useful. It is not the same thing as finding out what a room of real
people would decide.

## What it is good for

Think of it as a rehearsal room. Some things a rehearsal does well:

- **Surfacing objections before the real meeting.** A persona that holds a firm position and
  states what would change its mind tells you which conditions you would have to meet. The
  summary's evidence plan collects those requests in one table
  ([`../studies/EVIDENCE-LEAN.md`](../studies/EVIDENCE-LEAN.md) describes the measurement behind it).
- **Practising the questions you would ask.** A structured persona carries the concern behind
  its position. By default it says it plainly, so the room argues with the real reason. With
  **hidden agendas** on, it keeps the concern back until someone draws it out, which is the
  exercise the feature was first built for: "a concern volunteered on turn 1 cannot be drawn
  out" ([`../project/PHASE6-STRUCTURED-PERSONAS.md`](../project/PHASE6-STRUCTURED-PERSONAS.md)).
  The summary says afterwards which concerns came up and which were answered. See
  [Personas and convictions](personas-and-convictions.md).
- **Asking "what if" in a controlled way.** You can fork a run at any turn with one change, or
  run an ensemble where groups differ in one declared thing, such as a working assumption or a
  scheduled message. See [How a run works](how-a-run-works.md) and
  [Reading the results](reading-the-results.md).
- **Finding out what evidence would settle a question.** Personas asking for evidence must now
  also say what they expect it to show and which way they lean today. On the two briefs where this
  was measured, runs ending with a stated lean went from 0 of 3 and 1 of 3 to 3 of 3
  ([`../studies/EVIDENCE-LEAN-2.md`](../studies/EVIDENCE-LEAN-2.md), [`../studies/EVIDENCE-LEAN-3.md`](../studies/EVIDENCE-LEAN-3.md)).

The project's original specification described something narrower: a showcase that lets people
demonstrate multi-agent simulation to their own stakeholders
([`../project/PROJECT-SPEC.md`](../project/PROJECT-SPEC.md) §2). The decision-support features came later, in
September 2026, and the README lists them under that heading.

## What it is not

**It is not a forecast.** A run does not predict what real stakeholders will do. The personas
were written by you; the model fills in the rest. The ready-made persona archetypes ship with the
label "not yet qualified", because qualifying one would mean running ensembles to show it holds
its positions, moves on its stated conditions and does not dominate the room. Across a dozen
packs that was estimated at well over $100, so it has not been done
([`../../matrix_studio/persona_packs.py`](../../matrix_studio/persona_packs.py)).

**It is not a poll.** "60% support" means three of the five characters you cast ended the run
accepting the outcome. Change the cast and the number changes. The share is of your cast, not of
any population.

**It is not a source of facts.** The summary is an analyst model's reading of the transcript.
Personas can cite documents, but the citation check confirms only that a persona had the passage
in front of it, not that the passage says what the persona claims (see
[Evidence](evidence-and-retrieval.md)). Consultants answer only from their own documents, and say
so when the answer is not there.

**The "why" is not ground truth either.** With cognition on, each persona states a reason for each
turn, in the same model call that writes the turn. That is better than inventing a reason
afterwards, which the project rejected outright. It is still the model's self-report, and the
README warns that it "can be mistaken, confabulate, or rationalize"
([`../../README.md`](../../README.md), "Cognition & Honesty Note";
[`../project/PHASE2C-REQUIREMENTS.md`](../project/PHASE2C-REQUIREMENTS.md)).

## Why one run is an anecdote

A conversation is path-dependent. Each turn's sampled text changes what the next selection call
sees, which changes who speaks, which changes every later turn. A setting injects its influence
once, at the start; sampling injects variation on every turn
([`../ENSEMBLE-CONVERSATIONS.md`](../ENSEMBLE-CONVERSATIONS.md) §3.1).

The project measured this before it built ensembles. Two runs with byte-identical configuration
reached different conclusions: one widened the scope of the work, the other never settled the
central question at all. Nobody induced that difference. A single run would have handed you either
answer with the same confidence (§2 of the same document).

Later measurements kept showing the same thing. In one ensemble of six runs, the report grouped
32 distinct conclusions; 28 of them were reached in only one run
([`../studies/ASSUMPTION-ENSEMBLE.md`](../studies/ASSUMPTION-ENSEMBLE.md)). The Phase 6 work found that differences
smaller than about 0.02 in similarity, or 0.2 in rates, could not be resolved at 15 turns and three
runs per arm ([`../project/PHASE6-STRUCTURED-PERSONAS.md`](../project/PHASE6-STRUCTURED-PERSONAS.md), the
methodological note at the end).

You cannot reduce this variation by lowering the temperature. The engine asks for the configured
temperature (0.7 by default), but the default conversation model accepts only `temperature=1`, and
the request is silently dropped
([`../../matrix_studio/models.py`](../../matrix_studio/models.py)). The ensemble design adds that
temperature only changes the spread around the same centre; it does not reach new conclusions
([`../ENSEMBLE-CONVERSATIONS.md`](../ENSEMBLE-CONVERSATIONS.md) §6). There is no seed either.
Repeat runs are repeats, not reproductions
([`../project/PHASE6-STRUCTURED-PERSONAS.md`](../project/PHASE6-STRUCTURED-PERSONAS.md), "Repeats").

The decision brief builds this in. For a single conversation its confidence line always reads
"not yet measured", and it tells you to run an ensemble if you want one
([`../../matrix_studio/brief.py`](../../matrix_studio/brief.py)).

## The variation is measured, and so is the instrument

Two kinds of noise matter, and the project measured both.

**Run-to-run variation** is what ensembles measure. An ensemble runs the same brief several times,
in labelled groups. Each conclusion is counted per group and never pooled, and the counts are
coarse tiers: unanimous, split, rare, absent. A group needs at least two runs; the default is five
([`../ENSEMBLE-CONVERSATIONS.md`](../ENSEMBLE-CONVERSATIONS.md);
[`../../matrix_studio/ensemble_spec.py`](../../matrix_studio/ensemble_spec.py)).

**Variation in the models that score runs** is the other kind. Two findings:

- The blind judge used in the Phase 6 studies was asked to score the same four transcripts five
  times each. Its count of distinct positions did not move at all (spread 0). Other fields moved
  by up to 2. So a gap of 1 in those other fields is inside the judge's own noise
  ([`../studies/JUDGE-VARIANCE.md`](../studies/JUDGE-VARIANCE.md)). The caveat in that document matters: four
  transcripts of one brief, where the count happened to be easy.
- The analyst that extracts the evidence plan is not stable. The same three transcripts, scored a
  day apart, gave a best-guess share of 0.36 and then 0.50, and a stated lean in one of three and
  then two of three ([`../studies/MODERATOR-ASSUMPTIONS.md`](../studies/MODERATOR-ASSUMPTIONS.md); the caveat added
  to [`../studies/EVIDENCE-LEAN.md`](../studies/EVIDENCE-LEAN.md)).

The practical reading: when you compare two runs, or two summaries, a small difference may be
noise in the run, noise in the analyst, or both.

## What a run can and cannot tell you

| A run can tell you | A run cannot tell you |
|---|---|
| Which objections these characters raised, in their own words | Which objections real people would raise |
| What each persona said would change its mind | Whether that condition would really change anyone's mind |
| What the analyst model read as the outcome | What the outcome "really" was |
| That an outcome is possible from this brief | How likely that outcome is (an ensemble gives a coarse count) |
| What changed after one fork | Whether the change caused the difference (one draw each side) |

## Related

- [How a run works](how-a-run-works.md)
- [Reading the results](reading-the-results.md)
- [Common misconceptions](common-misconceptions.md)
- The record: [`../ENSEMBLE-CONVERSATIONS.md`](../ENSEMBLE-CONVERSATIONS.md),
  [`../studies/JUDGE-VARIANCE.md`](../studies/JUDGE-VARIANCE.md),
  [`../studies/ASSUMPTION-ENSEMBLE.md`](../studies/ASSUMPTION-ENSEMBLE.md)
