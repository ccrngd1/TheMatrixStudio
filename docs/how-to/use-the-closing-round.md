# How to use the closing round and read each persona's final stance

Give a run one final round in which every persona states where they now stand, then read each persona's stance: support, with conditions, holding out, or not stated.

## Prerequisites

- Matrix Studio open and signed in, with the new-run wizard open.
- Budget for one extra round: one model call per persona.

## Turn on the closing round

1. On the **Topic** step, tick **Closing round when the ceiling is reached**.
2. Set up and launch the run as usual.

The closing round runs only when the run reaches its turn count (**Max messages**, or **Turn ceiling** with "End when the conversation is finished" on). A run that ends on its own first, because the moderator judged it finished or everyone passed, gets no closing round.

In the conversation the closing round appears as one round in which everyone spoke at once ("round N · X spoke at once"). Each persona says where they stand, what they can accept and what they cannot.

## Read the stances

Stances are worked out when the summary is generated, so a live run has none.

1. Wait for the run to end. A **Complete** run generates its summary on its own (unless you turned that off under **Summary options**). For a **Stopped** or **Capped** run, open the **Analysis** tab and tap **Generate summary**, then **Regenerate with this prompt**.
2. Open the **Analysis** tab. **Where the room ended** shows the share who support, and below it each persona with their stance and its basis.

   | Stance | Means |
   |---|---|
   | ▲ support | their closing statement accepts the outcome, or the summary records their position moving and does not list them as a dissenter |
   | ◐ with conditions | their closing statement accepts it on conditions |
   | ▼ holding out | their closing statement rejects it, or the summary names them as a dissenter |
   | ◆ not stated | nothing recorded says which way they went |

   The basis says whether the closing statement or the summary decided, and quotes the sentence it rests on where there is one. When the closing statement could not decide (no statement, an unclear one, or a quote that is not really in it), the line says so and the summary decides.

3. Read the share as written: "% support" counts ▲ only. Those accepting with conditions are shown beside it as their own share.

The same stances appear elsewhere:

- on the run's card in the Runs list, as coloured rings round each persona and counts beside them;
- in the room map on the **Cast** tab (the cast drawer on a wide screen), as ring colours;
- in each persona's dossier, under **Stance**, with "from closing statement" or "from summary".

Regenerating the summary works the stances out again.

## Check it worked

The basis lines read "From their closing statement" for the personas the closing round decided. If every line reads "From the summary", check that the run reached its turn count; otherwise it had no closing round. Runs summarised before 2026-10-01 show stances without a basis.

## Related

- The stance rules in full: [MOBILE-UI.md](../MOBILE-UI.md) §6.1, and [reference](../reference/)
- Why the closing round does not ask for consensus: [explanation](../explanation/)
