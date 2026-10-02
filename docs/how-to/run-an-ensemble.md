# How to run an ensemble

Run the same brief several times, optionally in groups that differ in exactly one thing, and get one report that counts each conclusion per group.

An ensemble group may differ only in the speaker method, a scheduled message, or a working assumption. The form offers the first two; the third needs the API.

## Prerequisites

- Matrix Studio open and signed in.
- A brief that would make a good single run: topic and cast set up as in [How to start a run from a saved cast or a persona archetype](start-a-run-from-the-library.md).
- Budget for every conversation. The cost estimate at the bottom of the form prices the whole ensemble, including the report.

## Run the same brief several times

1. Open the new-run wizard: **New run** on the Runs screen, or **New ensemble** on the Ensembles screen (both open the same form).
2. On the **Topic** step, set **Run** to **Several times — same brief, then a report**.
3. Set **How many times** (2 to 12; 5 by default).

   **Generate avatars** turns off for an ensemble. Turn it back on only if you want portraits drawn again for every conversation.

4. Set up the cast and the other steps as for a single run.
5. On **Launch**, tap **Run N simulations**.

The ensemble's page opens. If research is on, it researches once before any conversation starts, and the conversations appear when it finishes.

## Add a group that differs in speaker method

1. Do steps 1 to 3 above.
2. Tick **Also compare against hybrid**, and set **Hybrid runs** (at least 2).

   The second group uses the hybrid method, which opens with rounds where everyone speaks at once and then switches to moderated turns. It uses the **Opening rounds** value, which the form shows only when the base **Conversation method** is Hybrid; otherwise it is 2.

3. Continue from step 4 above.

## Add a group that hears a scheduled message

1. On the **Assume** step, add the message under **Scheduled messages** (see [How to set working assumptions and scheduled messages](set-assumptions-and-scheduled-messages.md)). Use a speaker who is not in the cast; a cast member's name is refused.
2. Back on **Topic**, with **Run** set to **Several times**, tick **Also compare with and without the scheduled messages**, and set **Runs with the messages**.

   The base group runs without the messages; the new group hears them at the same turn in every run.

3. Continue from step 4 above.

The note under the ensemble options gives the total number of conversations. Trust that number: the **Launch** button and the **Type** row on the review count only the base and hybrid groups.

## Add a group that runs under a different working assumption

**Deployed system, API only.** The form has no control for this. Send the request yourself, with your sign-in token.

1. Write the request: the same body the form would send, plus `cells`. Each cell is a group with a label, a size and its `assumptions`:

   ```json
   {
     "topic": "Whether the volunteer library should open on Sunday mornings",
     "cast": [
       { "name": "Mina", "persona": "Runs the volunteer rota." },
       { "name": "Theo", "persona": "Speaks for families who want weekend hours." }
     ],
     "config": { "max_messages": 20 },
     "cells": [
       { "label": "low-turnout", "n": 3,
         "overrides": { "assumptions": [{ "statement": "About 40 people would visit on a Sunday morning" }] } },
       { "label": "high-turnout", "n": 3,
         "overrides": { "assumptions": [{ "statement": "About 150 people would visit on a Sunday morning" }] } }
     ]
   }
   ```

   The only keys a cell may override are `selection.method`, `selection.hybrid_opening_rounds`, `injections` and `assumptions`. Anything else, such as `max_messages`, is refused with the reason. Each cell needs at least 2 runs, and all cells together at most 12.

2. Copy your ID token from the signed-in app: open the browser's developer console on the app's page and run `JSON.parse(sessionStorage.getItem('matrix.tokens')).idToken`. It expires after about an hour.
3. Optionally price it first, then send it:

   ```bash
   curl -s -X POST "<SpaUrl>/api/ensembles/forecast" -H "Authorization: Bearer $ID_TOKEN" \
     -H "Content-Type: application/json" -d @ensemble.json
   curl -s -X POST "<SpaUrl>/api/ensembles" -H "Authorization: Bearer $ID_TOKEN" \
     -H "Content-Type: application/json" -d @ensemble.json
   ```

   The response holds the `ensemble_id`. The ensemble appears on the Ensembles screen.

## From the terminal

`scripts/start_conversation.py setup.json --owner <sub> --ensemble 5` starts a replicates-only ensemble, and `--hybrid 3` adds a hybrid group. Use `--dry-run` first to see the plan. Setup is in [How to start a run from a setup file](start-a-run-from-a-setup-file.md).

## Check it worked

- The ensemble's page lists **Groups** with what each varies ("nothing varied" for the base group) and **Conversations (X of N finished)**.
- On the Runs screen the ensemble appears under **Ensembles**; its conversations are folded under it, not listed among individual runs.
- The report is built when the last conversation ends: [How to read an ensemble's comparison report](read-an-ensemble-report.md).

## Related

- The ensemble request and every limit: [reference](../reference/)
- Why groups are counted separately and only one thing may vary: [explanation](../explanation/)
