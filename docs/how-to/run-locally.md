# How to run Matrix Studio locally

Run the API and web app on your own machine, for development or debugging.

There is no laptop-only storage: the local server reads and writes the DynamoDB tables, S3 bucket and S3 Vectors bucket of a deployed stack. Model calls go to your configured provider and cost money.

## Prerequisites

- A deployed stack. See [How to deploy Matrix Studio to AWS](deploy-to-aws.md).
- Python 3.11 or later; Node 18 or later for the web app.
- AWS credentials that can read and write that stack's tables and buckets directly, and call Bedrock. The local server uses these credentials as they are; it does not assume the per-user tenant role.
- Model credentials (Bedrock by default; see [How to configure models and spend caps](configure-models-and-spend-caps.md) for other providers).

Before you start, know two consequences:

- Everything you create locally belongs to one fixed owner, `local-single-user`. Those runs do not appear for Cognito users of the deployed app, and theirs do not appear locally.
- The server's start-up sweep marks every run with status `running` in the tables as `interrupted`, across all users. Against a deployed stack that would cut off live runs, so the steps below turn it off.

## Steps

1. Install the package from the repository root. The `documents` extra adds PDF and Word reading:

   ```bash
   python -m venv .venv
   .venv/bin/pip install -e ".[dev,documents]"
   ```

2. Read the storage names from the stack outputs:

   ```bash
   export AWS_REGION=<region>
   out() { aws cloudformation describe-stacks --stack-name matrix-studio-stack \
     --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
   export TABLE_PREFIX=matrix-studio
   export DATA_BUCKET=$(out DataBucketName) VECTOR_BUCKET=$(out VectorBucketName)
   ```

   Set these as real environment variables. A `.env` file is read only for the settings in `matrix_studio/settings.py` (models, caps, avatars); the storage names are read from the environment.

3. Turn off the start-up sweep and authentication, then start the server:

   ```bash
   export STARTUP_SWEEP=false AUTH_MODE=single-user
   .venv/bin/matrix-studio serve            # http://127.0.0.1:8000
   ```

   Use `--host` and `--port` to change where it listens.

4. Open `http://127.0.0.1:8000` if you built the web app into `matrix_studio/static/` (`cd frontend && npm run build`).

5. For frontend work with hot reload, start the Vite dev server in a second terminal instead:

   ```bash
   cd frontend && npm ci && npm run dev
   ```

   Open the URL it prints (port 5173 by default). It forwards `/api` and `/config.json` to the server on port 8000.

## Check it worked

```bash
curl -s http://127.0.0.1:8000/config.json     # {"...","authRequired":false}
curl -s http://127.0.0.1:8000/api/health      # {"status":"ok",...}
curl -s http://127.0.0.1:8000/api/models      # the default model and each role's model
```

The web app opens on the Runs screen with no sign-in. The server log names the tables it is using; a warning that the runs table does not exist means `TABLE_PREFIX` or `AWS_REGION` is wrong.

Runs started from the local server execute in the server process, not in Step Functions. Press **Stop** on any live run before you shut the server down: a run cut off by the process exiting stays marked `running` (the list shows it as Stalled), and with the sweep off nothing marks it resumable. A stopped run can be resumed later.

## Run one conversation without the web app

To run a definition file from the terminal and print the result, with nothing stored:

```bash
.venv/bin/matrix-studio run examples/minimal.json --no-db -o result.json
```

`--max-messages N` overrides the turn count. Without `--no-db` the run is stored under `local-single-user` like any other local run.

## Related

- Settings and their defaults: [reference](../reference/)
- Why there is no local-only mode: [explanation](../explanation/)
