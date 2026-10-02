# How to research a subject before a run

Have the app search the open web for sources on the topic before turn 1, store them in new collections, and bind those to the run.

Research writes only into collections it creates for that pass, named `Research — <run name> · <scope>`. It never writes into a collection you bound yourself (since 2026-10-01; see [PERSONA-RESEARCH.md](../PERSONA-RESEARCH.md) §5.1, "Reversed 2026-10-01").

## Prerequisites

- Matrix Studio open and signed in.
- A search provider configured for the deployment:
  - **Deployed system:** a Secrets Manager secret whose JSON holds one or more of `TAVILY_API_KEY`, `BRAVE_API_KEY` and `EXA_API_KEY`, passed at deploy time with `-c search_secret_arn=<secret-arn>` (see [How to deploy Matrix Studio to AWS](deploy-to-aws.md)).
  - **Local server:** the same key names as environment variables.

  Without one, a run that asks for research records it as **Unavailable** and runs without it.
- For per-persona research, personas with at least one position (Cast step, **Positions this persona defends**). A persona with none is skipped.
- Budget: a few minutes and roughly $0.25 for a six-persona cast, before the conversation.

## Steps

1. Set up the run as usual through the **Cast** step.
2. On the **Knowledge** step, tick **Research the subject before starting**.
3. Choose what to research:
   - **Shared research — one corpus every persona can search**
   - **Per-persona research — their case, and the case against it**: for each position, one search for it and one for what the persona said would change their mind
   - **Consultant research — a library for each consultant** (shown only when the run has consultants)
4. Set **Sources per query** (1 to 20; 5 by default). Higher finds more and costs more.
5. Go to **Launch**. The button now reads **Research N collections, then run simulation**, where N is one shared collection plus one per persona with positions plus one per consultant. Tap it.

The run is created at once and stays **Pending** while it researches; the conversation starts when research finishes.

## Check what it created

1. When the run has ended, open its **Analysis** tab and find **Pre-conversation research**. It shows:
   - the outcome: **Researched**, **Researched — nothing found**, **Unavailable**, **Failed** or **Skipped**, with the provider and cost;
   - how many sources it found, how many of them are **controlling** (a statute, regulation or ruling), and how many passages were embedded;
   - one row per scope (shared, each persona, each consultant) with the collection it wrote.
2. Open **Knowledge** to see the new collections, owned by you. They can be bound to other runs like any collection.

"Researched — nothing found" is a result, not a failure: the absence of a controlling source is stored as a finding the conversation can retrieve.

## If you run an ensemble, branch or start fresh

- **Ensemble:** research runs once, before any conversation starts, and every conversation reads the same collections. The panel is on the ensemble's page.
- **Branch or resume:** reads the parent's research and never searches again.
- **Start fresh from this setup:** the earlier run's research collections are not copied. With research on, the new run researches into new collections. To reuse the earlier research instead, bind those collections on the **Knowledge** step and leave research off.

## Related

- Research settings and the source tiers: [reference](../reference/)
- Why research gets collections of its own: [explanation](../explanation/)
