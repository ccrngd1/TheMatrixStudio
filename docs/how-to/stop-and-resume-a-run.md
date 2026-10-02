# How to stop and resume a run

Stop a live run after the turn it is generating, and later continue it from where it stopped.

## Prerequisites

- Matrix Studio open and signed in.
- To stop: a run with status **Pending** or **Live**.
- To resume: a run with status **Stopped**, **Interrupted** or **Failed**.

## Stop a run

1. Open the run.
2. Tap **Stop** in the header.

   The button changes to "Stopping after this turn…". The turn being generated finishes and is kept; no further turns start.

3. Wait for the status to change to **Stopped**.

A stopped run keeps its transcript and final checkpoint, and the scrubber, asides, exports and theatre all work on it. It does not generate a summary on its own; to get one, open the **Analysis** tab and use **Generate summary**.

From a script, `POST /api/runs/{ref}/stop` does the same; it answers 409 if the run has already ended.

## Resume a run

1. Open the run.
2. Tap **Resume** in the header (it appears only for Stopped, Interrupted and Failed runs).

   It changes to "Resuming…", and new turns stream in on the same run, with the same name.

If **Resume** fails, the reason appears in red under the header.

## If the run is Complete or Capped

Neither can be resumed. To get more turns, branch it from its last turn with **Continue (+N turns)**: [How to branch a run from a turn](branch-a-run.md).

## If the run shows as Stalled

**Stalled** means the run is marked live but nothing has happened for two minutes; a run stuck at Pending usually never started executing. **Resume** is not offered while a run is marked live, and **Stop** has nothing running to act on. Check the turn loop's executions in Step Functions on the deployed stack, or the server log locally.

## Check it worked

- After a stop, the run sits under **Finished & stopped** on the Runs screen with a **Stopped** tag.
- After a resume, the turn count in the header rises past where it stopped.

## Related

- Run statuses and what each allows: [reference](../reference/)
