# Settings and environment variables

Every deployment setting and environment variable Matrix Studio reads, with its default and effect. Sources: `matrix_studio/settings.py` (the `Settings` class), every direct `os.environ` / `os.getenv` read under `matrix_studio/`, the model roles in `matrix_studio/models.py`, the Lambda environments set by `infra/matrix_infra/stack.py`, and the CDK context read by `infra/matrix_infra/config.py`. Variables read only by scripts are listed in [cli-and-scripts.md](cli-and-scripts.md).

See also: [explanation](../explanation/) (why the defaults are what they are), [how-to guides](../how-to/) (configuring a deployment), [http-api.md](http-api.md), [run-config.md](run-config.md), [events.md](events.md), [data-model.md](data-model.md), [cli-and-scripts.md](cli-and-scripts.md).

## How settings are read

| Rule | Behaviour |
|---|---|
| Env var name | The upper-case field name. No prefix, no aliases; matching is case-insensitive (`case_sensitive=False`). `litellm_model` is `LITELLM_MODEL`. |
| Precedence | Process environment, then a `.env` file in the working directory, then the field default. |
| `.env` file | Read into the `Settings` object only. The application does not export it into the process environment, so variables in the *process env* rows below are not set by `.env`. (litellm's own import, in its default `LITELLM_MODE=DEV`, calls `load_dotenv()` and can put `.env` values into the environment; that is litellm's behaviour, not the application's.) |
| Unknown variables | Ignored (`extra="ignore"`). |
| Invalid values | A value outside a field's constraint (for example `LITELLM_TEMPERATURE=3`, `AUTH_MODE=JWT`) raises a validation error at the first `get_settings()` call. On Lambda that is at import time, so the function fails to initialise. |
| Caching | `get_settings()` builds one `Settings` per process and caches it. A change to the environment after the first call is not seen. Direct `os.environ` reads (the *process env* rows) are read at call time. |
| Test mode | When `_MSS_TEST_MODE` is set at the moment `matrix_studio.settings` is imported, no `.env` file is read. |

In the tables, **Setting** is the `Settings` field, or *process env* for a variable read directly with `os.environ`.

## Models and providers

| Env var | Setting | Type | Default | Effect |
|---|---|---|---|---|
| `LITELLM_MODEL` | `litellm_model` | str | `bedrock/global.anthropic.claude-sonnet-5` | The deployment's default model: the fallback for every model role nothing else names (see [Model roles](#model-roles)), the `default` in `GET /api/models`, and the first entry of the selectable list. |
| `LITELLM_TEMPERATURE` | `litellm_temperature` | float, 0.0 to 2.0 | `0.7` | Temperature for persona turns (`voice`), reflections and adaptive pressure. |
| `LITELLM_MAX_TOKENS` | `litellm_max_tokens` | int, >= 1 | `2048` | Output budget for one persona turn. |
| `SUMMARY_MAX_TOKENS` | `summary_max_tokens` | int, >= 1 | `16000` | Output budget for the structured summary: any analysis call that passes no budget of its own. Asides (450 or 1500), the stance classifier (3000) and ensemble position extraction (8000) have their own constants. |
| `AVAILABLE_MODELS` | `available_models` | str, comma-separated | `""` | Extra model strings offered in the UI pickers. See `available_model_list` under [Derived values](#derived-values). |
| `AWS_ACCESS_KEY_ID` | `aws_access_key_id` | str or null | `null` | Read by the application only for `GET /api/health` `readiness.bedrock`. Model and storage calls take credentials from the boto3 / litellm credential chain in the process environment. |
| `AWS_SECRET_ACCESS_KEY` | `aws_secret_access_key` | str or null | `null` | As `AWS_ACCESS_KEY_ID`. |
| `AWS_BEARER_TOKEN_BEDROCK` | *process env* | str | unset | Counted by `GET /api/health` `readiness.bedrock`. Otherwise consumed by the provider SDK, not by the application. |
| `AWS_REGION` | `aws_region`, and *process env* | str | `us-east-1` | The `aws_region` field is defined but read by nothing. The process variable is read directly by the storage layer (`DynamoStorage`, `TenantCredentials`, default `us-east-1` there too) and by every boto3 client that dispatches to a worker (Step Functions, Lambda). Lambda sets it automatically. |
| `OPENAI_API_KEY` | `openai_api_key` | str or null | `null` | Read by the application only for `GET /api/health` `readiness.openai`. litellm reads the process variable itself. |
| `ANTHROPIC_API_KEY` | `anthropic_api_key` | str or null | `null` | Read by the application only for `GET /api/health` `readiness.anthropic`. litellm reads the process variable itself. |

### Model roles

A run makes several kinds of model call. Each is a **role**; `ROLES` in `matrix_studio/models.py` lists ten. A run may override any role in `config.models` (see `run-config.md`); `GET /api/models` returns what each role resolves to with nothing overridden (`roles`), the role names (`overridable_roles`), and the roles with a pinned default (`roles_pinned_by_default`).

`LOW_VARIANCE_MODEL` is `bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0`.

| Role | Calls | Default with no model named | Temperature | Output budget |
|---|---|---|---|---|
| `voice` | Persona turns; consultant (expert) answers | `LITELLM_MODEL` | Turns: `LITELLM_TEMPERATURE`. Consultants: 0.2 | Turns: `LITELLM_MAX_TOKENS`. Consultants: 600 |
| `speaker_selection` | Next-speaker selection; the moderator's dynamic-assumption check | `LOW_VARIANCE_MODEL` (pinned) | Selection: 0.3. Assumption check: 0.2 | None set |
| `validation` | The pre-emit validation gate | `LOW_VARIANCE_MODEL` (pinned) | 0.0 | 50 |
| `reflection` | Cognition reflections | `LITELLM_MODEL` | `LITELLM_TEMPERATURE` | 120 |
| `summary` | The structured run summary. Also the fallback resolution for analysis calls made with no run model: research query planning and tiering, ensemble report extraction, clustering and synthesis | `LITELLM_MODEL` | 0.3 (summary) | `SUMMARY_MAX_TOKENS` |
| `aside` | Aside thread replies (analyst, persona, room) | `LITELLM_MODEL` | Analyst: 0.4. Persona: 0.6 | 450 in the request; 1500 on the aside worker |
| `naming` | Run codename and description | `LOW_VARIANCE_MODEL` (pinned, as reported by `/api/models`) | 0.9 | 60 |
| `wizard` | `POST /api/personas/suggest` cast drafts | `LITELLM_MODEL` | 1.0 | 16000 |
| `pressure` | Adaptive-pressure branch mutation (experimental) | `LITELLM_MODEL` | `LITELLM_TEMPERATURE` | 300 |
| `stance` | Closing-statement stance classifier, run with each summary | `LOW_VARIANCE_MODEL` (pinned) | 0.0 | 3000 |

**Resolution order** (`ModelSet.resolve`), first match wins:

1. `config.models[role]`: an explicit per-role choice. An unknown role name is dropped with a logged warning.
2. `config.model`: the conversation's model, applied to **every** role, including the pinned ones.
3. `ROLE_DEFAULTS[role]`: `LOW_VARIANCE_MODEL` for `validation`, `speaker_selection`, `naming` and `stance`.
4. `LITELLM_MODEL`.

Edge cases:

| Case | Resolution |
|---|---|
| Summary or aside on a branch run (`parent_run_id` set) or an imported run (`config.imported`) | The run's stored `model` and `models` are ignored; the role resolves as if nothing were named (`service.resolve_model`). A `model` in the request body still wins. |
| `POST /api/runs/{ref}/summary` or a thread message with `model` | The body's `model` wins over every rule above, for that call. |
| `naming` | The codename call (`naming.generate_run_name`) does not go through `ModelSet`: it uses the create request's top-level `model`, else `LITELLM_MODEL`. `config.model`, `config.models.naming` and the pinned default are not applied, although `/api/models` reports `naming` as `LOW_VARIANCE_MODEL`. |
| `wizard` | `POST /api/personas/suggest` has no run config: it uses the body's `model`, else `LITELLM_MODEL`. |

Each run logs its resolved role map once at start (`ModelSet.log_plan`).

## Run limits and cost caps

| Env var | Setting | Type | Default | Effect |
|---|---|---|---|---|
| `MAX_MESSAGES` | `max_messages` | int, >= 1 | `20` | Turn budget for a run whose `config.max_messages` is absent. Also the default the cost forecast assumes. |
| `MAX_RUN_COST_USD` | `max_run_cost_usd` | float, >= 0 | `0.0` | Per-run cost cap in USD; `0` means no cap. Checked against the run's accumulated cost after each turn; reaching it ends the run with status `capped`. |
| `COST_WARN_THRESHOLD` | `cost_warn_threshold` | float, >= 0 | `1.0` | Defined, but read by nothing in the backend and not served to the frontend. No effect. |
| `MAX_USER_MONTHLY_COST_USD` | `max_user_monthly_cost_usd` | float, >= 0 | `0.0` | Flat per-user monthly cap in USD; `0` means no cap. Checked before a run or ensemble starts (`402` from the create routes) and between turns. |
| `USER_SPEND_CAPS_JSON` | `user_spend_caps_json` | str (JSON object) | `""` | Per-Cognito-group caps, for example `{"trial": 5, "staff": 100}`. See `monthly_cap_for` under [Derived values](#derived-values). |

## Auth and tenancy

| Env var | Setting | Type | Default | Effect |
|---|---|---|---|---|
| `AUTH_MODE` | `auth_mode` | str: `single-user` or `jwt` | `single-user` | Where the caller's identity comes from when a request carries no verified claims. `single-user`: every such request is the owner `local-single-user`. `jwt`: such a request is `401`. Verified API Gateway claims win in both modes; claims with no `sub` are `401`. Any other value fails validation. Also sets `authRequired` in `GET /config.json` (`false` only for `single-user`). |
| `TENANT_ROLE_ARN` | *process env* | str | `""` | The IAM role assumed per owner with a session policy scoped to that owner's partitions and S3 prefix. Empty: storage uses the process's own credentials, with no per-tenant scoping. |
| `COGNITO_HOSTED_UI_URL` | *process env* | str | `""` | Served as `hostedUiUrl` by `GET /config.json`. |
| `COGNITO_CLIENT_ID` | *process env* | str | `""` | Served as `clientId` by `GET /config.json`. |
| `COGNITO_USER_POOL_ID` | *process env* | str | `""` | Served as `userPoolId` by `GET /config.json`. |
| `USER_POOL_ID` | *process env* | str | unset | Set on the API function by the stack. Not read by the application; read by `scripts/start_conversation.py` to check that `--owner` is a real user. |

On the deployed stack `GET /config.json` is served by CloudFront from the SPA bucket, not by the application, so the three `COGNITO_*` variables are not set there.

## Storage

| Env var | Setting | Type | Default | Effect |
|---|---|---|---|---|
| `TABLE_PREFIX` | *process env* | str | `matrix-studio` | DynamoDB table names are `{TABLE_PREFIX}-{name}` (for example `<prefix>-runs`, `<prefix>-thread-messages`). Also the prefix of per-knowledge-base vector index names, `{TABLE_PREFIX}-kb-{kb_id}`, sanitised to lower-case letters, digits and hyphens and truncated to 63 characters by shortening the prefix. |
| `DATA_BUCKET` | *process env* | str | `""` | S3 bucket for snapshot bodies, document text and avatars. Empty: any body write raises a `StorageError`. |
| `VECTOR_BUCKET` | *process env* | str | `""` | S3 Vectors bucket. Empty: embedding, vector search and knowledge-base index creation are unavailable (a knowledge-base upload answers `502`, `POST /api/runs/{ref}/documents/embed` answers `422`). |
| `VECTOR_INDEX` | *process env* | str | `{TABLE_PREFIX}-chunks` | The shared vector index for run-scoped document chunks. Knowledge bases use their own per-KB index instead. |
| `DATA_DIR` | `data_dir` | str (path) | `./data` | Local directory; only `{DATA_DIR}/blobs` is used, for avatar images not stored in S3. A relative path resolves against the checkout root (the directory holding `pyproject.toml`), or the working directory when installed; an absolute path is used as given (`resolved_data_dir`). |
| `TABLE_<NAME>` | *process env* | str | unset | Set by the stack for every table (`TABLE_RUNS`, `TABLE_EVENTS`, `TABLE_SNAPSHOTS`, `TABLE_SUMMARIES`, `TABLE_THREADS`, `TABLE_THREAD_MESSAGES`, `TABLE_KNOWLEDGE_BASES`, `TABLE_KB_GRANTS`, `TABLE_DOCUMENTS`, `TABLE_CONNECTIONS`). Not read by the application, which derives table names from `TABLE_PREFIX`. |

## Local server

| Env var | Setting | Type | Default | Effect |
|---|---|---|---|---|
| `MATRIX_HOST` | `matrix_host` | str | `127.0.0.1` | Bind address for `matrix-studio serve` when `--host` is not given. The `Dockerfile` sets `0.0.0.0`. |
| `MATRIX_PORT` | `matrix_port` | int, 1 to 65535 | `8000` | Bind port for `matrix-studio serve` when `--port` is not given. |
| `STARTUP_SWEEP` | `startup_sweep` | bool | `true` | At app startup, mark every run still `running` as `interrupted` and append a `sim.interrupted` event to each. The stack sets `false` on every function. |

## Documents and retrieval

| Env var | Setting | Type | Default | Effect |
|---|---|---|---|---|
| `MAX_UPLOAD_BYTES` | `max_upload_bytes` | int, >= 1024 | `10485760` (10 MiB) | Largest file `POST /api/documents/extract` accepts, counted while streaming; over it is `413`. Served by `GET /api/documents/formats`. |
| `MAX_DOCUMENT_CHARS` | `max_document_chars` | int, >= 1000 | `400000` | Largest extracted text from one file (`413` from extract) and largest knowledge-base document text (`422` from `POST /api/knowledge-bases/{kb_id}/documents`). Served by `GET /api/documents/formats`. |

Not configurable by environment: the embedding model default (`bedrock/amazon.titan-embed-text-v2:0`, `embeddings.DEFAULT_EMBEDDING_MODEL`; a run may set `config.retrieval.embedding_model`) and the vector width (`EMBEDDING_DIMENSION = 1024`, in both `matrix_studio/storage/vectors.py` and `infra/matrix_infra/config.py`).

## Research and web search

| Env var | Setting | Type | Default | Effect |
|---|---|---|---|---|
| `SEARCH_PROVIDER` | *process env* | str: `tavily`, `exa` or `brave` | unset | Forces one provider. Its key must be set, or research reports itself unavailable; an unknown name is likewise unavailable. Unset: the first provider with a key, in the order Tavily, Exa, Brave. A run's `config.research.provider` takes precedence. |
| `TAVILY_API_KEY` | *process env* | str | unset | Tavily search key (returns page text). |
| `EXA_API_KEY` | *process env* | str | unset | Exa search key (returns page text). |
| `BRAVE_API_KEY` | *process env* | str | unset | Brave search key (returns snippets, so pages are fetched separately). |
| `SEARCH_SECRET_ARN` | *process env* | str | unset | A Secrets Manager secret holding a JSON object keyed by the three key variable names. Loaded once per process, at the first provider selection. Only those three names are exported; a key already in the environment is not overwritten; an unreadable secret is logged and research reports itself unavailable. Set on the research function only, and only when the `search_secret_arn` context is given. |

With no provider key at all, a run that asks for research records research status `unavailable` and holds the conversation without it.

## Avatars

| Env var | Setting | Type | Default | Effect |
|---|---|---|---|---|
| `ENABLE_AVATARS` | `enable_avatars` | bool | `true` | The default for a run's `config.generate_avatars` when that is absent, and what the cost forecast assumes. Off: no portrait is generated, even for a run with `generate_avatars: true`, and `POST /api/runs/{ref}/agents/{name}/regenerate-avatar` answers `502`. |
| `AVATAR_MODEL_ID` | `avatar_model_id` | str | `stability.sd3-5-large-v1:0` | Bedrock image model. |
| `AVATAR_REGION` | `avatar_region` | str | `us-west-2` | Region of the Bedrock runtime client used for images. |
| `AVATAR_ASPECT_RATIO` | `avatar_aspect_ratio` | str | `1:1` | Sent as `aspect_ratio`. |
| `AVATAR_STYLE` | `avatar_style` | str: `illustration`, `anime`, `3d` | `anime` | Style clause of the portrait prompt. Matched case-insensitively; an unknown value uses `illustration`. |
| `AVATAR_COST_USD` | *process env* | float | `0.08` | Price charged per generated portrait in the run's cost and in the forecast. Negative values clamp to 0; a non-number uses 0.08. |

## Workers and orchestration

| Env var | Setting | Type | Default | Effect |
|---|---|---|---|---|
| `TURN_LOOP_ARN` | *process env* | str | `""` | Step Functions state machine that executes runs. Set: creating, resuming or branching a run starts an execution. Empty: the run executes in a background task in the API process (the local case). |
| `RESEARCH_FUNCTION` | *process env* | str | `""` | Research worker for ensembles. Set: `POST /api/ensembles` with research on dispatches the pass asynchronously and returns. Empty: the pass runs in the API process. |
| `ENSEMBLE_REPORT_FUNCTION` | *process env* | str | `""` | Ensemble report worker. Set: the last member's finalise and `POST /api/ensembles/{id}/report` dispatch it (the route answers `202`). Empty: the report is built inline. |
| `ASIDE_FUNCTION` | *process env* | str | `""` | Aside worker. Set: `POST /api/threads/{id}/messages` and `POST /api/runs/{ref}/summary` dispatch generation and answer `202`. Empty: the reply or summary is generated in the request. |
| `TURN_BUDGET` | *process env* | int | `1` | Turns generated per Turn-state invocation. A `turn_budget` in the state input wins. Read by the turn worker only. |
| `ENSEMBLE_STAGGER_SECONDS` | *process env* | float | `1.0` | Delay between starting one ensemble member and the next. Read at each fan-out. Negative clamps to 0; a non-number logs a warning and uses 1.0; `0` when `_MSS_TEST_MODE` is set. |

`STARTUP_SWEEP` is listed under [Local server](#local-server).

## Experimental and validation flags

| Env var | Setting | Type | Default | Effect |
|---|---|---|---|---|
| `VALIDATION_ENABLED` | `validation_enabled` | bool | `true` | The pre-emit validation gate. Off: no validation calls and no `validation.*` events. |
| `VALIDATION_RETRY_BUDGET` | `validation_retry_budget` | int, >= 0 | `1` | Regenerations of a rejected turn before it is emitted with a `validation.flagged` event. |
| `ADAPTIVE_PRESSURE_ENABLED` | `adaptive_pressure_enabled` | bool | `false` | Allows the `adaptive_pressure` branch mutation. Off: a branch request with it is `422`. |
| `STRUCTURED_OUTPUT` | `structured_output` | bool | `false` | Enables `GET /api/runs/{ref}/turns/{turn}/structured` for every request. Off: that route is `403` unless the request passes `?opt_in=true`. |

## Logging

| Env var | Setting | Type | Default | Effect |
|---|---|---|---|---|
| `LOG_LEVEL` | *process env* | str (Python level name) | `INFO` | Root log level, set at import by `matrix_studio/api/lambda_handler.py` and `matrix_studio/step_handlers.py`. The CLI ignores it and uses `-v` (DEBUG) or INFO. |

## Test only

| Env var | Setting | Type | Default | Effect |
|---|---|---|---|---|
| `_MSS_TEST_MODE` | *process env* | any non-empty value | unset | No `.env` file is read (checked once, when `matrix_studio.settings` is imported), and the ensemble stagger is 0 (checked at each fan-out). Set by the test fixtures. |

## Derived values

| Name | Defined on | Value |
|---|---|---|
| `available_model_list` | `Settings` property | `LITELLM_MODEL` first, then each comma-separated entry of `AVAILABLE_MODELS`, trimmed, empty entries and duplicates dropped. Served by `GET /api/models` as `models`, each with a display `label`. |
| `monthly_cap_for(groups)` | `Settings` method | The cap that applies to a user in `groups`; `0.0` means none. With no `USER_SPEND_CAPS_JSON` or no groups: `MAX_USER_MONTHLY_COST_USD`. Otherwise the caps of the user's groups that appear in the JSON with a numeric value: none listed gives `MAX_USER_MONTHLY_COST_USD`; any listed cap `<= 0` gives `0.0` (no cap); else the **highest**. Malformed JSON or a non-object logs a warning and gives `MAX_USER_MONTHLY_COST_USD`. |
| `resolved_data_dir` | `Settings` property | `DATA_DIR` as an absolute path (see [Storage](#storage)). |

Example: with `MAX_USER_MONTHLY_COST_USD=10` and `USER_SPEND_CAPS_JSON={"trial": 5, "staff": 100}`, a user in `trial` and `staff` has a cap of 100, a user in `trial` only has 5, and a user in neither has 10.

## Deployed stack environment

What `infra/matrix_infra/stack.py` sets on each Lambda function. `<prefix>` is the `prefix` context value. Resource names and ARNs are placeholders. `LITELLM_MODEL`, `AVAILABLE_MODELS` and every other `Settings` field not listed here are not set, so their code defaults apply. Lambda sets `AWS_REGION` itself.

| Function | Handler | Timeout | Memory |
|---|---|---|---|
| `<prefix>-api` | `matrix_studio.api.lambda_handler.handler` | 30 s | 2048 MB |
| `<prefix>-research` | `matrix_studio.step_handlers.research` | 15 min | 2048 MB |
| `<prefix>-prepare` | `matrix_studio.step_handlers.prepare` | 10 min | 2048 MB |
| `<prefix>-turn` | `matrix_studio.step_handlers.turn` | 5 min | 2048 MB |
| `<prefix>-finalise` | `matrix_studio.step_handlers.finalise` | 15 min | 2048 MB |
| `<prefix>-ensemble-report` | `matrix_studio.step_handlers.ensemble_report` | 15 min | 2048 MB |
| `<prefix>-aside` | `matrix_studio.step_handlers.aside` | 15 min | 2048 MB |

| Variable | API | Workers (all six) | Value |
|---|---|---|---|
| `AUTH_MODE` | yes | no | `jwt` |
| `STARTUP_SWEEP` | yes | yes | `false` |
| `DATA_DIR` | yes | yes | `/tmp/data` (also the `Dockerfile.lambda` default) |
| `DATA_BUCKET` | yes | yes | `<data-bucket>` |
| `VECTOR_BUCKET` | yes | yes | `<vector-bucket>` |
| `VECTOR_INDEX` | yes | yes | `<prefix>-chunks` |
| `TABLE_PREFIX` | yes | yes | `<prefix>` |
| `TENANT_ROLE_ARN` | yes | yes | `arn:aws:iam::<account>:role/<prefix>-tenant` |
| `TABLE_<NAME>` | yes | yes | `<prefix>-<table>` for each table |
| `MAX_USER_MONTHLY_COST_USD` | yes | yes | the `user_monthly_cap_usd` context, as a string (`0.0` by default) |
| `USER_SPEND_CAPS_JSON` | yes | yes | the `user_spend_caps_json` context; only when given |
| `USER_POOL_ID` | yes | no | `<user-pool-id>` |
| `TURN_LOOP_ARN` | yes | research only | `arn:aws:states:<region>:<account>:stateMachine:<prefix>-turn-loop` |
| `RESEARCH_FUNCTION` | yes | no | `<prefix>-research` |
| `ENSEMBLE_REPORT_FUNCTION` | yes | finalise only | `<prefix>-ensemble-report` |
| `ASIDE_FUNCTION` | yes | no | `<prefix>-aside` |
| `SEARCH_SECRET_ARN` | no | research only | the `search_secret_arn` context; only when given |

The local `Dockerfile` sets `DATA_DIR=/app/data`, `MATRIX_HOST=0.0.0.0` and `MATRIX_PORT=8000`.

## CDK stack configuration

`StackConfig.from_context` reads these from CDK context (`infra/cdk.json` `context`, or `-c key=value`). The stack is named `<prefix>-stack`; its account comes from `CDK_DEFAULT_ACCOUNT` and its region from `region`, else `CDK_DEFAULT_REGION`.

| Context key | Type | Default | Effect |
|---|---|---|---|
| `prefix` | str | `matrix-studio` (also set in `cdk.json`) | Prefix of every resource name, so two deployments can share an account. Becomes `TABLE_PREFIX`. |
| `region` | str or null | `null` | Region for the stack; null uses `CDK_DEFAULT_REGION`. |
| `retain_data` | bool | `true` (also set in `cdk.json`) | Whether tables and buckets survive `cdk destroy`. A string value is false when it is `false`, `0`, `no` or `off` (case-insensitive). |
| `user_monthly_cap_usd` | float | `0.0` | Becomes `MAX_USER_MONTHLY_COST_USD` on the API and every worker. |
| `user_spend_caps_json` | str or null | `null` | Becomes `USER_SPEND_CAPS_JSON` on the API and every worker when set. |
| `extra_callback_urls` | list of str, or a comma-separated str | `[]` | Extra OAuth callback URLs, and the only origins the HTTP API's CORS rule allows (methods GET, POST, DELETE, OPTIONS). |
| `admin_email` | str or null | `null` | When set, creates a Cognito user with this email and adds it to the `admins` group. |
| `verify_principal_arn` | str or null | `null` | An extra principal the tenant role trusts, for `scripts/verify_tenant_isolation.py`. |
| `search_secret_arn` | str or null | `null` | Becomes `SEARCH_SECRET_ARN` on the research function, with `secretsmanager:GetSecretValue` on that secret only. Unset: research is unavailable on the deployment. |

Example:

```bash
cd infra
npx cdk deploy -c prefix=demo-studio -c user_monthly_cap_usd=25 \
  -c user_spend_caps_json='{"trial": 5}' -c retain_data=false
```
