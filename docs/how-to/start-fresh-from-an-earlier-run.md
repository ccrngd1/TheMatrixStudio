# How to start a new run from an earlier run's setup

Copy an earlier run's topic and cast into the new-run form, change what you want, and run it from turn 0 as a separate conversation.

Nothing from the earlier transcript carries over, and the earlier run is not changed. To continue the earlier conversation from a point in it instead, branch it: [How to branch a run from a turn](branch-a-run.md).

## Prerequisites

- Matrix Studio open and signed in.
- The earlier run, in any status.

## Steps

1. Open the earlier run.
2. Tap **⋯** in its header and choose **Start fresh from this setup**.

   From the scrubber, **Start over with this setup** does the same.

3. Wait for "Prefilled from …" at the top of the wizard ("New run from a setup").
4. On the **Topic** step, read any amber notes under **Import a setup**. They name what could not be copied: documents attached to the whole cast rather than to one persona, documents with no recoverable text, and collections that the earlier run's research wrote into.
5. Change the **Run name (codename)**. It is filled in with the earlier run's name; if you keep it, the new run gets that name with `-2` added.
6. Make the change you came to make: the topic, a persona's convictions, who is in the cast, the documents.
7. Set again anything the copy does not carry. Copied: the topic, description, every persona (description, goals, positions, withheld concerns, what they will not weigh, their documents and their collection bindings), the turn count, model, avatars, cognition, the cast-wide collections and "End when the conversation is finished". **Not copied**, so check each:
   - the conversation method and **Closing round when the ceiling is reached** (Topic step)
   - consultants (Cast step)
   - **Research the subject before starting** and **Ask personas to cite their sources inline** (Knowledge step)
   - working assumptions, moderator assumptions and scheduled messages (Assume step)
   - **Summary options**, and the evidence-lean setting on the Cast step
8. Go to **Launch**, check the summary, and tap **Run simulation**.

## If the earlier run used research

Its research collections are left out on purpose. With **Research the subject before starting** on, the new run researches into collections of its own. To have the new run read the earlier research instead, bind those collections yourself on the **Knowledge** step; their names start with "Research —". See [How to research a subject before a run](research-before-a-run.md).

## Check it worked

The new run opens with its own name. Its header has no "From … @" chip, because it is not a branch, and the earlier run's lineage does not list it.

## Related

- The fields a setup holds: [reference](../reference/)
- Starting over versus branching: [explanation](../explanation/)
