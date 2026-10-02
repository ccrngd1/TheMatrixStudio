# How to set working assumptions and scheduled messages

Tell the room what to reason from when nobody in it can know, and schedule messages that enter the conversation after a given turn.

A working assumption is shown to every persona and marked in the transcript; it is not evidence, and a persona may dispute it. A scheduled message appears as injected by you, never as something a persona chose to say, and does not use up a turn.

## Prerequisites

- Matrix Studio open and signed in, with the new-run wizard open.
- The run's turn count set on the **Topic** step (**Max messages**). A message must be scheduled before the last turn.

## Add working assumptions

1. Go to the **Assume** step.
2. Under **Working assumptions**, tap **+ Add assumption** (up to eight).
3. Type the statement, for example "Assume about 60 people would visit on a Sunday morning", and optionally its **Basis**. Each field takes up to 300 characters.

   They are numbered A1, A2 and so on in the order you add them.

4. To let the moderator add its own when the room is stuck on something nobody can know, tick **Let the moderator add assumptions when the room is stuck on something nobody can know**. It checks every four turns, adds at most three per run, and costs one small model call per check. There is no approval step.

## Add scheduled messages

1. On the same step, under **Scheduled messages**, tap **+ Add message** (up to five).
2. Set **After turn**: the message enters after that turn. It must be lower than the run's turn count.
3. Enter who it is **From** (for example "Customer" or "City council"), up to 60 characters, and the text, up to 2,000 characters.

   Use a name outside the cast. In an ensemble that compares runs with and without the message, a cast member's name is refused.

4. If you lower **Max messages** afterwards, check that every message still comes before the last turn; otherwise the launch is refused with the turn that could never be reached.

## Launch

1. Go to **Launch**. The **Assume** group shows how many assumptions and scheduled messages are set.
2. Tap **Run simulation**.

## Check it worked

- Each assumption appears in the conversation as an **ASSUMES** card ("A1 … — set before the run"). The card counts the messages that cite it and names anyone who appears to dispute it. One the moderator added says who made it and at which turn.
- Each scheduled message appears after its turn, marked as injected, and in the scrubber's timeline as an injection mark.
- The decision brief lists what was assumed ([How to export a run or a decision brief](export-a-run-or-a-brief.md)).

## Next

- To see what an assumption was worth, fork with a different value: [How to fork a run with a different assumption](fork-with-a-different-assumption.md).
- To compare runs with and without a message, use an ensemble: [How to run an ensemble](run-an-ensemble.md).
- To add a message to a conversation that has already happened, branch it with **Inject message**: [How to branch a run from a turn](branch-a-run.md).

## Related

- Limits and fields: [reference](../reference/)
- Assumptions versus evidence: [explanation](../explanation/)
