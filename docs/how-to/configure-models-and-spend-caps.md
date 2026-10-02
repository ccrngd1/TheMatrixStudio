# How to configure models and spend caps

Choose which models a run uses, per deployment, per run or per role, and limit what a user or a run may spend.

## Prerequisites

- For deployment-wide settings: access to the server's environment (local) or to `cdk deploy` (deployed; administrator credentials, see [How to deploy Matrix Studio to AWS](deploy-to-aws.md)).
- For per-run settings: a signed-in user, or `scripts/start_conversation.py` with the credentials described in [How to start a run from a setup file](start-a-run-from-a-setup-file.md).

## Set the default model and the model list (local server)

1. Set the variables in the server's environment or in a `.env` file in the directory you start it from:

   ```bash
   LITELLM_MODEL=bedrock/global.anthropic.claude-sonnet-5     # the default
   AVAILABLE_MODELS=bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0,openai/gpt-4o
   ```

   `AVAILABLE_MODELS` is the list offered in the Model menus; the default model is always included first. Any LiteLLM model string works.

2. Set the credentials for each provider you list: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or AWS credentials (or `AWS_BEARER_TOKEN_BEDROCK`) for Bedrock.

3. Restart `matrix-studio serve`.

4. Check the result:

   ```bash
   curl -s http://127.0.0.1:8000/api/models
   ```

   `default` is the conversation model, `models` is the menu, and `roles` shows what each kind of call will use when a run names no model.

**Deployed system:** the stack sets none of these variables and has no context flag for them, so a deployed app uses the built-in default and its Model menu has one entry. Changing that means changing the stack.

## Choose the model for one run

1. On the new-run **Topic** step, pick a **Model**.

   This applies to every kind of call in the run, including speaker selection, the validation gate and naming, which otherwise use a cheaper model that honours a low temperature.

2. After the run, open **⋯** (Run options) and check **Models used**.

3. To change the model for the summary, asides and branches of an existing run, pick it under **Model** in Run options. It does not change turns already generated.

## Choose a model per role

The new-run form cannot do this, and **Import a setup** ignores the field. Use a definition file:

1. Add `config.models` to the definition, naming only the roles you want to change:

   ```json
   {
     "topic": "Whether the volunteer library should open on Sunday mornings",
     "cast": [{ "name": "Mina", "persona": "Runs the volunteer rota." }],
     "config": {
       "models": { "summary": "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0" }
     }
   }
   ```

   The roles are `voice`, `speaker_selection`, `validation`, `reflection`, `summary`, `aside`, `naming`, `wizard`, `pressure`, `stance` and `name_check`. To set one model for the whole run as well, put it in `config.model`; it applies to every role except `name_check`, the real-name check, which only `models.name_check` moves.

2. Print the resolved plan without starting anything:

   ```bash
   python scripts/start_conversation.py definition.json --owner <sub> --dry-run
   ```

   Each `model:<role>` line is what that role will use. The plan does not reflect a top-level `model` key, so use `config.model` in files you check this way.

3. Start it without `--dry-run`.

## Cap a user's monthly spend

**Deployed system.**

1. Deploy with a flat cap, per-group caps, or both:

   ```bash
   cd infra
   npx cdk deploy -c admin_email=you@example.com \
     -c user_monthly_cap_usd=25 \
     -c user_spend_caps_json='{"trial": 5, "staff": 100}'
   ```

   A user in several listed groups gets the highest of their caps. A group with a cap of `0` has no cap. A user in none of the listed groups gets `user_monthly_cap_usd`; `0` (the default) means no cap.

2. Put users in groups (needs Cognito admin permissions; `out` is the stack-output helper from [How to deploy Matrix Studio to AWS](deploy-to-aws.md)):

   ```bash
   aws cognito-idp create-group --user-pool-id "$(out UserPoolId)" --group-name staff
   aws cognito-idp admin-add-user-to-group --user-pool-id "$(out UserPoolId)" \
     --username <username-or-sub> --group-name staff
   ```

   A new membership takes effect when the user next signs in.

3. Check: the cost estimate at the bottom of the new-run form now says how much is left of the monthly budget. A launch over the cap is refused with the amount spent, and a live run that crosses it ends as `capped`.

On a local server set `MAX_USER_MONTHLY_COST_USD` and `USER_SPEND_CAPS_JSON` instead. A local request carries no groups, so only the flat cap applies.

## Cap each run's spend

1. Set `MAX_RUN_COST_USD` (in dollars, `0` = off) in the server's environment and restart it. It applies to every run on that server; a run that reaches it ends as `capped`.

**Deployed system:** the stack has no flag for this setting, so the deployed per-run cap is off. Use the monthly cap above.

Spend is counted from the cost the provider reports. A provider that reports none, such as a local Ollama model, counts as $0.

## Related

- Every setting, role and default: [reference](../reference/)
- Why some roles use a different model by default: [explanation](../explanation/)
