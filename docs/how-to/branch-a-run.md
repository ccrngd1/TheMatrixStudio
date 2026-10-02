# How to branch a run from a turn

Fork a finished run at a chosen turn into a new run, either unchanged or with one change applied at the fork, and let it generate forward.

The original run is never modified. To start over from turn 0 with a different setup instead, see [How to start a new run from an earlier run's setup](start-fresh-from-an-earlier-run.md).

## Prerequisites

- Matrix Studio open and signed in.
- A run that has ended with a transcript: status **Complete**, **Stopped** or **Capped**. A failed or interrupted run cannot be branched from the scrubber; resume it instead ([How to stop and resume a run](stop-and-resume-a-run.md)).
- Branching generates new turns, which costs money.

## Steps

1. Open the run.
2. Open the scrubber in one of three ways:
   - tap **Scrub** below the conversation;
   - tap **⋯** in the header and choose **Scrub and branch**;
   - tap a message (or its turn number) and, in **Message context**, tap **Branch from turn NN**. The scrubber opens at that turn.
3. Choose the turn to fork from: drag the slider, tap a bar in the timeline, or use the previous and next arrows. The transcript beside the controls shows the conversation as it stood at that turn. Turn 0 is before anyone spoke.
4. Under **Change at this turn**, choose one:

   | Choice | What to fill in |
   |---|---|
   | **— none (fork unchanged) —** | nothing; the fork starts from identical state |
   | **Inject message** | a speaker name (it can be someone new, such as "Moderator"), the message, and **New discussion turns** after it |
   | **Continue (+N turns)** | **Add turns** |
   | **Edit goal** | the persona, and their new goals one per line |
   | **Add persona** | a name, a description and optional goals |
   | **Remove persona** | the persona |
   | **Change an assumption** / **Withdraw an assumption** | the assumption, and for a change its new value (offered only when an assumption is in force at that turn; see [How to fork a run with a different assumption](fork-with-a-different-assumption.md)) |
   | **Adaptive pressure (experimental)** | an optional focus; works only if the server has `ADAPTIVE_PRESSURE_ENABLED=true`, and is refused otherwise |

5. Optionally choose a **Model** for the branch. It defaults to the model selected in the run's options.
6. Read the cost note under the controls ("A branch from turn N costs …"). Everything after the fork turn is generated again, so a later fork is cheaper.
7. Tap **Branch from here** (no change) or **Branch with change**.

The new run opens and starts generating.

## If the run stopped short and you only want more turns

Fork from the last turn with **Continue (+N turns)**. This is also the way forward for a **Capped** run, which cannot be resumed. If it was capped by a monthly budget, the branch stops again until the budget resets or is raised.

## Check it worked

- The new run's header shows a **From** chip naming the parent and the fork turn; tap it to go back.
- The parent's header lists the new run under **Branches**, and its **Analysis** tab shows them all under **Timeline branches**.
- On the Runs screen the new run is tagged "branch @ N", and **Branches only** filters the list to branches.

## Related

- Every change kind and its fields: [reference](../reference/)
- What a fork can and cannot tell you: [explanation](../explanation/)
