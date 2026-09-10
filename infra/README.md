# Infrastructure (Phase 1)

CDK app for the AWS deployment. See `docs/AWS-SERVERLESS-ARCHITECTURE.md` for the
design and `docs/AWS-IMPLEMENTATION-PLAN.md` for the phase ordering.

**Status: deployed** to account 791580863750, us-east-1, on 2026-09-10. All four
acceptance checks pass, including a live Bedrock call from the Lambda.

**What Phase 1 gives you:** a Cognito login, the SPA on CloudFront, and an
authenticated API that answers `/api/health`. **Nothing is wired to storage yet, and
creating a run silently loses it** — see
[Known state](#known-state--read-this-before-demoing) below. Read that before
showing this to anyone.

## What this creates

| | |
|---|---|
| **Cognito** | user pool (native users, **self-signup off**), Hosted UI domain, public SPA client (no secret → PKCE), `admins` group, optionally one admin user |
| **DynamoDB** | 10 tables per §4, on-demand, encrypted, PITR on; `runs` has both GSIs |
| **S3** | one data bucket (per-user prefixes, versioned) + one private SPA bucket |
| **S3 Vectors** | one vector bucket, one probe index — 1024-dim, cosine, `text` non-filterable |
| **API Gateway** | HTTP API, JWT authorizer on **every** route, CORS pinned to the CloudFront origin |
| **Lambda** | container image (791 MB measured), Bedrock-only IAM |
| **CloudFront** | SPA distribution, `index.html` no-store + `/assets/*` immutable (cache policy **and** response header), 403/404 → `/index.html` |

## Prerequisites

- **Node 20+** recommended for the CDK CLI. Verified working on Node 18 — `cdk
  synth` succeeds and only warns — but 18 has been end-of-life since 2025-11-30 and
  is unsupported, so a future CLI release may stop at the warning.
- **Docker running** — the Lambda is a container image, built during `cdk deploy`.
  Verified: the image builds from the CDK-staged context and is **791 MB**, which
  is comfortably under the 10 GB container limit and roughly 3× over the 250 MB
  unzipped limit a zip-packaged Lambda would have to fit. That measurement is why
  it is a container image.
- **Valid AWS credentials** with permission to create all of the above.
- **A bootstrapped account/region**: `cdk bootstrap aws://ACCOUNT/REGION`.

## Setup

```bash
cd infra
uv venv .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
npm install -g aws-cdk        # or: npx aws-cdk <command>
```

## Deploy

```bash
cd infra
cdk deploy -c admin_email=you@example.com
```

Context flags:

| Flag | Default | Effect |
|---|---|---|
| `admin_email` | *none* | Creates the first user and emails a temporary password. Without it the pool has no users and you must create one in the console. |
| `prefix` | `matrix-studio` | Resource name prefix, so two deployments can share an account. |
| `retain_data` | `true` | Tables and the data bucket survive `cdk destroy`. Set `false` **deliberately** for a throwaway demo. |
| `extra_callback_urls` | *none* | Extra OAuth redirect URLs, comma-separated (e.g. `http://localhost:5173`). Also added to the API's CORS allowlist. |
| `region` | CLI default | Region for everything except the Stability image model, which is pinned to `us-west-2`. Must be bootstrapped. |

Then deploy the frontend:

```bash
cd frontend && npm ci && npm run build
aws s3 sync ../matrix_studio/static "s3://$(
  aws cloudformation describe-stacks --stack-name matrix-studio-stack \
    --query "Stacks[0].Outputs[?OutputKey=='SpaBucketName'].OutputValue" --output text
)" --delete
```

You do **not** need a CloudFront invalidation for the hashed assets — that is the
point of the two cache behaviours — but `index.html` is served no-cache, so a new
build is picked up on the next request.

## Verify the phase is done

The plan's acceptance criteria, in order:

```bash
# 1. Log in. Self-signup is disabled, so use the admin user (or create one).
open "$(aws cloudformation describe-stacks --stack-name matrix-studio-stack \
  --query "Stacks[0].Outputs[?OutputKey=='HostedUiUrl'].OutputValue" --output text)"

# 2. The SPA loads from CloudFront.
open "$(... OutputKey=='SpaUrl' ...)"

# 3. Unauthenticated /api/health is 401 — API Gateway refuses it before Lambda.
API=$(... OutputKey=='ApiUrl' ...)
curl -s -o /dev/null -w '%{http_code}\n' "$API/api/health"          # expect 401

# 4. Authenticated /api/health is 200.
curl -s -H "Authorization: Bearer $ID_TOKEN" "$API/api/health"      # expect 200 {"status":"ok"}
```

`$ID_TOKEN` is the `id_token` from the Hosted UI's code exchange. It must be the
**id** token, not the access token: the authorizer's audience is the app client id,
which is the `aud` claim on the id token — an access token carries `client_id`
instead and will be rejected.

To create a user by hand:

```bash
aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" \
  --username someone@example.com \
  --user-attributes Name=email,Value=someone@example.com Name=email_verified,Value=true
```

## Known state — read this before demoing

Phase 1 is infrastructure only. Two consequences are visible in the UI, and the
first one looks like a working feature:

1. **Creating a run appears to work and then loses it.** `POST /api/runs` returns
   **201 with a real LLM-generated codename** — and the run then never appears in
   `GET /api/runs`, and `GET /api/runs/{name}` is 404.

   Two independent causes, both phase boundaries rather than misconfigurations:

   - **Lambda freezes the execution environment when the handler returns**, so the
     `asyncio` background task that runs the conversation never gets scheduled. The
     logs show one line — `Starting simulation …` — and an **8 ms billed duration**.
     The run row is never written. This is why **Phase 4 of the plan was cancelled**
     and Step Functions (Phase 5) is a prerequisite for a run to execute at all, not
     an optimisation.
   - **Storage is SQLite in `/tmp`**, which is per-sandbox and ephemeral. Two
     concurrent requests see two different databases and neither survives the
     sandbox. **Phase 2** replaces it with DynamoDB and S3.

   The tables above exist and are empty. Everything up to and including the Bedrock
   call works.
2. **`STARTUP_SWEEP=false`** is set on the function, and it has to be. The sweep
   assumes it is the only process; with concurrent sandboxes, two cold starts would
   each mark the other's in-flight run as interrupted.

The Lambda's IAM role grants **Bedrock only** — no DynamoDB, no S3 Vectors. That is
deliberate (§3): storage access arrives in Phase 2 as per-request STS credentials
scoped with `dynamodb:LeadingKeys`, so a missing tenant filter in application code
*cannot* leak data. Granting the function broad rights now would make that boundary
optional without anything failing.

## Tests

```bash
cd infra && .venv/bin/python -m pytest tests/ -q
```

41 assertions over the synthesized template — no AWS account, no credentials, a few
seconds. They deliberately do not restate the stack; each one covers a property
whose absence is a **working deployment with a real defect**: `index.html` cached
(silent blank page), a JWT authorizer with no audience (accepts any client's
tokens), self-signup left on (open registration), passage text left filterable
(breaks one-round-trip retrieval, and unfixable after index creation).

## Teardown

```bash
cdk destroy
```

With the default `retain_data=true` this leaves the tables, the data bucket and the
user pool behind — orphaned, and costing almost nothing at rest. Delete them by hand
once you are sure, or deploy with `-c retain_data=false` from the start if the data
is disposable.
