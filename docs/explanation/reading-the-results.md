# Reading the results

This page explains what each result of a run is, how it is produced, and how much weight it can
bear: the summary, stance, position-shift flags, the room map, ensemble reports and the decision
brief. For where to find them in the app, see [`../how-to/`](../how-to/). For field-by-field
definitions, see [`../reference/`](../reference/).

The short version: the **transcript and the event log are the record**. Almost everything else is
a reading of that record, by a model or by a simple rule, and each one is labelled with how it was
made so you can check it.

## The summary is analysis, not ground truth

When a run completes, an analyst model reads the transcript and writes a structured summary: an
overview, points of consensus, dissenters and what they objected to, key ideas, open questions, an
evidence plan, and a conditional recommendation. Asides (questions you put to the analyst, a
persona, the room or a consultant afterwards) use the same machinery and never change the run
([`../../matrix_studio/analysis.py`](../../matrix_studio/analysis.py)).

The module says what this is in its first lines: "model-generated ANALYSIS of the transcript, not
ground truth or canonical persona statements". Exports repeat the label in words, because a file
loses the styling that marks it in the app ([`../../matrix_studio/export.py`](../../matrix_studio/export.py)).

Some guardrails always apply, even if you supply your own analyst instructions. The analyst must
base every point on what was said and must not invent content or speakers. In the evidence plan,
any column the conversation did not supply is written as exactly "not stated", and the conditional
recommendation must say the lean is "not stated" when nobody gave a best guess. The fixed wording is
deliberate: counting it is how the project measured how often a request for evidence came without a
guess.

Three measured facts limit how far a summary can be trusted:

- **The analyst is not stable on identical transcripts.** The same three runs, scored a day apart,
  gave a best-guess share of 0.36 and then 0.50, and a stated lean in one run of three and then two
  ([`../MODERATOR-ASSUMPTIONS.md`](../MODERATOR-ASSUMPTIONS.md)). One run scored 0.00 on one pass and
  1.00 on another ([`../EVIDENCE-LEAN.md`](../EVIDENCE-LEAN.md), re-analysis).
- **It can leave things out.** After the evidence plan was added, 10 of 50 summaries were stored
  without an overview, though the reply was complete. Moving the overview to the front of the request
  fixed it, and a summary now names any field the model omitted rather than showing an empty one
  (`_summary_system_prompt` and `_coerce_summary`).
- **Its counts depend on its reading.** A summary's dissenters list decides part of the stance
  (below), so a dissenter the analyst missed changes a persona's stance.

When the summary and the transcript disagree, the transcript is right.

## Stance: where each persona ended

Every finished run with a summary gets a stance per persona, shown on run cards, in the HUD, on the
room map and in the Analysis tab. There are four states
([`../../matrix_studio/stance.py`](../../matrix_studio/stance.py)):

| | state | meaning |
|---|---|---|
| ▲ | support | accepts the outcome the room reached |
| ◐ | with conditions | accepts, but on something not yet done or settled, or while keeping an objection of its own standing |
| ▼ | holding out | does not accept it |
| ◆ | not stated | nothing recorded says which way they went |

Stance is computed **after the run**, when the summary is written. A live run shows turn and spend
but no stance. The alternative, a classifier labelling every turn live, was set aside with a note
that the project's record on classifiers is mixed, and the owner chose post-run only
([`../MOBILE-UI.md`](../MOBILE-UI.md) §6.1).

### How it is derived

Each persona's stance comes from one of two sources, in this order:

1. **Their closing statement, if the run had a closing round.** One model call reads every
   closing statement in the room and classes each as *accepts*, *accepts with conditions*,
   *rejects* or *unclear*. For each it must quote, character for character, the sentence that
   decides the class. **The code then checks that the quote really is in that persona's
   statement**, ignoring only case, curly quotes and spacing, and that it is at least 12 characters
   long. A quote that is not there makes the verdict *unclear*. The quote shown to you is cut from
   the persona's own words, never the model's rendering of them. The classifier runs at temperature 0
   on a small model, so that a regenerated summary should get the same verdicts.
2. **Otherwise, the summary.** A persona named among the summary's dissenters is ▼. A persona with
   a flagged position shift who is not a dissenter is ▲. Everyone else is ◆. If the summary has no
   dissenter list at all, nobody gets a stance from this rule, because "not a dissenter" would be a
   guess.

Each persona's basis is stored with its stance: the class, the quote, which source decided it, and
why the closing statement did not, if it did not. The dossier and the Analysis tab show it.

Why the verbatim check? "This project has reverted classifiers that sounded right and failed
held-out checks." A classifier that is confidently wrong is the failure to design against. The
module is honest about the limit: "A quote proves the model read the sentence, not that it read it
right." The classifier was smoke-checked on four live closing statements, agreeing with a careful
human reading four times out of four, the same on three repeats. None of the four was a rejection.
That is a smoke check, not a validation, and a held-out check against hand labels is still owed.

Stances are not backfilled: only summaries generated from 1 October 2026 carry this form.

### Why "not stated" is not "undecided"

"Not stated" means the record does not say. It does not mean the persona was torn.

The reason this distinction exists is a specific run. With only the summary rule, four of its six
personas had plainly accepted the final plan, most of them in so many words, and read as "not
stated". Accepting is not a position shift, and the summary rule only knows about shifts and
dissenters. A persona can also be "not stated" simply because the run had no closing round, or its
closing statement was unclear, or the quote did not check out. The basis tells you which.

### Why "with conditions" is its own state

In the same run, two personas signed the plan while keeping a standing objection. The summary rule
showed them as holding out, which hid that they had signed. Forcing them into support would hide
the objection instead. So acceptance with conditions became a state of its own.

It is also kept out of the headline number. "% support" counts ▲ alone, and the with-conditions
share is printed beside it ("50% support · +17% with conditions"), "because conditions that are
never met would make a combined figure overstate the room" ([`../MOBILE-UI.md`](../MOBILE-UI.md)
§6.1; [`../../frontend/src/components/run/Stance.tsx`](../../frontend/src/components/run/Stance.tsx)).

And "support" from the summary rule is the weakest state of all. It means a shift was flagged and
the persona was not listed as a dissenter. "A flagged shift is a word match, and moving is not the
same as agreeing."

## Position shifts are a word match that asks to be checked

When a persona says its position moved, the message gets a **SHIFT** flag. The flag shows what the
persona credited and the conditions it had said would move it, and marks the case where none of
those conditions appears to be named ([`../../matrix_studio/shifts.py`](../../matrix_studio/shifts.py)).

How it works matters for how you read it:

- **It looks for realised shifts in words**: "you've convinced me", "I concede", "that changed my
  mind". Conditional and negated forms are excluded: "that would move me", "nothing has changed my
  position". On the twelve evidence-lean runs (480 messages) the first version fired 51 times,
  almost all prospective; the current one fires 8 times, each a real shift or an explicit concession.
  It still makes mistakes. In the run that led to the stance change above it flagged a shift that
  was not one and missed one that was.
- **What it credits comes from the names in the message**: another persona, a consultant, a
  scheduled message, a working assumption. It is never guessed from who spoke last. A shift that
  credits nobody is recorded as crediting nobody, "which is itself worth a reader's attention".
- **"Appears to name a condition" is a word overlap**: at least two content words shared between
  the message and one of the persona's stated conditions. It is not a judgement that the condition
  was met.
- **It never changes the turn.** A gate that regenerated "abandoned convictions" was deliberately
  not built: "conceded a position" is a matter of degree, and a false positive would rewrite a turn
  in which a persona legitimately changed its mind. "This records; the reader decides."

The flag exists because of one finding. The only fold found in the evidence-lean folding check was a
persona giving ground for a reason on neither of its conditions while saying it was on its list
([`../EVIDENCE-LEAN-FOLDING.md`](../EVIDENCE-LEAN-FOLDING.md)). Nothing had checked. Now the
evidence for a check is in front of you, and the check is yours.

## The room map shows sequence, not replies

The room map draws one node per persona on a ring, sized by turns taken, with a halo in its end
stance. It draws one curve between each pair of personas who **spoke back to back**, thicker the more
often they did. Consultants and injected messages are left out.

A curve means "one spoke right after the other". It does not mean "they argued" or "one answered the
other". The engine records no reply structure. In round-based methods adjacency is forced by the
order of the round, so curves overstate exchange there in particular. The design weighed adding reply
attribution and chose to label the map honestly instead, because a reply detector would be another
classifier with the same measurement burden as stance
([`../MOBILE-UI.md`](../MOBILE-UI.md) §6.2;
[`../../frontend/src/components/run/Stance.tsx`](../../frontend/src/components/run/Stance.tsx), `RoomMap`).

What the map does show well is who was left out: a small node with few curves is a persona the
conversation did not reach.

## Convergence is not agreement

A run "converges" when, in the moderated method, the moderator declines to pick anyone twice in a
row after everyone has spoken, or, in a round-based method, everyone passes in the same round.
That says nobody had anything left to add. It does not say whether the room *resolved* the question
or *folded*.

The project tried to build a flag for the difference and could not. The phrase lists that detect
accommodating language recalled only 0.41 of hand-labelled accommodation on that cast, below the
pre-registered bar, so no flag was added and convergence "stays unqualified, and says so". In the
hand-labelled sample, the closing turns of converged runs were mostly accommodating language,
including a run judged to have genuinely settled, so accommodating language may simply be how a
converged ending sounds ([`../CAPITULATION-STUDY.md`](../CAPITULATION-STUDY.md)). Read the last
turns before you read "converged" as "agreed".

## Ensembles count per group and never pool

An ensemble runs one brief several times. By default all runs are identical replicates; you can add
groups that each differ from the base in exactly one declared thing: the speaker method (and its opening
rounds), a scheduled message, or a working assumption
([`../../matrix_studio/ensemble_spec.py`](../../matrix_studio/ensemble_spec.py), `CELL_OVERRIDES`).

Why so restrictive:

- **Replicates first.** The question an ensemble answers is "can I trust this conclusion?", and
  that needs repeats under identical conditions. Varying settings across single runs "produces
  differences you cannot attribute" ([`../ENSEMBLE-CONVERSATIONS.md`](../ENSEMBLE-CONVERSATIONS.md)).
- **One declared difference, proved.** Before an ensemble is created, the planner compares the
  actual configs of every member, not what the groups declared, and refuses anything that differs
  beyond the declared keys. Tempting variables are refused with their reasons in the error. Turn
  count is refused because a run that ends early produces "a missing datum that reads as a dissent".
  Persona instructions are refused because "the personas are the measuring instrument": telling one
  group to "be more sceptical" manufactures the disagreement the ensemble claims to discover.
- **Never pooled.** Suppose a conclusion appears in five of nine runs. Pooled, that reads as a weak
  split. By group it could be 5 of 5 against 0 of 4, a strong finding that depends on the method, or
  3 of 5 against 2 of 4, a coin flip in both. Same numerator, three different actions. So the claim
  table has no total column, and a test asserts its absence (§4, §8).

Each claim gets a coarse tier per group: unanimous, split, rare (exactly one run) or absent. There is
no percentage, because five replicates do not support reading 3 of 5 against 2 of 5 as a difference.
A group that produced no usable extraction shows "—", not "0 of N": an empty group has no opinion.
The report refuses to exist with fewer than two usable runs, because "one conversation under an
'ensemble report' heading is the most misleading artefact this system could emit".

The counts themselves are made by a model grouping similar claims, biased against merging, and the
report says so in its own body. If that grouping fails, the report falls back to plain text matching
and says its counts are not reliable: on one five-run ensemble, text matching counted 128 of 128
claims as unique ([`../../matrix_studio/ensemble_reporting.py`](../../matrix_studio/ensemble_reporting.py)).
Check the grouped phrasings before trusting a count.

A worked example of reading one honestly: an ensemble varied one working assumption across two
groups of three. One conclusion appeared in two of three runs of one group and none of the other,
which met the pre-registered bar. The write-up then put it in context: 32 distinct conclusions, 28 of
them in a single run, so "one 2–0 split among 32 candidates is the weakest form the criterion allows"
and could arise by chance ([`../ASSUMPTION-ENSEMBLE.md`](../ASSUMPTION-ENSEMBLE.md)).

## The decision brief

The brief is a one-page view for someone who will not open the app. Its rule: "No number without its
group and replicate count. Where either is missing, it says 'not yet measured'." For a single run
the confidence line therefore always reads "not yet measured", and the brief says how to get a
measurement, which is to run an ensemble. For an ensemble it leads with conclusions that recurred,
counted per group. Every list is capped, and a cap that cut something says how much was cut
([`../../matrix_studio/brief.py`](../../matrix_studio/brief.py)).

## Related

- [What Matrix Studio is for](what-matrix-studio-is-for.md)
- [Common misconceptions](common-misconceptions.md)
- The record: [`../MOBILE-UI.md`](../MOBILE-UI.md) §6, [`../ENSEMBLE-CONVERSATIONS.md`](../ENSEMBLE-CONVERSATIONS.md),
  [`../EVIDENCE-LEAN-FOLDING.md`](../EVIDENCE-LEAN-FOLDING.md), [`../CAPITULATION-STUDY.md`](../CAPITULATION-STUDY.md)
