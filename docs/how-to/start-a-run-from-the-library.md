# How to start a run from a saved cast or a persona archetype

Start a conversation from a cast you saved earlier, or add one of the ready-made persona archetypes to a new run, from the **Library** tab.

## Prerequisites

- Matrix Studio open and signed in (on a deployed stack, with your Cognito user).
- For a saved cast: a cast saved as a template (see "Save a cast" below). Archetypes need nothing; they ship with the app.

## Start from a saved cast

1. Tap **Library** in the bottom bar.
2. Under **Saved casts**, find the cast and tap **Start a run with this cast**.

   The new-run wizard opens on its **Cast** step with a note: "Loaded the saved cast … from the library". The library copy is not changed by anything you edit here.

3. Edit the cast if you need to. Each persona's convictions and knowledge-base bindings came with it; pasted documents are never saved in a template, so add them again if the run needs them.
4. Tap **Topic** in the step bar and enter the topic. A run needs a topic and at least one persona with a name and a description.
5. Set anything else on the **Topic**, **Knowledge** and **Assume** steps.
6. Go to **Launch**, check the summary (each group has an **Edit** button), and tap **Run simulation**.

## Add a persona archetype

1. In **Library**, under **Persona archetypes**, tap **Add to a new run** on the archetype you want.

   The wizard opens on **Cast** with that persona added. Each archetype is marked "not yet qualified": nobody has measured that it holds its positions as written.

2. To add more archetypes, use **+ Add from library…** above the cast on the same step. A name already in the cast gets a number added, such as "Ines 2".
3. Continue from step 4 above.

You can also do both at once from inside the wizard: **Load template…** replaces the cast with a saved one (it asks first if the cast is not empty), and **+ Add from library…** adds an archetype.

## Save a cast

1. In the wizard's **Cast** step, build the cast: type personas, add archetypes, or describe the situation under **Draft a cast for me** and tap **Draft cast** (this replaces the cast and makes a model call).
2. Tap **Save as template**.
3. Enter a **Template name** (no `/`) and, optionally, what the cast is for. Tap **Save**.
4. If the name is taken, the form says so; tap **Replace it** to overwrite, or change the name.

The message after saving says how many pasted documents were left out. To reuse a document across runs, put it in a knowledge collection and bind that: [How to manage knowledge collections](manage-knowledge-collections.md).

To delete a template, open **Manage** beside **Save as template** and tap **delete** on it. Runs already started from it are not affected.

## Check it worked

After **Run simulation** the run's conversation screen opens. Its status reads **Pending** while it prepares, then **Live** with the turn count as messages arrive, and the run appears under **Live now** on the Runs screen. If a saved cast or archetype has since been deleted or renamed, the wizard says so in a red note instead of loading an empty cast.

## Related

- Every wizard field: [reference](../reference/)
- Why archetypes are labelled "not yet qualified": [explanation](../explanation/)
