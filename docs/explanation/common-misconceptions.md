# Common misconceptions

Each entry states a reading that is easy to fall into, the correction, and where the longer
explanation lives.

### "A line on the room map means they argued."

A line means two personas spoke **back to back**, nothing more. The engine records no reply
structure, so the map cannot know who answered whom. In round-based methods the order of the round
forces adjacency, so lines overstate exchange there in particular. What the map does show well is
who was left out.
See [Reading the results](reading-the-results.md#the-room-map-shows-sequence-not-replies);
[`../MOBILE-UI.md`](../MOBILE-UI.md) §6.2.

### "'Not stated' means undecided."

"Not stated" means **nothing recorded says which way the persona went**. Often the persona was
clear and the rule simply could not see it: accepting a plan is not a position shift, a run may have
had no closing round, or the classifier's quote failed the verbatim check. The stance basis says
which. See [Reading the results](reading-the-results.md#why-not-stated-is-not-undecided);
[`../../matrix_studio/stance.py`](../../matrix_studio/stance.py).

### "A SHIFT flag means they folded."

A shift flag means the persona **said** its position moved, found by matching phrases like "you've
convinced me". It also lists what the persona credited and the conditions it had stated, and marks
when none of those conditions appears to be named. Whether that was a legitimate change of mind or a
fold is a judgement the flag leaves to you, by design. The phrase matching also misfires in both
directions. See [Reading the results](reading-the-results.md#position-shifts-are-a-word-match-that-asks-to-be-checked);
[`../studies/EVIDENCE-LEAN-FOLDING.md`](../studies/EVIDENCE-LEAN-FOLDING.md).

### "More turns give a better answer."

Not reliably. One fair 40-turn run had stated every position by turn 25 and spent the last fifteen
turns on "nothing to add". Turn count can also shift outcomes systematically: in one set of runs a
blocking objection was held at 24 turns and logged as an accepted risk at 40. That is a bias, not an
improvement, and it is why ensembles refuse to vary turn count. See
[`../studies/SPEAKER-SELECTION-EVALUATION.md`](../studies/SPEAKER-SELECTION-EVALUATION.md) §13;
[`../ENSEMBLE-CONVERSATIONS.md`](../ENSEMBLE-CONVERSATIONS.md) §3.4.

### "The summary is what happened."

The summary is an analyst model's **reading** of the transcript. The same transcripts scored by the
same analyst a day apart gave different results. When the summary and the transcript disagree, the
transcript is right. See [Reading the results](reading-the-results.md#the-summary-is-analysis-not-ground-truth);
[`../studies/MODERATOR-ASSUMPTIONS.md`](../studies/MODERATOR-ASSUMPTIONS.md).

### "Converged means they agreed."

Converged means **nobody had anything left to add**: the moderator declined to pick anyone twice in a
row, or everyone passed in a round. It does not say whether the room resolved the question or gave
ground without a reason. An attempt to flag the difference failed its own validation step. See
[`../studies/CAPITULATION-STUDY.md`](../studies/CAPITULATION-STUDY.md).

### "Hiding a run deletes it."

Hiding sets one flag on the run. The run still opens by its link, still sits in its ensemble, and its
cost still counts. Showing it again removes the flag and leaves the run exactly as it was. See
[`../../matrix_studio/api/app.py`](../../matrix_studio/api/app.py), `set_run_hidden`.

### "One run tells you what the room thinks."

One run is one draw. Two runs with byte-identical settings reached different conclusions, and in one
six-run ensemble 28 of 32 distinct conclusions appeared in a single run only. The decision brief's
confidence line for a single run always reads "not yet measured". See
[What Matrix Studio is for](what-matrix-studio-is-for.md#why-one-run-is-an-anecdote);
[`../ENSEMBLE-CONVERSATIONS.md`](../ENSEMBLE-CONVERSATIONS.md) §2.

### "A fork shows what the change caused."

A fork is one draw on each side of the change, and everything after the fork point is generated
afresh. It shows a difference; it cannot attribute it. To attribute it, make the change an ensemble
variable and compare groups. Note too that a branch does not carry the parent's memories,
reflections or relationship notes. See [How a run works](how-a-run-works.md#branching-and-forking);
[`../studies/ASSUMPTION-ENSEMBLE.md`](../studies/ASSUMPTION-ENSEMBLE.md).

### "Research makes a run more accurate."

In three pre-registered comparisons, research reached every conversation and did not settle the
question it was built to answer. It was recorded as not useful for that brief, though harmless. The
web also blocks automated readers at many authoritative sites, so a researched corpus leans toward
whatever is easy to fetch. See [Evidence](evidence-and-retrieval.md#what-research-was-measured-to-do);
[`../PERSONA-RESEARCH.md`](../PERSONA-RESEARCH.md) §9, §12.5.

### "A citation means the source says that."

The citation check confirms that the persona **had the passage in front of it**, or credited
someone who did. It does not compare the sentence with the passage, and an uncited claim is not
checked at all. See [Evidence](evidence-and-retrieval.md#what-it-cannot-check).

### "The why-trace is the persona's real reasoning."

The rationale is written in the same model call as the turn, which makes it better than an
after-the-fact explanation, but it is still the model describing itself. It can be mistaken or
rationalised. See [How a run works](how-a-run-works.md#cognition-memory-reflection-and-the-why-trace);
[`../project/PHASE2C-REQUIREMENTS.md`](../project/PHASE2C-REQUIREMENTS.md).

### "A working assumption is evidence."

An assumption is something the room is told to reason *from* when nobody can know the answer. The
personas are told it changes what they would do if it holds, not what they are convinced of, and it
never satisfies a "what would change my mind" condition. See
[`../../matrix_studio/assumptions.py`](../../matrix_studio/assumptions.py).
