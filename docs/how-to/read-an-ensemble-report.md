# How to read an ensemble's comparison report

Find an ensemble's report, get it built if it has not been, and read what held in each group.

## Prerequisites

- Matrix Studio open and signed in.
- An ensemble ([How to run an ensemble](run-an-ensemble.md)).

## Open the report

1. Tap **Ensembles** in the bottom bar, then the ensemble. (On the Runs screen, ensembles are listed under **Ensembles** at the top.)

   The card says **report ready**, **report failed**, or the ensemble's status.

2. Scroll to **Report**. What you see depends on its state:

   | Shown | Do this |
   |---|---|
   | "Waiting for N conversations to finish" | wait; the report is built only when every conversation has ended |
   | "Building the report — started …" | wait; it takes several minutes and appears on its own, so you can leave the page |
   | "Every conversation has finished and no report is being built." | tap **Build the report** |
   | an error in red | read it, then tap **Try again** |
   | the report | read on |

Building costs one model pass per conversation plus one over all of them.

## Read it

Read the sections in this order:

1. **Groups**, at the top of the page: what each group varied, and how many of its conversations completed. A group that is short of conversations has its counts out of fewer runs; a member that never started is listed as "never started" under **Conversations**.
2. **What the runs concluded**. The table **Reached in two or more runs of a group** has one row per conclusion and one column per group. Each cell reads "N of M" with a tier: **every run**, **some runs** or **1 run only**. A cell showing "—" means that group produced nothing usable, which is different from "0 of M". Conclusions that never recurred inside a group are folded under **Never reached twice within a group**; open it to see them.
3. **What every run in a group agreed on**: demands and refusals held in all runs of at least one group, with which groups.
4. **What held, by group**: the full claim table, per group.
5. **Across the conversations**: the written synthesis.
6. The amber notes at the end: the report's own caveats. Read them before you compare numbers.

Compare groups column by column. There is no total across groups by design: a conclusion held in every run of one group and none of the other is a different finding from one split evenly in both.

## Rebuild or take it away

- **Rebuild report** builds it again and charges again. Grouping of similar conclusions can come out differently on a rebuild; the old report stays on screen until the new one lands.
- **Brief** opens a one-page decision brief of the ensemble, and the **Export** buttons download it as Markdown, HTML or PDF. Each finished conversation also has its own **Brief** in the **Conversations** list. See [How to export a run or a decision brief](export-a-run-or-a-brief.md).
- To read one conversation in full, tap its name under **Conversations**.

## Check it worked

The ensemble's card on the Ensembles screen reads **report ready**, and the page shows the report cost under the caveats.

## Related

- The report's fields and tiers: [reference](../reference/)
- Why counts are per group and never pooled: [explanation](../explanation/)
