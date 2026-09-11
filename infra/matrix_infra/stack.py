# SPDX-License-Identifier: Apache-2.0
"""
Phase 1: the infrastructure skeleton. Everything stood up, nothing wired.

Deliberately one stack. Cross-stack references in CDK create export/import
dependencies that make a resource impossible to rename or move without a two-step
deploy, and at this size the legibility of "one file, one `cdk deploy`" is worth
more than separation that nothing yet needs.

What this stack does NOT do, and why that is the point of the phase: no route
except `/api/health` is wired, no table is read, no vector is written. Phase 1
exists to surface the boring blockers — container image size, IAM policy shape,
the `s3vectors` API — while there is nothing else to blame for a failure.

See `docs/AWS-SERVERLESS-ARCHITECTURE.md` for the design and
`docs/AWS-IMPLEMENTATION-PLAN.md` for the ordering.
"""

from typing import Dict, List, Optional

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as apigw_authorizers
from aws_cdk import aws_apigatewayv2_integrations as apigw_integrations
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3vectors as s3vectors
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as sfn_tasks
from constructs import Construct

from matrix_infra.config import (
    EMBEDDING_DIMENSION,
    NON_FILTERABLE_METADATA_KEYS,
    StackConfig,
)

# Repository root, relative to this file — the Docker build context for the
# Lambda image.
PROJECT_ROOT = "../"


class MatrixStudioStack(Stack):
    """Cognito · DynamoDB · S3 · S3 Vectors · HTTP API · Lambda · CloudFront."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: Optional[StackConfig] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config or StackConfig()

        # Order matters in exactly one place: the Cognito app client's callback URL
        # needs CloudFront's domain name, so the distribution is created first.
        # CloudFront never references Cognito, so there is no cycle.
        self._create_data_stores()
        self._create_vector_store()
        self._create_spa_hosting()
        self._create_user_pool()
        self._create_api()
        self._create_turn_loop()
        self._outputs()

    # ------------------------------------------------------------------ #
    # DynamoDB and S3
    # ------------------------------------------------------------------ #

    @property
    def _removal_policy(self) -> RemovalPolicy:
        return (
            RemovalPolicy.RETAIN
            if self.config.retain_data
            else RemovalPolicy.DESTROY
        )

    def _table(
        self,
        name: str,
        partition_key: str,
        sort_key: Optional[str] = None,
    ) -> dynamodb.Table:
        """One table, on-demand, encrypted, with point-in-time recovery.

        **On-demand billing** because the design's stated property is that nothing
        has an hourly capacity floor: a demo that nobody uses for a week should
        cost nothing, and provisioned capacity would bill continuously for idle
        tables. The trade is a higher per-request price at sustained high volume,
        which is the right way round for this workload — conversation runs are
        bursty by nature.

        **Point-in-time recovery on** by default. The event log is the source of
        truth for every run, and it is reconstructible from nothing else.
        """
        return dynamodb.Table(
            self,
            # Left as it was, deliberately. Tidying this to strip hyphens too —
            # "Knowledge-BasesTable" -> "KnowledgeBasesTable" — looks like an
            # improvement and is a deployment failure: a changed logical id makes
            # CloudFormation treat the resource as NEW, so it tries to create a table
            # whose `TableName` the orphaned old one still holds, and
            # `AWS::EarlyValidation::ResourceExistenceCheck` refuses the whole change
            # set. CDK already sanitises the hyphen for the physical id, so there was
            # nothing wrong to fix.
            f"{name.title().replace('_', '')}Table",
            table_name=f"{self.config.prefix}-{name}",
            partition_key=dynamodb.Attribute(
                name=partition_key, type=dynamodb.AttributeType.STRING
            ),
            sort_key=(
                dynamodb.Attribute(name=sort_key, type=dynamodb.AttributeType.STRING)
                if sort_key
                else None
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=(
                dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True
                )
            ),
            removal_policy=self._removal_policy,
        )

    def _create_data_stores(self) -> None:
        """The nine tables of §4, and the one S3 bucket behind them.

        Keys are `pk`/`sk` rather than domain names like `owner_sub`/`run_id`,
        because the *values* carry the type (`USER#{sub}`, `RUN#{id}#{seq}`) and
        several tables partition on something other than a user. Generic attribute
        names keep the table definitions honest about that.
        """
        # `events` and `snapshots` are USER-partitioned rather than run-partitioned.
        # Run-partitioning is the more obvious shape and was the earlier draft, but
        # it is the one table pair `dynamodb:LeadingKeys` could not reach — so
        # isolation would rest on the API checking run ownership first, i.e. on a
        # code path instead of a credential. A single run's events stay contiguous
        # inside the user's partition because the sort key leads with the run id.
        self.tables: Dict[str, dynamodb.Table] = {
            "runs": self._table("runs", "pk", "sk"),
            "events": self._table("events", "pk", "sk"),
            "snapshots": self._table("snapshots", "pk", "sk"),
            "summaries": self._table("summaries", "pk", "sk"),
            "threads": self._table("threads", "pk", "sk"),
            # HYPHENATED, like every other multi-word table here. The dict key stays
            # underscored because it becomes the `TABLE_THREAD_MESSAGES` environment
            # variable, and a hyphen is not valid in one.
            #
            # It was `thread_messages` and that shipped: the real table was
            # `matrix-studio-thread_messages` while the storage layer asked for
            # `matrix-studio-thread-messages`, so every thread-message operation would
            # have failed with ResourceNotFoundException in production. It passed
            # locally because the moto fixture created the hyphen form — the fixture
            # and the stack had diverged, which is the exact failure
            # `test_table_names_match_the_storage_layer` now guards.
            "thread_messages": self._table("thread-messages", "pk", "sk"),
            # Not under a user partition on purpose: a shared KB is read by
            # principals who do not own it (§8b), so a `USER#{sub}` prefix would
            # make sharing impossible to express. Authorisation here is the
            # `kb_grants` check, re-run at QUERY time rather than only at binding
            # time — otherwise revoking a grant would not take effect until
            # somebody happened to re-bind.
            "knowledge_bases": self._table("knowledge-bases", "pk", "sk"),
            "kb_grants": self._table("kb-grants", "pk", "sk"),
            "documents": self._table("documents", "pk", "sk"),
        }

        # `connections` exists for the WebSocket fan-out that §5.1 marks as v2, and
        # is created now only because its TTL attribute has to be declared at table
        # creation. TTL cleans up connection rows that `$disconnect` never reached,
        # which happens whenever a client vanishes rather than closing.
        self.tables["connections"] = dynamodb.Table(
            self,
            "ConnectionsTable",
            table_name=f"{self.config.prefix}-connections",
            partition_key=dynamodb.Attribute(
                name="pk", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="sk", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            time_to_live_attribute="expires_at",
            removal_policy=self._removal_policy,
        )

        # The two GSIs §4 calls for, both driven by screens that already exist:
        # the history list, and finding runs left in a non-terminal state.
        self.tables["runs"].add_global_secondary_index(
            index_name="by-owner-created",
            partition_key=dynamodb.Attribute(
                name="owner_sub", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="created_at", type=dynamodb.AttributeType.NUMBER
            ),
        )
        self.tables["runs"].add_global_secondary_index(
            index_name="by-owner-status",
            partition_key=dynamodb.Attribute(
                name="owner_sub", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="status", type=dynamodb.AttributeType.STRING
            ),
        )

        # Two lookups address an item by its own id with no run or user context:
        #   get_thread(thread_id) · document_text/delete_document(document_id)
        # Both partitions are keyed by run, so without an index there is no way to
        # know which partition to read. A Scan is not the fallback: it is O(table)
        # and reads ACROSS TENANTS, so a tenancy slip would become a full-table
        # disclosure rather than a mistake confined to one partition.
        #
        # KEYS_ONLY, so the index stays small and cheap to write: the lookup is
        # GSI query -> base-table GetItem. The GSI makes an item *findable*, never
        # *readable* — authorisation is still the explicit `owner_sub` check
        # against the resolved run, which `api/app.py` performs for both thread
        # routes via `_require_thread_run`.
        #
        # See docs/PHASE2-STORAGE-KEY-DESIGN.md §5. Missing from the first Phase 1
        # deploy, and found by designing the port rather than by running it.
        for table_name, id_attr in (
            ("threads", "thread_id"),
            ("documents", "document_id"),
        ):
            self.tables[table_name].add_global_secondary_index(
                index_name=f"by-{id_attr.replace('_', '-')}",
                partition_key=dynamodb.Attribute(
                    name=id_attr, type=dynamodb.AttributeType.STRING
                ),
                projection_type=dynamodb.ProjectionType.KEYS_ONLY,
            )

        # One bucket, four per-user prefixes (§5.1). One rather than four because
        # the isolation boundary is the `{sub}/` prefix in the scoped role's
        # policy, not the bucket name — four buckets would multiply the policy
        # without adding a boundary.
        self.data_bucket = s3.Bucket(
            self,
            "DataBucket",
            bucket_name=f"{self.config.prefix}-data-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            # Snapshots and document text are content that a bug could overwrite
            # with something wrong. Versioning is the cheap way to make that
            # recoverable, and these objects are small.
            versioned=True,
            removal_policy=self._removal_policy,
        )

    def _create_vector_store(self) -> None:
        """S3 Vectors: one bucket, one index — the one Phase 3 writes to.

        L1 constructs (`CfnVectorBucket`, `CfnIndex`) because S3 Vectors has no L2
        yet. That is fine here — there is nothing an L2 would add over naming the
        four properties that matter.

        **One index now, one index PER KNOWLEDGE BASE later** (§8b). The reason is
        an API constraint rather than a preference: `QueryVectors` takes a single
        index, so a query cannot span several. Per-KB indexes therefore mean a
        persona bound to three KBs issues three parallel queries and merges by
        distance — which is why the ingest path must know which KB a document
        belongs to before it embeds anything. The limit is 10,000 indexes per
        vector bucket.

        This one index exists to validate the API and the IAM shape early, which
        is the whole point of Phase 1.
        """
        self.vector_bucket = s3vectors.CfnVectorBucket(
            self,
            "VectorBucket",
            vector_bucket_name=f"{self.config.prefix}-vectors-{self.account}",
        )

        self.chunks_index = s3vectors.CfnIndex(
            self,
            "ChunksIndex",
            index_name=f"{self.config.prefix}-chunks",
            vector_bucket_name=self.vector_bucket.vector_bucket_name,
            # float32 is the only data type S3 Vectors accepts.
            data_type="float32",
            dimension=EMBEDDING_DIMENSION,
            # Cosine, because Titan v2 returns unit-normalised vectors and the
            # retrieval code's distance→cosine conversion assumes exactly that
            # (`is_unit_norm` guards it, and skips the similarity floor when the
            # assumption fails rather than applying a meaningless threshold).
            distance_metric="cosine",
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=NON_FILTERABLE_METADATA_KEYS,
            ),
        )
        self.chunks_index.add_resource_dependency(self.vector_bucket)

    # ------------------------------------------------------------------ #
    # SPA hosting
    # ------------------------------------------------------------------ #

    def _create_spa_hosting(self) -> None:
        """CloudFront over a private S3 bucket, with the two cache behaviours.

        The behaviours are not decoration. A Vite build emits `index.html`
        referencing content-hashed `/assets/*` files, so:

        * `/assets/*` is **immutable** — the filename changes when the content
          does, so it can be cached forever.
        * `index.html` is **never cached**, because it is the one file whose name
          stays the same while its contents change. Caching it serves an old
          document that references asset filenames which no longer exist, and the
          page hangs blank with no error anywhere. That is not hypothetical: it is
          the failure this project already hit, which is why it is called out in
          the plan.

        The 403/404 → `/index.html` mappings are what make client-side routing
        work: S3 has no object at `/runs/trusted-robot`, and without the mapping a
        deep link or a refresh returns the S3 error instead of the app.
        """
        self.spa_bucket = s3.Bucket(
            self,
            "SpaBucket",
            bucket_name=f"{self.config.prefix}-spa-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            # The SPA is a build artefact, reproducible from source, so destroying
            # it costs nothing — unlike the data bucket, which is why this one does
            # not follow `retain_data`.
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # Origin Access Control, not the legacy Origin Access Identity: OAI does
        # not support SSE-KMS and is no longer the recommended mechanism. Either
        # way the bucket stays private and CloudFront is the only reader.
        spa_origin = origins.S3BucketOrigin.with_origin_access_control(
            self.spa_bucket
        )

        # A cache POLICY governs CloudFront's own cache. It says nothing to the
        # browser. Measured on the first real deployment: with only the policies
        # set, neither `index.html` nor a hashed asset carried a `Cache-Control`
        # header at all — so the edge behaved correctly and the browser fell back
        # to heuristic caching, which for an HTML response carrying `Last-Modified`
        # means caching it anyway. That is the same stale-`index.html` blank page,
        # relocated from the edge to the client, where an invalidation cannot reach
        # it.
        #
        # Response headers policies fix it declaratively. Setting `--cache-control`
        # on the `aws s3 sync` would also work, but it depends on whoever runs the
        # upload remembering two flags, and the failure is silent.
        no_store = cloudfront.ResponseHeadersPolicy(
            self,
            "SpaNoStorePolicy",
            response_headers_policy_name=f"{self.config.prefix}-html-no-store",
            custom_headers_behavior=cloudfront.ResponseCustomHeadersBehavior(
                custom_headers=[
                    cloudfront.ResponseCustomHeader(
                        header="Cache-Control",
                        value="no-store, must-revalidate",
                        # Override, because S3 may send its own value and the
                        # weaker one winning is the whole bug.
                        override=True,
                    )
                ]
            ),
        )
        immutable = cloudfront.ResponseHeadersPolicy(
            self,
            "SpaImmutablePolicy",
            response_headers_policy_name=f"{self.config.prefix}-assets-immutable",
            custom_headers_behavior=cloudfront.ResponseCustomHeadersBehavior(
                custom_headers=[
                    cloudfront.ResponseCustomHeader(
                        header="Cache-Control",
                        # A year, and `immutable` so a browser does not even
                        # revalidate. Safe only because the filename contains a
                        # content hash: new content is a new URL.
                        value="public, max-age=31536000, immutable",
                        override=True,
                    )
                ]
            ),
        )

        self.distribution = cloudfront.Distribution(
            self,
            "SpaDistribution",
            comment=f"{self.config.prefix} SPA",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=spa_origin,
                viewer_protocol_policy=(
                    cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS
                ),
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                response_headers_policy=no_store,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
            ),
            additional_behaviors={
                "/assets/*": cloudfront.BehaviorOptions(
                    origin=spa_origin,
                    viewer_protocol_policy=(
                        cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS
                    ),
                    cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                    response_headers_policy=immutable,
                    allowed_methods=(
                        cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS
                    ),
                ),
            },
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
            ],
        )

    # ------------------------------------------------------------------ #
    # Cognito
    # ------------------------------------------------------------------ #

    def _create_user_pool(self) -> None:
        """A standalone user pool with native users (§2).

        Standalone rather than federated because federation to a corporate IdP is a
        procurement exercise that would block something otherwise ready. The thing
        that makes it a deferral rather than a decision to redo: **`sub` is the
        tenant key regardless of where the identity came from**, so adding an IdP
        later is registering it on this pool and pointing the Hosted UI at it —
        nothing in the isolation model, the key design or any route changes.

        The one cost worth stating now rather than discovering later: a federated
        user gets a *different* `sub` from the native account of the same person, so
        demo-era conversations will not follow a user across the switch unless
        somebody plans a one-time remap keyed on email.
        """
        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{self.config.prefix}-users",
            # An admin creates accounts. Self-signup would put an open
            # registration endpoint on the internet, which for a demo is all
            # downside — there is nobody it is meant to let in.
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            # Optional, not required: a demo audience that cannot enrol MFA is a
            # demo nobody sees. The switch to REQUIRED is one line, and the SMS/TOTP
            # settings below are configured now so flipping it needs no other change.
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            # ESSENTIALS covers MFA and the password policy above. Threat
            # protection (compromised-credential detection, adaptive
            # authentication) needs the PLUS plan, which is several times the
            # per-MAU price — a real recurring cost, not a flag. Stated as an
            # explicit NO_ENFORCEMENT rather than left unset so the choice is
            # visible: turning it on is two enum changes, and worth doing before
            # this holds anything but demo data.
            feature_plan=cognito.FeaturePlan.ESSENTIALS,
            standard_threat_protection_mode=(
                cognito.StandardThreatProtectionMode.NO_ENFORCEMENT
            ),
            removal_policy=self._removal_policy,
        )

        # Hosted UI, so there is no login form in this codebase and no password
        # handling anywhere in it.
        self.user_pool_domain = self.user_pool.add_domain(
            "HostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                # Account-scoped so two deployments in one account do not collide
                # on a globally-unique prefix.
                domain_prefix=f"{self.config.prefix}-{self.account}",
            ),
        )

        callback_urls: List[str] = [
            f"https://{self.distribution.distribution_domain_name}",
            f"https://{self.distribution.distribution_domain_name}/",
        ]
        callback_urls.extend(self.config.extra_callback_urls or [])

        self.user_pool_client = self.user_pool.add_client(
            "SpaClient",
            user_pool_client_name=f"{self.config.prefix}-spa",
            # No secret: this is a public client running in a browser, where a
            # secret would be readable by anyone who opened devtools. PKCE is what
            # replaces it, and Cognito requires PKCE for a public client using the
            # authorization-code flow — so this single line is what makes the flow
            # "authorization code WITH PKCE" rather than plain authorization code.
            generate_secret=False,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(
                    authorization_code_grant=True,
                    # Implicit grant explicitly OFF. It returns tokens in the URL
                    # fragment, where they land in browser history and any
                    # `Referer` header. Leaving it on "just in case" is how an
                    # application ends up with tokens in server logs.
                    implicit_code_grant=False,
                ),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=callback_urls,
                logout_urls=callback_urls,
            ),
            # Short access token, long refresh: the SPA holds tokens in memory and
            # refreshes through the SDK, so a stolen access token has a small
            # window while a user is not asked to log in every hour.
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
            # Cognito's default returns a generic error for a wrong password AND
            # for a user that does not exist. Keeping it prevents the login form
            # from being a way to enumerate who has an account.
            prevent_user_existence_errors=True,
        )

        # Entitlement travels as a JWT claim (§7), so authorisation needs no extra
        # lookup. Created here rather than later because a group referenced by a
        # policy has to exist before the policy can name it.
        self.admin_group = cognito.CfnUserPoolGroup(
            self,
            "AdminGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="admins",
            description="Administrative users: may see other users' runs and "
            "manage spend caps. Carries no privilege until Phase 7 reads it.",
        )

        # The first user, so the deployment is loginable without a console visit.
        # Only when an email is supplied: a pool with a hardcoded default account
        # would be a worse default than a pool with no users.
        self.admin_user: Optional[cognito.CfnUserPoolUser] = None
        if self.config.admin_email:
            self.admin_user = cognito.CfnUserPoolUser(
                self,
                "AdminUser",
                user_pool_id=self.user_pool.user_pool_id,
                username=self.config.admin_email,
                user_attributes=[
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="email", value=self.config.admin_email
                    ),
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="email_verified", value="true"
                    ),
                ],
                # Cognito emails a temporary password. No password is set here, and
                # none is put in the template — a CloudFormation parameter or a
                # context value would be readable in the console and in
                # `cdk synth` output forever.
                desired_delivery_mediums=["EMAIL"],
            )
            cognito.CfnUserPoolUserToGroupAttachment(
                self,
                "AdminUserInGroup",
                user_pool_id=self.user_pool.user_pool_id,
                group_name=self.admin_group.group_name,
                username=self.admin_user.username,
            ).add_resource_dependency(self.admin_user)

    # ------------------------------------------------------------------ #
    # API
    # ------------------------------------------------------------------ #

    def _create_api(self) -> None:
        """Container-image Lambda behind an HTTP API with a JWT authorizer.

        The authorizer is the reason `identity.py` verifies no signatures: API
        Gateway validates the token — issuer, audience, expiry, signature —
        *before* the Lambda is invoked, and passes the verified claims in the
        request context. An unauthenticated request never reaches application code.
        """
        self.api_lambda = lambda_.DockerImageFunction(
            self,
            "ApiFunction",
            function_name=f"{self.config.prefix}-api",
            code=lambda_.DockerImageCode.from_image_asset(
                PROJECT_ROOT,
                file="Dockerfile.lambda",
                # `.dockerignore` is shared with the standalone `Dockerfile`, which
                # legitimately needs `frontend/` and `examples/` — so these are
                # excluded here rather than there. Nothing in this list is COPYed
                # by Dockerfile.lambda, and every megabyte of context is staged
                # locally and uploaded whenever the asset hash changes.
                exclude=[
                    "frontend",
                    "tests",
                    "docs",
                    "scripts",
                    "examples",
                    "*.md",
                    "!README.md",  # pip install reads it via pyproject metadata
                ],
            ),
            # 1769 MB is where a Lambda gets a full vCPU. Below it, CPU is
            # fractional and the image's import time — litellm is not cheap to
            # import — stretches every cold start.
            memory_size=2048,
            # 30s matches API Gateway's own non-negotiable integration timeout, so
            # the Lambda cannot outlive the request that invoked it. Run execution
            # does not live here: that is Step Functions in Phase 5, which is what
            # makes a 40-turn conversation possible at all.
            timeout=Duration.seconds(30),
            environment={
                # The setting that turns tenancy on. Without it the API would
                # attribute every authenticated request to the single local user —
                # which the identity layer guards against by preferring verified
                # claims, but relying on that would be relying on a backstop.
                "AUTH_MODE": "jwt",
                # Off, and this is not an optimisation. The sweep assumes it is the
                # only process; with concurrent Lambda sandboxes, two cold starts
                # would each mark the other's in-flight run as interrupted.
                "STARTUP_SWEEP": "false",
                # /tmp is the only writable path in a Lambda sandbox, and it is
                # per-sandbox and ephemeral — which is exactly why SQLite cannot be
                # the datastore and Phase 2 replaces it. Set so nothing tries to
                # write into the read-only image root instead.
                "DATA_DIR": "/tmp/data",
                "DATA_BUCKET": self.data_bucket.bucket_name,
                "VECTOR_BUCKET": self.vector_bucket.vector_bucket_name,
                "VECTOR_INDEX": self.chunks_index.index_name,
                "USER_POOL_ID": self.user_pool.user_pool_id,
                **{
                    f"TABLE_{name.upper()}": table.table_name
                    for name, table in self.tables.items()
                },
            },
        )

        # Phase 1 grants nothing but Bedrock. The tables and buckets are named in
        # the environment so Phase 2 has somewhere to read them from, but the
        # function cannot touch them yet — a skeleton that could already read every
        # tenant's data would defeat the point of §3, where access arrives as
        # per-request scoped credentials rather than as the function's own role.
        self.api_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                # The region segment is a WILDCARD, and this was learned the hard
                # way on the first deployment rather than reasoned out.
                #
                # The default model is a GLOBAL inference profile
                # (`global.anthropic.claude-haiku-4-5-...`), and when Bedrock
                # authorises the underlying foundation model for a global profile
                # it evaluates a REGION-LESS ARN:
                #     arn:aws:bedrock:::foundation-model/anthropic.claude-haiku-...
                # The first version pinned `us-east-1` and `us-west-2`, which
                # cannot match an empty region segment, so every model call failed
                # with AccessDenied. An IAM `*` matches zero characters, so the
                # wildcard covers both the empty form and every real region.
                #
                # This is not as broad as it looks: foundation-model ARNs carry no
                # account id, so there is no cross-account reach here — the grant
                # is "any Bedrock foundation model", which is what a global profile
                # requires by construction. It also subsumes the us-west-2 entry
                # the image model needed (§5.1). Narrowing to specific models would
                # mean the IAM policy has to be redeployed whenever someone changes
                # `AVAILABLE_MODELS`, coupling configuration to infrastructure;
                # Phase 7's model-invocation logging is the right place for
                # per-model accountability.
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
                    f"arn:aws:bedrock:*:{self.account}:application-inference-profile/*",
                ],
            )
        )

        # §3's mechanism, and the reason the Lambda's own role grants no storage
        # rights. The API reads `sub` from the verified JWT, assumes THIS role with a
        # session policy pinning `dynamodb:LeadingKeys` to `USER#{sub}` and S3 object
        # ARNs to `.../{sub}/*`, and does all storage work with the result.
        #
        # Effective permissions are the INTERSECTION of this role's policy and the
        # session policy, which is why this grants the whole table: the per-request
        # policy is what narrows it, built from a claim the caller cannot forge. A
        # narrow role with a broad session policy — the natural mistake — would
        # produce credentials that work in testing and enforce nothing.
        self.tenant_role = iam.Role(
            self,
            "TenantRole",
            role_name=f"{self.config.prefix}-tenant",
            assumed_by=(
                iam.CompositePrincipal(
                    iam.ArnPrincipal(self.api_lambda.role.role_arn),
                    # Verification only, and off unless explicitly asked for. See
                    # StackConfig.verify_principal_arn: the trust policy is what
                    # makes this role's breadth safe, so widening it is a decision
                    # rather than a convenience.
                    iam.ArnPrincipal(self.config.verify_principal_arn),
                )
                if self.config.verify_principal_arn
                else iam.ArnPrincipal(self.api_lambda.role.role_arn)
            ),
            max_session_duration=Duration.hours(1),
            description="Assumed per request with a session policy scoping every "
            "storage call to one Cognito sub. See matrix_studio/storage/"
            "credentials.py.",
        )
        # A WILDCARD over this deployment's tables, not a grant per table.
        #
        # Enumerating them produced 22 statements and a 5,429-character inline
        # policy, which CDK silently spilled into an `AWS::IAM::ManagedPolicy`
        # overflow — IAM caps an inline policy at 10,240 characters. It worked, but a
        # role may attach only 10 managed policies, so growing the table list would
        # eventually fail the deploy for a reason with nothing to do with the change
        # that triggered it.
        #
        # The wildcard costs nothing in security. This role is deliberately broad —
        # effective permissions are the INTERSECTION of it and the per-request
        # session policy — so enumerating tables narrows nothing while making the
        # policy fragile. What actually confines a request is
        # `dynamodb:LeadingKeys` in `credentials.session_policy`, and the prefix
        # keeps this deployment out of any other stack's tables in the same account.
        self.tenant_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query",
                    "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
                    "dynamodb:BatchWriteItem", "dynamodb:ConditionCheckItem",
                    # Scan is here for the startup sweep only, which is a system
                    # operation and off on Lambda (STARTUP_SWEEP=false). No request
                    # path may scan.
                    "dynamodb:Scan",
                ],
                resources=[
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/"
                    f"{self.config.prefix}-*",
                    f"arn:aws:dynamodb:{self.region}:{self.account}:table/"
                    f"{self.config.prefix}-*/index/*",
                ],
            )
        )
        self.data_bucket.grant_read_write(self.tenant_role)
        self.tenant_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3vectors:GetVectors", "s3vectors:PutVectors",
                         "s3vectors:QueryVectors", "s3vectors:DeleteVectors",
                         "s3vectors:ListVectors"],
                resources=[
                    self.vector_bucket.attr_vector_bucket_arn,
                    f"{self.vector_bucket.attr_vector_bucket_arn}/index/*",
                ],
            )
        )
        # The Lambda may assume it, and nothing else may. Narrower than granting
        # `sts:AssumeRole` on `*`, which would let a compromised function reach any
        # role in the account it happened to be trusted by.
        self.api_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[self.tenant_role.role_arn],
            )
        )
        self.api_lambda.add_environment("TENANT_ROLE_ARN", self.tenant_role.role_arn)
        self.api_lambda.add_environment("TABLE_PREFIX", self.config.prefix)

        self.authorizer = apigw_authorizers.HttpJwtAuthorizer(
            "JwtAuthorizer",
            jwt_issuer=(
                f"https://cognito-idp.{self.region}.amazonaws.com/"
                f"{self.user_pool.user_pool_id}"
            ),
            # The audience is the app client id. Without it any token issued by any
            # client on this pool would be accepted — which matters the moment a
            # second client exists.
            jwt_audience=[self.user_pool_client.user_pool_client_id],
            authorizer_name=f"{self.config.prefix}-jwt",
        )

        self.http_api = apigw.HttpApi(
            self,
            "HttpApi",
            api_name=f"{self.config.prefix}-api",
            # The SPA is served from CloudFront, a different origin from the API,
            # so the browser preflights every non-simple request. Restricted to the
            # CloudFront domain rather than `*`: with credentials in an
            # `Authorization` header, a permissive origin list is what lets any
            # page on the internet call this API with a user's token.
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=[
                    f"https://{self.distribution.distribution_domain_name}",
                    *(self.config.extra_callback_urls or []),
                ],
                allow_methods=[
                    apigw.CorsHttpMethod.GET,
                    apigw.CorsHttpMethod.POST,
                    apigw.CorsHttpMethod.DELETE,
                    apigw.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["authorization", "content-type"],
                max_age=Duration.hours(1),
            ),
            default_authorizer=self.authorizer,
        )

        integration = apigw_integrations.HttpLambdaIntegration(
            "ApiIntegration", handler=self.api_lambda
        )

        # One catch-all route. FastAPI already owns the routing table, and
        # duplicating 38 routes here would create two places for a path to be wrong
        # — with the CDK copy silently winning.
        self.http_api.add_routes(
            path="/{proxy+}",
            methods=[apigw.HttpMethod.ANY],
            integration=integration,
        )

    # ------------------------------------------------------------------ #

    def _worker(
        self, construct_id: str, name: str, handler: str, timeout: Duration
    ) -> lambda_.DockerImageFunction:
        """A worker Lambda on the SAME image as the API, with a different handler.

        One image, three entry points. A second image would double build time and
        upload size, and — more to the point — let the worker's dependencies drift
        from the API's, so a run could execute against a different engine than the
        one the API validated its request with.

        Each worker gets the storage environment and Bedrock, but **no storage rights
        of its own**: like the API it assumes the tenant role per run with a session
        policy scoped to one `sub`. That is §3's mechanism and it does not get an
        exception for being a background worker — a worker with ambient table access
        is exactly the hole `dynamodb:LeadingKeys` exists to close.
        """
        fn = lambda_.DockerImageFunction(
            self,
            construct_id,
            function_name=f"{self.config.prefix}-{name}",
            code=lambda_.DockerImageCode.from_image_asset(
                PROJECT_ROOT,
                file="Dockerfile.lambda",
                # `cmd` overrides the image's own CMD, which is the API handler.
                cmd=[handler],
                exclude=[
                    "frontend", "tests", "docs", "scripts", "examples",
                    "*.md", "!README.md",
                ],
            ),
            memory_size=2048,
            timeout=timeout,
            environment={
                # No AUTH_MODE: a worker has no request and no JWT. Its tenant comes
                # from the execution input, which the API wrote from verified claims.
                "STARTUP_SWEEP": "false",
                "DATA_DIR": "/tmp/data",
                "DATA_BUCKET": self.data_bucket.bucket_name,
                "VECTOR_BUCKET": self.vector_bucket.vector_bucket_name,
                "VECTOR_INDEX": self.chunks_index.index_name,
                "TABLE_PREFIX": self.config.prefix,
                "TENANT_ROLE_ARN": self.tenant_role.role_arn,
                **{
                    f"TABLE_{n.upper()}": t.table_name
                    for n, t in self.tables.items()
                },
            },
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                # Same region wildcard and same reason as the API's grant: a global
                # inference profile is authorised against a region-LESS ARN.
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
                    f"arn:aws:bedrock:*:{self.account}:application-inference-profile/*",
                ],
            )
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"], resources=[self.tenant_role.role_arn]
            )
        )
        # The tenant role trusts each worker explicitly. Adding the principal here
        # rather than widening the role's `assumed_by` keeps the trust policy an
        # enumeration of known callers instead of a pattern.
        self.tenant_role.assume_role_policy.add_statements(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                principals=[iam.ArnPrincipal(fn.role.role_arn)],
            )
        )
        return fn

    def _create_turn_loop(self) -> None:
        """§5.2's turn loop, as a Standard workflow.

        **This is the first phase in which a run can execute at all.** Phase 4 planned
        to run the loop in the API Lambda's background task and was cancelled on
        measurement: Lambda freezes the sandbox when the handler returns, so the run
        died in 8 ms having logged one line. API Gateway's 30-second integration
        timeout rules out the obvious repair. See `docs/AWS-IMPLEMENTATION-PLAN.md`.

        Standard rather than Express: no duration ceiling (a 40-turn conversation at
        6–13 s/turn is minutes, and the budget is user-controlled), and Express's
        at-least-once execution would be wrong for a loop whose states append to an
        event log.
        """
        self.prepare_lambda = self._worker(
            "PrepareFunction", "prepare",
            "matrix_studio.step_handlers.prepare",
            # Generous, because this state ingests and embeds the whole corpus. The
            # 666-chunk measurement corpus took ~90 s of Bedrock calls.
            Duration.minutes(10),
        )
        self.turn_lambda = self._worker(
            "TurnFunction", "turn",
            "matrix_studio.step_handlers.turn",
            # A turn is 6–13 s measured. Five minutes is room for the validation
            # gate's regeneration and a slow provider, without being so long that a
            # wedged turn holds a run open for a quarter of an hour.
            Duration.minutes(5),
        )
        self.finalise_lambda = self._worker(
            "FinaliseFunction", "finalise",
            "matrix_studio.step_handlers.finalise",
            # Writes the terminal status and generates the run summary, which is one
            # LLM call over the whole transcript.
            Duration.minutes(5),
        )

        prepare = sfn_tasks.LambdaInvoke(
            self, "Prepare",
            lambda_function=self.prepare_lambda,
            # `payload_response_only` keeps the state's output as the handler's return
            # value rather than wrapping it in Lambda's envelope. Without it every
            # downstream Choice and every input path would have to reach through
            # `$.Payload`, and the handlers' event shape — deliberately identical
            # across the three — would stop being the contract.
            payload_response_only=True,
        )
        turn = sfn_tasks.LambdaInvoke(
            self, "Turn",
            lambda_function=self.turn_lambda,
            payload_response_only=True,
        )
        finalise = sfn_tasks.LambdaInvoke(
            self, "Finalise",
            lambda_function=self.finalise_lambda,
            payload_response_only=True,
        )

        # Retry, which is the operational reason this is an orchestrator rather than
        # a loop. §7 names Bedrock throttling as the real risk at company scale:
        # quotas are per-account per-model, so many concurrent users hit them.
        #
        # Retrying the whole turn is safe because a slice is idempotent by
        # construction — it trims dangling events past the last checkpoint before
        # generating, so a turn that died half-written is not appended twice.
        for task in (prepare, turn, finalise):
            task.add_retry(
                errors=["States.TaskFailed", "States.Timeout"],
                interval=Duration.seconds(5),
                max_attempts=4,
                backoff_rate=2.0,
                max_delay=Duration.minutes(2),
            )
            # Lambda's own transient failures get a separate, faster policy: a
            # throttle or a service exception is not a turn that went wrong.
            task.add_retry(
                errors=[
                    "Lambda.TooManyRequestsException",
                    "Lambda.ServiceException",
                    "Lambda.AWSLambdaException",
                    "Lambda.SdkClientException",
                ],
                interval=Duration.seconds(2),
                max_attempts=6,
                backoff_rate=2.0,
            )

        # A turn that exhausts its retries must still leave the run in a terminal
        # status. Without this the run sits at `running` for ever with no execution
        # behind it, which is indistinguishable to a user from a run that is simply
        # slow — and is the failure mode the startup sweep used to paper over.
        mark_failed = sfn_tasks.LambdaInvoke(
            self, "MarkFailed",
            lambda_function=self.finalise_lambda,
            payload=sfn.TaskInput.from_object({
                "run_id": sfn.JsonPath.string_at("$.run_id"),
                "owner_sub": sfn.JsonPath.string_at("$.owner_sub"),
                "status": "failed",
            }),
            payload_response_only=True,
        )
        for task in (prepare, turn):
            task.add_catch(
                mark_failed,
                errors=["States.ALL"],
                # The error is discarded rather than merged into the state, because
                # the payload MarkFailed needs is `run_id` and `owner_sub` and a
                # Step Functions error object can be large enough to matter against
                # the 256 KB state I/O quota.
                result_path=sfn.JsonPath.DISCARD,
            )

        # CheckContinue is a pure Choice over the turn's own output — no DynamoDB read.
        #
        # Step Functions can read DynamoDB natively, and §6 originally described the
        # stop check that way. Rejected: a native integration reads with the STATE
        # MACHINE's role, which is not per-tenant, so `dynamodb:LeadingKeys` cannot
        # constrain it. Every other storage access in this system goes through
        # credentials scoped to one `sub`; this would have been the single exception,
        # on the hottest path. The turn Lambda already reads the run row for its
        # config and returns the verdict, so the same guarantee costs nothing.
        check = (
            sfn.Choice(self, "CheckContinue")
            .when(sfn.Condition.boolean_equals("$.done", True), finalise)
            .otherwise(turn)
        )

        turn.next(check)
        definition = prepare.next(turn)

        self.turn_loop = sfn.StateMachine(
            self,
            "TurnLoop",
            state_machine_name=f"{self.config.prefix}-turn-loop",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            state_machine_type=sfn.StateMachineType.STANDARD,
            # A ceiling, not an expectation. A 40-turn run is minutes; a day means
            # something is wedged, and an execution that never ends is one that keeps
            # a run listed as `running` for ever.
            timeout=Duration.days(1),
            tracing_enabled=True,
        )

        # The API starts executions; nothing else does.
        self.turn_loop.grant_start_execution(self.api_lambda)
        self.api_lambda.add_environment(
            "TURN_LOOP_ARN", self.turn_loop.state_machine_arn
        )

    # ------------------------------------------------------------------ #

    def _outputs(self) -> None:
        """Everything needed to log in, deploy the SPA, and check the phase is done."""
        CfnOutput(
            self,
            "ApiUrl",
            value=self.http_api.api_endpoint,
            description="HTTP API base URL. GET /api/health with a bearer token "
            "returns 200; without one, 401.",
        )
        CfnOutput(
            self,
            "SpaUrl",
            value=f"https://{self.distribution.distribution_domain_name}",
            description="CloudFront URL for the SPA.",
        )
        CfnOutput(
            self,
            "SpaBucketName",
            value=self.spa_bucket.bucket_name,
            description="Sync the built frontend here, then invalidate index.html.",
        )
        CfnOutput(
            self,
            "HostedUiUrl",
            value=self.user_pool_domain.base_url(),
            description="Cognito Hosted UI. Self-signup is disabled; an admin "
            "creates users.",
        )
        CfnOutput(
            self,
            "UserPoolId",
            value=self.user_pool.user_pool_id,
            description="Create users with `aws cognito-idp admin-create-user`.",
        )
        CfnOutput(
            self,
            "UserPoolClientId",
            value=self.user_pool_client.user_pool_client_id,
            description="Public SPA client (no secret; PKCE). Also the JWT audience.",
        )
        CfnOutput(
            self,
            "DataBucketName",
            value=self.data_bucket.bucket_name,
            description="Snapshots, document text, uploads, avatars — per-user "
            "prefixes.",
        )
        CfnOutput(
            self,
            "TenantRoleArn",
            value=self.tenant_role.role_arn,
            description="Assumed per request with a tenant-scoped session policy "
            "(§3). Verify with scripts/verify_tenant_isolation.py.",
        )
        CfnOutput(
            self,
            "VectorBucketName",
            value=self.vector_bucket.vector_bucket_name,
            description="S3 Vectors bucket. One index per knowledge base (§8b).",
        )
