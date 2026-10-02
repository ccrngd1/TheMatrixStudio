# How to verify a deployment

Run the deployment checks under `scripts/` against a deployed stack and read the scoreboard.

**Deployed system.** The checks run against real AWS services. They create test runs, collections, Cognito users and Step Functions executions. Most clean up after themselves; `turn-loop` leaves its runs under the owner `phase5-verify` (remove them with [How to delete runs](delete-runs.md)) and `kb-grants` keeps one test collection that it reuses. The cap step of `turn-loop` temporarily changes the turn worker's configuration and puts it back. Two checks make Bedrock calls; a full run costs about $0.10.

## Prerequisites

- A deployed stack ([How to deploy Matrix Studio to AWS](deploy-to-aws.md)) with the default prefix `matrix-studio`. Two checks look for resources by that name: `tenant-isolation` (the stack `matrix-studio-stack`) and the cap step of `turn-loop` (the function `matrix-studio-turn`).
- Administrator-level credentials for the account. Between them the checks read and write DynamoDB, S3 and S3 Vectors, create and delete Cognito users, start Step Functions executions, read and update a Lambda function's configuration, and read Bedrock logging settings.
- The Python environment with the `dev` extra (`pip install -e ".[dev]"`), which includes the Cognito sign-in library one check needs.

## Steps

1. From the repository root, set the environment from the stack outputs:

   ```bash
   export AWS_REGION=<region>
   out() { aws cloudformation describe-stacks --stack-name matrix-studio-stack \
     --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
   export TABLE_PREFIX=matrix-studio
   export DATA_BUCKET=$(out DataBucketName)
   export VECTOR_BUCKET=$(out VectorBucketName)
   ```

   The suite refuses to start if any of the last three is missing. The turn-loop check finds the state machine whose name ends in `turn-loop`; set `TURN_LOOP_ARN` to choose one yourself.

2. Run every check:

   ```bash
   python scripts/verify_deployment.py
   ```

3. Read the scoreboard at the end. Each check is one of:

   | Verdict | Meaning |
   |---|---|
   | `PASS` | the check ran and passed |
   | `FAIL` / `ERROR` | it failed, or could not run; the lines above the scoreboard say why |
   | `NEEDS-SETUP` | it needs a temporary change to the stack first (only `tenant-isolation`) |

   A check that cannot run is reported as a failure, not skipped.

4. To run only some checks, name them (repeat `--only` for more than one):

   ```bash
   python scripts/verify_deployment.py --only kb-grants --only vector-retrieval
   ```

   The checks are `bedrock-logging`, `kb-grants`, `vector-retrieval`, `tenant-isolation`, `turn-loop` and `kb-retrieval`. `turn-loop` and `kb-retrieval` are the billable ones. `--timeout` sets the limit per check in seconds (default 1800).

## Run the tenant-isolation check

`tenant-isolation` reports `NEEDS-SETUP` until the tenant role temporarily trusts your own role. Opening that trust widens access to every user's data while it is open, so close it straight after.

1. Find the ARN of the IAM role your credentials use (`aws sts get-caller-identity` shows it as an assumed-role ARN; the role ARN is `arn:aws:iam::<account-id>:role/<role-name>`).

2. Deploy with the extra trust, plus every flag you normally deploy with:

   ```bash
   cd infra
   npx cdk deploy -c verify_principal_arn=arn:aws:iam::<account-id>:role/<role-name> \
     -c admin_email=you@example.com      # and your other usual flags
   ```

3. Run the check:

   ```bash
   cd .. && python scripts/verify_deployment.py --only tenant-isolation
   ```

4. Deploy again without `verify_principal_arn`, keeping your usual flags.

5. Run `--only tenant-isolation` once more. It must report `NEEDS-SETUP` again; that is how you confirm the trust is closed.

## Related

- What each check asserts: [reference](../reference/)
- Why the suite treats a skipped check as a failure: [explanation](../explanation/)
