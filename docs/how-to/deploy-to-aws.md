# How to deploy Matrix Studio to AWS

Deploy or update the whole stack (Cognito, API Gateway, Lambda, Step Functions, DynamoDB, S3, S3 Vectors and CloudFront) and publish the web app.

**Deployed system.** Every step here creates or changes resources in your AWS account.

## Prerequisites

- Python 3.11 or later, Node 18 or later (Node 20+ recommended for the CDK CLI), and Docker running. The Lambda functions are built as a container image during `cdk deploy`.
- The AWS CDK CLI: `npm install -g aws-cdk`.
- AWS credentials for the target account that may create IAM roles, Lambda functions, Step Functions state machines, DynamoDB tables, S3 and S3 Vectors buckets, a Cognito user pool, an HTTP API and a CloudFront distribution. Deploying is an administrator-level action.
- Bedrock model access in the target region for the models the app uses (Claude Sonnet 5 and Haiku 4.5 by default, Titan Text Embeddings v2), and for Stability SD3.5 Large in `us-west-2` if you want avatars.
- The account and region bootstrapped for CDK (step 3 does it once).
- Optional: a Secrets Manager secret holding web-search keys, if you want pre-conversation research. See [How to research a subject before a run](research-before-a-run.md).

## Steps

1. Set the region for every command that follows:

   ```bash
   export AWS_REGION=<region>            # for example us-east-1
   ```

2. Build the web app. From the repository root:

   ```bash
   cd frontend && npm ci && npm run build && cd ..
   ```

   The build writes to `matrix_studio/static/`, including `index.html` and `theatre.html`.

3. Create the CDK virtual environment and bootstrap the account (bootstrap is needed once per account and region):

   ```bash
   cd infra
   python -m venv .venv && .venv/bin/pip install -r requirements.txt
   npx cdk bootstrap aws://<account-id>/$AWS_REGION
   ```

   The venv must be at `infra/.venv`: `cdk.json` runs `.venv/bin/python app.py`.

4. Deploy the stack:

   ```bash
   npx cdk deploy -c admin_email=you@example.com
   ```

   Add any other context flags you need, each as `-c key=value`:

   | Flag | Use it to |
   |---|---|
   | `admin_email` | create the first user; Cognito emails a temporary password |
   | `prefix` | name resources differently (default `matrix-studio`; the stack is then `<prefix>-stack`) |
   | `region` | deploy to a region other than the CLI default |
   | `retain_data` | `false` only for a throwaway stack; tables and the data bucket are kept on `cdk destroy` by default |
   | `extra_callback_urls` | allow extra sign-in redirect URLs, comma-separated |
   | `user_monthly_cap_usd`, `user_spend_caps_json` | set monthly spend caps (see [How to configure models and spend caps](configure-models-and-spend-caps.md)) |
   | `search_secret_arn` | turn on web research |

   **Pass the same flags on every later deploy.** A flag you leave out goes back to its default. In particular, deploying without `admin_email` removes the admin user that an earlier deploy created.

5. Define a helper that reads a stack output. Use your stack name if you changed `prefix`:

   ```bash
   out() { aws cloudformation describe-stacks --stack-name matrix-studio-stack \
     --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
   ```

6. Go back to the repository root and upload the web app:

   ```bash
   cd ..
   aws s3 sync matrix_studio/static "s3://$(out SpaBucketName)" --delete --exclude config.json
   ```

   Keep `--exclude config.json`. `cdk deploy` writes `config.json` into the same bucket, and `--delete` would remove it; the app then shows a "Configuration error" page instead of the sign-in.

7. Invalidate the CloudFront cache:

   ```bash
   DIST=$(aws cloudformation describe-stack-resources --stack-name matrix-studio-stack \
     --query "StackResources[?ResourceType=='AWS::CloudFront::Distribution'].PhysicalResourceId" \
     --output text)
   aws cloudfront create-invalidation --distribution-id "$DIST" --paths "/*"
   ```

   Most files are either uncached or content-hashed, but the theatre's sprite sheets under `/theatre/` are cached for a day, so invalidate after any frontend change.

8. Open the `SpaUrl` output in a browser and sign in with the emailed temporary password. Cognito asks you to set a new one.

## Check it worked

- The app loads at `SpaUrl` and shows the Runs screen after sign-in.
- An unauthenticated API call is refused:

  ```bash
  curl -s -o /dev/null -w '%{http_code}\n' "$(out SpaUrl)/api/health"     # expect 401
  ```

- For a full check, run the verification suite: [How to verify a deployment](verify-a-deployment.md).

## Add another user

Self-signup is off. Create users yourself (needs `cognito-idp:AdminCreateUser`):

```bash
aws cognito-idp admin-create-user --user-pool-id "$(out UserPoolId)" \
  --username someone@example.com \
  --user-attributes Name=email,Value=someone@example.com Name=email_verified,Value=true
```

## Deploy a change later

1. Rebuild the web app if the frontend changed (step 2).
2. Run `npx cdk deploy` from `infra/` with the same flags as before (step 4). This rebuilds the container image if the Python code changed.
3. Sync and invalidate (steps 6 and 7).

## Related

- Every context flag and output: [reference](../reference/)
- Why the stack is shaped this way: [explanation](../explanation/)
