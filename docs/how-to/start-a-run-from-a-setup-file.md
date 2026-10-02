# How to start a run from a setup file

Load a conversation definition from a JSON file into the new-run form, or start it on the deployed stack straight from the terminal.

The file format is the body the run API accepts: `topic` and a `cast` whose members each have a `name` and a `persona`; everything else is optional. `examples/minimal.json` is a small example.

## Prerequisites

- A setup file, for example:

  ```json
  {
    "topic": "Whether the volunteer library should open on Sunday mornings",
    "cast": [
      { "name": "Mina", "persona": "Runs the volunteer rota.", "goals": ["Keep the rota sustainable"] },
      { "name": "Theo", "persona": "Speaks for families who want weekend hours.", "goals": ["Open on Sundays"] }
    ],
    "config": { "max_messages": 12 }
  }
  ```

- For the form: Matrix Studio open and signed in.
- For the terminal: see "Start it from the terminal" below.

## Load it into the form

1. Tap **New run** on the Runs screen.
2. On the **Topic** step, find **Import a setup**.
3. Tap **Choose file…** and pick the file, or paste the JSON into the box below it.

   The topic and the whole cast are replaced. Anything the form could not use is listed in amber under the box: a persona without a `name` or `persona`, a duplicate name, or document paths (a browser cannot read server paths; paste the text into `document_texts` instead).

4. Check the steps. The form reads only part of a setup:

   | Read from the file | Not read: set it in the form after loading |
   |---|---|
   | topic, name, description | speaker method, closing round |
   | each persona's name, description, goals, positions with firmness and what would change them, underlying concerns, what they will not weigh, documents as `document_texts`, knowledge-base bindings | research, inline citations, evidence lean |
   | `max_messages`, the top-level `model`, avatars, cognition | consultants, scheduled messages, `config.model` |
   | cast-wide knowledge bases, working assumptions, moderator assumptions, "end when finished", hidden agendas (`config.personas.withhold_concerns`; a file that does not say leaves it off) | per-role models (`config.models`) |

   Other structured-persona fields (role, background and formative events, `optimises_for`, `persuaded_by`, `formed_by`) are dropped without a warning. To keep them, start the file from the terminal.

5. Go to **Launch** and tap **Run simulation**.

## Start it from the terminal

**Deployed system.** `scripts/start_conversation.py` validates the file with the API's own request model and starts it on the deployed state machine, so every field in the file is honoured. It needs credentials that can write the stack's DynamoDB tables and S3 bucket and call `states:StartExecution`, and it costs money once it starts.

1. Set the environment:

   ```bash
   export AWS_REGION=<region> TABLE_PREFIX=matrix-studio
   out() { aws cloudformation describe-stacks --stack-name matrix-studio-stack \
     --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
   export DATA_BUCKET=$(out DataBucketName) VECTOR_BUCKET=$(out VectorBucketName)
   export USER_POOL_ID=$(out UserPoolId)
   export TURN_LOOP_ARN=$(aws stepfunctions list-state-machines \
     --query "stateMachines[?name=='matrix-studio-turn-loop'].stateMachineArn" --output text)
   ```

   Without `TURN_LOOP_ARN` the script refuses to start, because the run would be created and never execute.

2. Check the file and the model plan without creating anything:

   ```bash
   python scripts/start_conversation.py setup.json --owner <sub> --dry-run
   ```

   `<sub>` is the Cognito `sub` of the user who should own the run; the run appears in their Runs screen. An unknown key in `config` is refused here with its name.

3. Start a short run first to prove the path, then the full one:

   ```bash
   python scripts/start_conversation.py setup.json --owner <sub> --max-messages 4
   python scripts/start_conversation.py setup.json --owner <sub>
   ```

   The script watches the run and prints turns as they land. You can stop the script at any time; the run carries on. Re-attach with `--watch <run-id>`, or pass `--no-watch` to exit at once.

   The script refuses an owner that matches no Cognito user and owns no runs. `--allow-unknown-owner` overrides that for a genuinely new user.

To run the file several times as an ensemble, add `--ensemble N` (and `--hybrid N` for a second group that differs only in speaker method). See [How to run an ensemble](run-an-ensemble.md).

## Check it worked

The run opens (form) or the script prints `started` with the run's name, and the run appears on the owner's Runs screen.

## Related

- The full request format: [reference](../reference/)
