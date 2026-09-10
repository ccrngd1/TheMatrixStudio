# SPDX-License-Identifier: Apache-2.0
"""
Assertions over the synthesized CloudFormation template.

What these are for: infrastructure has a long feedback loop, and the mistakes that
matter here are the *silent* ones — a cache policy that serves a stale
`index.html`, a JWT authorizer with no audience, self-signup left on. All three
deploy successfully and look fine. None would be caught by "did `cdk deploy`
succeed".

What they are NOT for: re-stating the stack. A test asserting that a table named
`runs` exists tells you only that you can read your own code. Every assertion here
is about a property whose *absence* is a working deployment with a real defect.

These need no AWS account and no credentials — `Template.from_stack` synthesizes
in-process — so they run in the same pytest invocation as everything else.
"""

import re

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from matrix_infra.config import EMBEDDING_DIMENSION, StackConfig
from matrix_infra.stack import MatrixStudioStack

ACCOUNT = "111122223333"
REGION = "us-east-1"


def _template(**config_kwargs) -> Template:
    app = cdk.App()
    stack = MatrixStudioStack(
        app,
        "test-stack",
        config=StackConfig(**config_kwargs),
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    """The default deployment. Module-scoped: synthesis is not cheap."""
    return _template(admin_email="admin@example.com")


# --------------------------------------------------------------------------- #
# Cognito — the settings whose wrong value is a security hole that deploys fine
# --------------------------------------------------------------------------- #


def test_self_signup_is_disabled(template: Template):
    """Otherwise this is an open registration endpoint on the internet.

    Nothing in the app would look broken; anyone who found the Hosted UI could
    create an account and start spending Bedrock tokens.
    """
    template.has_resource_properties(
        "AWS::Cognito::UserPool",
        {"AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True}},
    )


def test_the_spa_client_has_no_secret(template: Template):
    """A browser client cannot keep a secret, and having one disables PKCE.

    Cognito requires PKCE for a *public* client on the authorization-code flow.
    Generating a secret would make this a confidential client, PKCE would become
    optional, and the flow would be vulnerable to code interception — while still
    working perfectly in a demo.

    Asserted as an explicit `false` rather than "absent": CDK emits the property
    either way, so `Match.absent()` would fail even on a correct stack — and if it
    ever passed, it would be because CDK changed its output, not because the
    setting was right.
    """
    template.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {"GenerateSecret": False},
    )


def test_the_implicit_grant_is_off(template: Template):
    """Implicit returns tokens in the URL fragment — history, Referer, logs.

    Asserted as an exact list rather than "code is present", because the failure
    mode is an *extra* flow being enabled alongside the right one.
    """
    template.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {"AllowedOAuthFlows": ["code"]},
    )


def test_user_existence_errors_are_hidden(template: Template):
    """Or the login form answers "does this person have an account here"."""
    template.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {"PreventUserExistenceErrors": "ENABLED"},
    )


def test_the_password_policy_is_not_the_default(template: Template):
    template.has_resource_properties(
        "AWS::Cognito::UserPool",
        {
            "Policies": {
                "PasswordPolicy": {
                    "MinimumLength": 12,
                    "RequireLowercase": True,
                    "RequireNumbers": True,
                    "RequireSymbols": True,
                    "RequireUppercase": True,
                }
            }
        },
    )


def test_no_password_appears_anywhere_in_the_template(template: Template):
    """The admin user must get a Cognito-emailed temporary password.

    A `TemporaryPassword` property — or a CloudFormation parameter for one — is
    readable in the console, in `cdk synth` output, and in the stack's event
    history forever. This is a whole-template scan rather than a property check
    because the point is that there is no such value ANYWHERE, including in a
    place nobody thought to look.
    """
    body = str(template.to_json())
    for forbidden in ("TemporaryPassword", "temporary_password"):
        assert forbidden not in body, f"{forbidden} must never be in the template"


def test_an_admin_user_is_created_when_an_email_is_given(template: Template):
    template.resource_count_is("AWS::Cognito::UserPoolUser", 1)
    template.resource_count_is("AWS::Cognito::UserPoolUserToGroupAttachment", 1)


def test_no_admin_email_means_no_user():
    """A pool carrying a hardcoded default account is worse than an empty pool.

    The tempting alternative — invent a username so the stack is always
    loginable — puts a known account on every deployment, and nobody would notice
    it was there.
    """
    _template().resource_count_is("AWS::Cognito::UserPoolUser", 0)


# --------------------------------------------------------------------------- #
# API Gateway — the authorizer
# --------------------------------------------------------------------------- #


def test_the_jwt_authorizer_pins_both_issuer_and_audience(template: Template):
    """An authorizer without an audience accepts tokens from ANY client on the pool.

    That is invisible with one client and a cross-application hole the moment a
    second one exists — including one somebody adds for an unrelated purpose.
    """
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Authorizer",
        {
            "AuthorizerType": "JWT",
            "JwtConfiguration": {
                "Audience": Match.any_value(),
                "Issuer": Match.any_value(),
            },
        },
    )


def test_every_route_requires_authorization(template: Template):
    """The whole premise of "no unauthenticated request reaches a Lambda".

    A `default_authorizer` that failed to attach would leave `AuthorizationType:
    NONE` on the catch-all route, and every endpoint would be public while the
    authorizer sat in the template looking correct.
    """
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    assert routes, "no routes found"
    for logical_id, route in routes.items():
        props = route["Properties"]
        assert props.get("AuthorizationType") == "JWT", (
            f"{logical_id} ({props.get('RouteKey')}) is not JWT-authorized"
        )
        assert props.get("AuthorizerId"), f"{logical_id} has no authorizer attached"


def test_cors_does_not_allow_any_origin(template: Template):
    """With bearer tokens, `*` lets any page on the internet call this API.

    Also asserts no wildcard slipped into the header list, which is the same hole
    reached a different way.
    """
    apis = template.find_resources("AWS::ApiGatewayV2::Api")
    cors = next(iter(apis.values()))["Properties"]["CorsConfiguration"]
    assert "*" not in cors["AllowOrigins"], cors["AllowOrigins"]
    assert "*" not in cors.get("AllowHeaders", []), cors.get("AllowHeaders")


# --------------------------------------------------------------------------- #
# CloudFront — the two cache behaviours, and the blank-page failure
# --------------------------------------------------------------------------- #


def test_index_html_is_never_cached_and_assets_are(template: Template):
    """The behaviour split that prevents a silent blank page.

    A Vite build emits content-hashed `/assets/*` referenced by an `index.html`
    whose name never changes. Cache `index.html` and a returning visitor gets an
    old document pointing at asset files that no longer exist: the page hangs
    blank, with no error in any log. This project already hit that, which is why
    the two behaviours are asserted to be DIFFERENT policies rather than merely
    present.
    """
    dist = next(iter(template.find_resources("AWS::CloudFront::Distribution").values()))
    config = dist["Properties"]["DistributionConfig"]

    default_policy = config["DefaultCacheBehavior"]["CachePolicyId"]
    behaviours = {b["PathPattern"]: b for b in config["CacheBehaviors"]}
    assert "/assets/*" in behaviours, "no immutable behaviour for hashed assets"
    assets_policy = behaviours["/assets/*"]["CachePolicyId"]

    # AWS managed policy ids: CachingDisabled and CachingOptimized.
    assert default_policy == "4135ea2d-6df8-44a3-9df3-4b5a84be39ad", default_policy
    assert assets_policy == "658327ea-f89d-4fab-a63d-7e88639e58f6", assets_policy
    assert default_policy != assets_policy


def test_deep_links_fall_back_to_the_spa(template: Template):
    """S3 has no object at `/runs/trusted-robot`.

    Without the 403/404 → `/index.html` mapping, a refresh on any client-side
    route returns an S3 error document instead of the application — so the app
    works until somebody bookmarks a page.
    """
    dist = next(iter(template.find_resources("AWS::CloudFront::Distribution").values()))
    responses = {
        r["ErrorCode"]: r
        for r in dist["Properties"]["DistributionConfig"]["CustomErrorResponses"]
    }
    for code in (403, 404):
        assert code in responses, f"no fallback for {code}"
        assert responses[code]["ResponseCode"] == 200
        assert responses[code]["ResponsePagePath"] == "/index.html"
        # A cached error response would pin the fallback in place across a deploy.
        assert responses[code]["ErrorCachingMinTTL"] == 0


def test_the_spa_bucket_is_private(template: Template):
    """CloudFront must be the only reader; a public bucket bypasses it entirely."""
    for bucket in template.find_resources("AWS::S3::Bucket").values():
        config = bucket["Properties"]["PublicAccessBlockConfiguration"]
        assert config == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }, config


# --------------------------------------------------------------------------- #
# S3 Vectors — the two create-time properties that cannot be changed later
# --------------------------------------------------------------------------- #


def test_the_vector_index_dimension_matches_the_measured_choice(template: Template):
    """1024, and immutable after creation.

    An index's dimension is fixed at creation, so a wrong value here is not a
    config change later — it is re-creating every index and re-embedding every
    document. The value came from measurement
    (docs/EMBEDDING-DIMENSION-MEASUREMENT.md), so this test also pins the code to
    the evidence rather than to a default.
    """
    assert EMBEDDING_DIMENSION == 1024
    template.has_resource_properties(
        "AWS::S3Vectors::Index",
        {
            "Dimension": 1024,
            "DataType": "float32",
            "DistanceMetric": "cosine",
        },
    )


def test_passage_text_is_declared_non_filterable(template: Template):
    """The property that makes one-round-trip retrieval possible.

    Passage text rides as vector metadata so a k-NN query returns ids, scores and
    passages together. It has to be non-filterable: filterable metadata has a
    ~2 KB per-vector budget that a 749-byte-mean passage would eat, and nothing
    ever filters on text. Declared at index creation, so it is not fixable later
    either.
    """
    template.has_resource_properties(
        "AWS::S3Vectors::Index",
        {"MetadataConfiguration": {"NonFilterableMetadataKeys": ["text"]}},
    )


# --------------------------------------------------------------------------- #
# Data protection
# --------------------------------------------------------------------------- #


def test_data_stores_are_retained_by_default(template: Template):
    """`cdk destroy` must not silently take every conversation with it.

    Asserted on tables and the data bucket. The SPA bucket is deliberately exempt
    — it holds a build artefact reproducible from source — so this checks the
    distinction actually exists rather than assuming it.
    """
    for logical_id, table in template.find_resources("AWS::DynamoDB::Table").items():
        assert table.get("DeletionPolicy") == "Retain", logical_id

    buckets = template.find_resources("AWS::S3::Bucket")
    policies = {b.get("DeletionPolicy") for b in buckets.values()}
    assert "Retain" in policies, "the data bucket must be retained"
    assert "Delete" in policies, (
        "the SPA bucket should be destroyable; if both are Retain, every deploy "
        "leaves an orphaned bucket behind"
    )


@pytest.mark.parametrize(
    "context_value,expected",
    [
        # The case that motivates the whole function: `-c retain_data=false`
        # reaches Python as the STRING "false", and `bool("false")` is True. A
        # naive read would keep data somebody explicitly asked to be destroyable —
        # and the identical bug in a future flag with the opposite polarity is
        # data loss.
        ("false", False),
        ("False", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("true", True),
        ("yes", True),
        ("1", True),
        # Anything unrecognised keeps data. An unparseable value must not be read
        # as permission to delete.
        ("maybe", True),
        ("", True),
        (False, False),
        (True, True),
    ],
)
def test_retain_data_parses_context_strings(context_value, expected):
    node = cdk.App(context={"retain_data": context_value}).node
    assert StackConfig.from_context(node).retain_data is expected


def test_retain_data_defaults_to_retaining():
    """Omitting the flag must never be the destructive choice."""
    assert StackConfig.from_context(cdk.App().node).retain_data is True


def test_destroyable_deployment_marks_tables_for_deletion():
    """The other side of the flag, so the safe default is a choice not an accident."""
    t = _template(retain_data=False)
    for logical_id, table in t.find_resources("AWS::DynamoDB::Table").items():
        assert table.get("DeletionPolicy") == "Delete", logical_id


def test_tables_have_point_in_time_recovery(template: Template):
    """The event log is the source of truth and reconstructible from nothing else."""
    for logical_id, table in template.find_resources("AWS::DynamoDB::Table").items():
        spec = table["Properties"].get("PointInTimeRecoverySpecification")
        if logical_id.startswith("Connections"):
            continue  # ephemeral by design; TTL deletes rows on purpose
        assert spec == {"PointInTimeRecoveryEnabled": True}, logical_id


def test_tables_are_on_demand(template: Template):
    """Provisioned capacity would bill continuously for an idle demo."""
    for logical_id, table in template.find_resources("AWS::DynamoDB::Table").items():
        assert table["Properties"]["BillingMode"] == "PAY_PER_REQUEST", logical_id


def test_the_runs_table_has_both_gsis(template: Template):
    """The history list and the interrupted-run sweep both need one."""
    tables = template.find_resources("AWS::DynamoDB::Table")
    runs = next(
        t for lid, t in tables.items()
        if t["Properties"].get("TableName") == "matrix-studio-runs"
    )
    names = {g["IndexName"] for g in runs["Properties"]["GlobalSecondaryIndexes"]}
    assert names == {"by-owner-created", "by-owner-status"}, names


# --------------------------------------------------------------------------- #
# The Lambda
# --------------------------------------------------------------------------- #


def test_the_api_lambda_is_a_container_image(template: Template):
    """A zip-packaged Lambda cannot hold this: litellm alone is 91 MB of the
    250 MB unzipped limit, and the installed environment is ~343 MB."""
    functions = template.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"FunctionName": "matrix-studio-api"}},
    )
    assert len(functions) == 1
    props = next(iter(functions.values()))["Properties"]
    assert props["PackageType"] == "Image"
    assert "Runtime" not in props, "an image function must not declare a runtime"


def test_the_api_lambda_runs_in_jwt_auth_mode(template: Template):
    """Without this the API would attribute every request to the local single user.

    The identity layer prefers verified claims and so would behave correctly
    anyway — but that is a backstop, and shipping a deployment that depends on the
    backstop means the primary mechanism is untested in production.
    """
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "matrix-studio-api",
            "Environment": {"Variables": Match.object_like({"AUTH_MODE": "jwt"})},
        },
    )


def _policies_for(template: Template, role_logical_prefix: str):
    """The inline policies attached to a role, found by its logical id.

    Needed because an earlier version of the test below scanned EVERY
    `AWS::IAM::Policy` in the template. That was fine while only one role had
    policies, and became wrong the moment the tenant role appeared — which
    legitimately holds DynamoDB rights. A test that cannot say *whose* permission it
    is checking will either fail on a correct change or pass on an incorrect one.
    """
    roles = {
        lid: r for lid, r in template.find_resources("AWS::IAM::Role").items()
        if lid.startswith(role_logical_prefix)
    }
    assert roles, f"no role with logical id starting {role_logical_prefix!r}"
    out = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        refs = {
            r.get("Ref") for r in policy["Properties"].get("Roles", [])
            if isinstance(r, dict)
        }
        if refs & set(roles):
            out.append(policy["Properties"]["PolicyDocument"])
    # Managed policies too, because CDK moves statements into an
    # `AWS::IAM::ManagedPolicy` overflow once an inline policy nears IAM's
    # 10,240-character limit — silently. An earlier version of this helper read only
    # `AWS::IAM::Policy` and therefore reported that the tenant role held no S3
    # rights when it held them in the overflow: a test that would have failed on a
    # correct stack, which is worse than one that passes on a broken one.
    for policy in template.find_resources("AWS::IAM::ManagedPolicy").values():
        refs = {
            r.get("Ref") for r in policy["Properties"].get("Roles", [])
            if isinstance(r, dict)
        }
        if refs & set(roles):
            out.append(policy["Properties"]["PolicyDocument"])
    return out


def _actions(documents) -> set:
    out = set()
    for doc in documents:
        for statement in doc["Statement"]:
            action = statement.get("Action")
            for a in ([action] if isinstance(action, str) else action or []):
                if isinstance(a, str):
                    out.add(a)
    return out


def test_the_api_lambda_holds_no_storage_rights_of_its_own(template: Template):
    """§3: storage access must arrive as per-request scoped credentials.

    The function's own role grants Bedrock and one `sts:AssumeRole`, and nothing
    else. If it could read the tables directly, the `dynamodb:LeadingKeys` boundary
    would become optional — application code would work either way, and the
    guarantee that "a missing tenant filter cannot leak data" would quietly stop
    being true with nothing failing.

    Scoped to the Lambda's own role. The tenant role holds these rights on purpose;
    a test that checked every policy in the template could not tell the two apart.
    """
    actions = _actions(_policies_for(template, "ApiFunctionServiceRole"))
    assert actions, "the API function has no policy at all"
    forbidden = {a for a in actions
                 if a.startswith(("dynamodb:", "s3vectors:", "s3:GetObject",
                                  "s3:PutObject"))}
    assert not forbidden, (
        f"the API function must not hold storage rights directly: {sorted(forbidden)}"
    )
    assert "bedrock:InvokeModel" in actions
    assert "sts:AssumeRole" in actions


def test_the_lambda_can_assume_only_the_tenant_role(template: Template):
    """`sts:AssumeRole` on `*` would let a compromised function reach any role
    in the account that happens to trust it."""
    for doc in _policies_for(template, "ApiFunctionServiceRole"):
        for statement in doc["Statement"]:
            action = statement.get("Action")
            actions = [action] if isinstance(action, str) else action or []
            if "sts:AssumeRole" not in actions:
                continue
            resource = statement["Resource"]
            resources = [resource] if not isinstance(resource, list) else resource
            assert "*" not in resources, "AssumeRole must name the tenant role"
            assert resources, "AssumeRole with no resource"


def test_the_tenant_role_is_assumable_only_by_the_api_function(template: Template):
    """Anything else in the account being able to assume it defeats the point.

    The role is deliberately broad — effective permissions are the intersection of
    it and the per-request session policy — so who may assume it *is* the control.
    """
    roles = {
        lid: r for lid, r in template.find_resources("AWS::IAM::Role").items()
        if lid.startswith("TenantRole")
    }
    assert len(roles) == 1
    trust = next(iter(roles.values()))["Properties"]["AssumeRolePolicyDocument"]
    principals = []
    for statement in trust["Statement"]:
        assert statement["Action"] == "sts:AssumeRole"
        principals.append(statement["Principal"])
    assert len(principals) == 1, f"more than one trusted principal: {principals}"
    body = str(principals[0])
    assert "AWS" in principals[0], principals[0]
    assert "Service" not in principals[0], (
        "a service principal here would let that service assume the tenant role"
    )
    assert "ApiFunctionServiceRole" in body, body


def test_no_verification_principal_by_default(template: Template):
    """`verify_principal_arn` must be off unless asked for.

    It widens the one control that makes the tenant role's deliberate breadth safe.
    Asserted as an absence, because the failure mode is somebody leaving the flag in
    a deploy script and nothing complaining.
    """
    trust = next(
        r for lid, r in template.find_resources("AWS::IAM::Role").items()
        if lid.startswith("TenantRole")
    )["Properties"]["AssumeRolePolicyDocument"]
    assert "arn:aws:iam::" not in str(trust), (
        "a literal principal ARN in the trust policy means verify_principal_arn "
        f"is set: {trust}"
    )


def test_the_verification_principal_is_additive_when_set():
    """When set, it must ADD a principal rather than replace the Lambda's.

    Replacing it would leave a role nothing in production can assume — a stack that
    deploys and then fails every request, which is the worse of the two mistakes.
    """
    t = _template(verify_principal_arn="arn:aws:iam::111122223333:role/Admin")
    trust = next(
        r for lid, r in t.find_resources("AWS::IAM::Role").items()
        if lid.startswith("TenantRole")
    )["Properties"]["AssumeRolePolicyDocument"]
    body = str(trust)
    assert "arn:aws:iam::111122223333:role/Admin" in body, body
    assert "ApiFunctionServiceRole" in body, (
        "the API function must still be able to assume the tenant role"
    )


def test_the_tenant_role_can_reach_storage(template: Template):
    """The other half: a role that cannot read the tables scopes nothing.

    Paired with the Lambda test above deliberately — a change that moved the grants
    to the wrong principal would satisfy one of them and break the other.
    """
    actions = _actions(_policies_for(template, "TenantRole"))
    for service in ("dynamodb:", "s3vectors:"):
        assert any(a.startswith(service) for a in actions), (service, sorted(actions))
    # Matched by prefix, not by exact name: CDK's bucket grants emit `s3:GetObject*`
    # with a trailing wildcard, so `"s3:GetObject" in actions` is False on a correct
    # stack. Asserting the exact string is a test that fails for the wrong reason.
    assert any(a.startswith("s3:GetObject") for a in actions), sorted(actions)
    assert any(a.startswith("s3:PutObject") for a in actions), sorted(actions)


def test_bedrock_foundation_model_access_is_region_wildcarded(template: Template):
    """A region-pinned foundation-model ARN cannot authorise a GLOBAL profile.

    Observed on the first real deployment, not deduced. The default model is
    `global.anthropic.claude-haiku-4-5-...`, and when Bedrock authorises the
    underlying foundation model for a global inference profile it evaluates a
    **region-less** ARN:

        arn:aws:bedrock:::foundation-model/anthropic.claude-haiku-4-5-...

    The first version granted `arn:aws:bedrock:us-east-1::foundation-model/*` and
    `...us-west-2...`, neither of which matches an empty region segment, so every
    model call returned AccessDenied — while `/api/health` returned 200 and the
    stack looked healthy. An IAM `*` matches zero characters, which is what makes
    the wildcard cover the empty form as well as every real region.

    The wildcard also subsumes the separate us-west-2 grant the Stability image
    model needs (§5.1), which the previous version of this test checked for.
    """
    resources = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if "bedrock:InvokeModel" in str(statement.get("Action")):
                resources.extend(statement["Resource"])
    assert resources, "no bedrock:InvokeModel grant found"

    flat = [str(r) for r in resources]
    assert any("bedrock:*::foundation-model" in r for r in flat), (
        "foundation-model ARNs must wildcard the region, or a global inference "
        f"profile cannot be invoked: {flat}"
    )
    # A region-pinned foundation-model ARN is the defect; assert it is gone rather
    # than only that the right one is present, since both could coexist and the
    # pinned one would be dead weight suggesting the region matters.
    assert not any(
        re.search(r"bedrock:[a-z]{2}-[a-z]+-\d::foundation-model", r) for r in flat
    ), f"region-pinned foundation-model ARN left behind: {flat}"


def test_the_browser_is_told_how_to_cache_too(template: Template):
    """A cache POLICY governs the edge; a browser needs a `Cache-Control` header.

    Found on the first real deployment: with only the cache policies set, neither
    `index.html` nor a hashed asset carried `Cache-Control` at all. The edge
    behaved correctly, and the browser fell back to heuristic caching — which for
    an HTML response with `Last-Modified` means caching it anyway. Same
    stale-`index.html` blank page, relocated to the client, where a CloudFront
    invalidation cannot reach it.

    Asserted on the response headers policies rather than on the cache policies,
    because those were already right and the deployment was still wrong.
    """
    policies = {
        p["Properties"]["ResponseHeadersPolicyConfig"]["Name"]: p["Properties"][
            "ResponseHeadersPolicyConfig"
        ]
        for p in template.find_resources(
            "AWS::CloudFront::ResponseHeadersPolicy"
        ).values()
    }

    html = policies["matrix-studio-html-no-store"]
    assets = policies["matrix-studio-assets-immutable"]

    def header(config):
        items = config["CustomHeadersConfig"]["Items"]
        entry = next(i for i in items if i["Header"] == "Cache-Control")
        # Override matters: S3 may send its own value, and the weaker one winning
        # is the entire bug.
        assert entry["Override"] is True, entry
        return entry["Value"]

    assert "no-store" in header(html), header(html)
    assert "immutable" in header(assets), header(assets)
    assert "max-age=31536000" in header(assets), header(assets)


def test_both_spa_behaviours_attach_a_response_headers_policy(template: Template):
    """The policies existing is not the same as them being attached.

    Two resources in the template and neither referenced by a behaviour is a
    deployment with the original defect and a passing test above it.
    """
    dist = next(
        iter(template.find_resources("AWS::CloudFront::Distribution").values())
    )
    config = dist["Properties"]["DistributionConfig"]
    assert config["DefaultCacheBehavior"].get("ResponseHeadersPolicyId"), (
        "index.html has no response headers policy attached"
    )
    assets = next(
        b for b in config["CacheBehaviors"] if b["PathPattern"] == "/assets/*"
    )
    assert assets.get("ResponseHeadersPolicyId"), (
        "/assets/* has no response headers policy attached"
    )
    assert (
        config["DefaultCacheBehavior"]["ResponseHeadersPolicyId"]
        != assets["ResponseHeadersPolicyId"]
    ), "both behaviours share one policy, so one of them is wrong"


def test_id_only_lookups_have_an_index(template: Template):
    """`get_thread(id)` and `document_text(id)` carry no run or user context.

    Both partitions are keyed by run, so without an index there is no partition to
    read. The fallback would be a Scan — O(table), and it reads ACROSS TENANTS, so
    a tenancy slip becomes a full-table disclosure instead of a mistake confined to
    one partition. See docs/PHASE2-STORAGE-KEY-DESIGN.md §5.

    Missing from the first Phase 1 deploy; found by designing the storage port.
    """
    tables = template.find_resources("AWS::DynamoDB::Table")
    wanted = {
        "matrix-studio-threads": "by-thread-id",
        "matrix-studio-documents": "by-document-id",
    }
    for table_name, index_name in wanted.items():
        table = next(
            t for t in tables.values()
            if t["Properties"].get("TableName") == table_name
        )
        gsis = {
            g["IndexName"]: g
            for g in table["Properties"].get("GlobalSecondaryIndexes", [])
        }
        assert index_name in gsis, f"{table_name} has no {index_name}: {list(gsis)}"
        # KEYS_ONLY on purpose: the index exists to locate an item, not to serve it.
        # A projection carrying attributes would make it tempting to read the item
        # straight off the index — skipping the base-table read, and with it the
        # ownership check that read is paired with.
        assert gsis[index_name]["Projection"]["ProjectionType"] == "KEYS_ONLY", (
            f"{index_name} should project keys only"
        )


def test_the_tenant_policy_does_not_overflow(template: Template):
    """One wildcard statement, not a grant per table.

    Enumerating the tables produced 22 statements and a 5,429-character inline
    policy, which CDK spilled into an `AWS::IAM::ManagedPolicy` overflow — IAM caps
    an inline policy at 10,240 characters. That worked, but a role may attach only
    ten managed policies, so adding tables would eventually fail a deploy for a
    reason unrelated to the change that triggered it.

    The wildcard loses nothing: this role is deliberately broad, and what confines a
    request is `dynamodb:LeadingKeys` in the per-request session policy.
    """
    overflow = [
        lid for lid in template.find_resources("AWS::IAM::ManagedPolicy")
        if "Overflow" in lid
    ]
    assert not overflow, (
        f"an IAM policy is overflowing into a managed policy: {overflow}. "
        "Prefer one wildcard statement over a grant per resource."
    )
