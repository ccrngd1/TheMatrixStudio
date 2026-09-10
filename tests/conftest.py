# SPDX-License-Identifier: Apache-2.0
"""Pytest configuration and shared fixtures."""

import os
import sys
from pathlib import Path

import pytest

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@pytest.fixture(autouse=True)
def reset_settings_singleton():
    """Reset the settings singleton between tests."""
    import matrix_studio.settings
    matrix_studio.settings._settings = None
    yield
    matrix_studio.settings._settings = None


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Give every test the same environment: code defaults, no local config.

    ``_MSS_TEST_MODE`` stops ``Settings`` from reading ``.env`` directly, but that
    is not sufficient on its own. Importing litellm runs ``load_dotenv()`` at
    import time (``litellm/__init__.py``), which copies the developer's ``.env``
    into ``os.environ`` — and real environment variables outrank everything. So
    on a machine with a populated ``.env`` (i.e. anyone who followed the README),
    tests would silently pick up local configuration and assertions about
    defaults would fail.

    Clearing every var that maps to a ``Settings`` field closes that hole at the
    root. The list is derived from the model so it cannot go stale as fields are
    added. Tests that want a specific value still set it with ``monkeypatch``,
    which runs after this fixture.
    """
    monkeypatch.setenv("_MSS_TEST_MODE", "1")

    # Force litellm's import — and therefore its one-time load_dotenv() — to
    # happen BEFORE the clearing below, not after it.
    #
    # Measured: with this absent, `pytest tests/test_settings.py` alone failed
    # test_settings_defaults while the full suite passed. Clearing ran first, then
    # the mock_analysis_llm fixture below imported matrix_studio.analysis ->
    # litellm -> load_dotenv(), which put the developer's LITELLM_MODEL straight
    # back into os.environ. In a whole-suite run litellm was already in
    # sys.modules by collection time, so load_dotenv never re-ran and the
    # clearing appeared to work. That made the isolation order-dependent: whether
    # a test saw code defaults or local config depended on which other test files
    # were selected.
    try:
        import litellm  # noqa: F401
    except ImportError:
        pass

    from matrix_studio.settings import Settings

    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)

    # Not Settings fields, but read straight from the environment by
    # litellm/boto3. Cleared so a stray credential cannot turn a mocked test
    # into a live billable call.
    for var in (
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_REGION",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def mock_analysis_llm(monkeypatch):
    """
    Globally mock the Phase 1.5 analysis LLM seam so NO test ever makes a live
    billable call (the real env carries a Bedrock key). The auto-summary that
    fires when a run completes goes through this. Tests that need specific
    analysis behavior patch ``matrix_studio.analysis._acompletion`` themselves,
    which overrides this default within their scope.
    """
    import json as _json

    async def _fake_acompletion(messages, model=None, temperature=0.4, max_tokens=None):
        # Return a valid structured summary for summary prompts; a short reply
        # otherwise. Detection is heuristic on the system prompt.
        system = messages[0]["content"] if messages else ""
        if "STRUCTURED analysis" in system or "JSON object" in system:
            content = _json.dumps(
                {
                    "consensus": ["mocked consensus point"],
                    "dissenters": [{"speaker": "Mock", "position": "mocked dissent"}],
                    "key_ideas": ["mocked idea"],
                    "open_questions": ["mocked question"],
                    "overview": "Mocked analyst overview of the transcript.",
                }
            )
        else:
            content = "Mocked aside reply grounded in the transcript."
        return {
            "content": content,
            "tokens_in": 100,
            "tokens_out": 20,
            "cost_usd": 0.0012,
        }

    monkeypatch.setattr(
        "matrix_studio.analysis._acompletion", _fake_acompletion
    )


# --------------------------------------------------------------------------- #
# Storage backend for the whole suite (Phase 2/3 swap)
#
# The storage layer is DynamoDB + S3 + S3 Vectors now, so every test that touches
# storage needs a mocked account. `moto` rather than DynamoDB Local: no container, so
# the suite stays one `pytest` invocation.
#
# Not autouse. A fixture that started moto for all 866 tests would slow the ones that
# never touch storage and, worse, would silently intercept any boto3 call a test made
# for another reason — including a Bedrock call a test meant to assert was mocked
# elsewhere. Tests opt in by asking for `db`.
# --------------------------------------------------------------------------- #

from tests.support import (  # noqa: E402
    TEST_DATA_BUCKET,
    TEST_OWNER,
    TEST_TABLE_PREFIX,
    TEST_VECTOR_BUCKET,
    TEST_VECTOR_DIM,
    TEST_VECTOR_INDEX,
)


def _mock_credentials(monkeypatch):
    # ONE attempt, no retries, short timeouts.
    #
    # Without this, a test that reaches real AWS by accident — a missing
    # `aws_backend`, say — fails with `ExpiredTokenException`, which botocore treats
    # as retryable: it backs off and retries, so a single misconfigured test takes
    # tens of seconds instead of milliseconds. The first run after the storage swap
    # managed ~31 tests in 15 minutes for exactly that reason, and the symptom (a
    # slow suite) points nowhere near the cause (a fixture that is not applied).
    #
    # Failing fast makes a misconfigured test look like a misconfigured test.
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("AWS_RETRY_MODE", "standard")
    monkeypatch.setenv("AWS_METADATA_SERVICE_TIMEOUT", "1")
    monkeypatch.setenv("AWS_METADATA_SERVICE_NUM_ATTEMPTS", "1")
    for name, value in (
        ("AWS_ACCESS_KEY_ID", "testing"),
        ("AWS_SECRET_ACCESS_KEY", "testing"),
        ("AWS_SECURITY_TOKEN", "testing"),
        ("AWS_SESSION_TOKEN", "testing"),
        ("AWS_DEFAULT_REGION", "us-east-1"),
        ("AWS_REGION", "us-east-1"),
    ):
        monkeypatch.setenv(name, value)


def provision(prefix=TEST_TABLE_PREFIX, data_bucket=TEST_DATA_BUCKET,
              vector_bucket=TEST_VECTOR_BUCKET, vector_index=TEST_VECTOR_INDEX):
    """Create the tables, buckets and vector index the storage layer expects.

    Mirrors `infra/matrix_infra/stack.py`, including the two id GSIs and the
    non-filterable `text` metadata key. A fixture that quietly diverges from the real
    stack makes the whole suite pass against infrastructure that cannot serve it.
    """
    import boto3

    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    id_index = {"threads": "thread_id", "documents": "document_id"}
    for table in ("runs", "events", "snapshots", "summaries", "threads",
                  "thread-messages", "documents", "knowledge-bases", "kb-grants",
                  "connections"):
        attrs = [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ]
        extra = {}
        if table in id_index:
            attrs.append({"AttributeName": id_index[table], "AttributeType": "S"})
            extra["GlobalSecondaryIndexes"] = [{
                "IndexName": f"by-{id_index[table].replace('_', '-')}",
                "KeySchema": [{"AttributeName": id_index[table], "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            }]
        ddb.create_table(
            TableName=f"{prefix}-{table}",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=attrs,
            BillingMode="PAY_PER_REQUEST",
            **extra,
        )
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=data_bucket)
    vec = boto3.client("s3vectors", region_name="us-east-1")
    vec.create_vector_bucket(vectorBucketName=vector_bucket)
    vec.create_index(
        vectorBucketName=vector_bucket, indexName=vector_index,
        dataType="float32", dimension=TEST_VECTOR_DIM, distanceMetric="cosine",
        metadataConfiguration={"nonFilterableMetadataKeys": ["text"]},
    )


@pytest.fixture
def aws_backend(monkeypatch):
    """A mocked AWS account with the tables, buckets and vector index provisioned.

    Separate from `db` because two kinds of test need it: those that drive the storage
    layer directly, and those that build the FastAPI app — whose lifespan connects to
    DynamoDB and, without this, reaches real AWS. That failure is worth naming: it
    surfaces as `ExpiredTokenException` on a `Scan`, which reads like a credentials
    problem rather than a missing fixture.
    """
    from moto import mock_aws

    _mock_credentials(monkeypatch)
    monkeypatch.setenv("TABLE_PREFIX", TEST_TABLE_PREFIX)
    monkeypatch.setenv("DATA_BUCKET", TEST_DATA_BUCKET)
    monkeypatch.setenv("VECTOR_BUCKET", TEST_VECTOR_BUCKET)
    monkeypatch.setenv("VECTOR_INDEX", TEST_VECTOR_INDEX)
    # The sweep scans across tenants and is a process-start operation. Off here for the
    # same reason it is off on Lambda: several app instances in one test session would
    # each conclude the others' runs were orphaned.
    monkeypatch.setenv("STARTUP_SWEEP", "false")

    with mock_aws():
        provision()
        yield


@pytest.fixture
async def db(aws_backend):
    """A storage layer bound to `TEST_OWNER`.

    Bound, because that is how the application uses it: `for_owner(sub)` at the
    request boundary. A test proving a cross-tenant read is refused passes
    `owner_sub=` explicitly, which overrides the binding.
    """
    from matrix_studio.storage import Database

    store = Database(
        table_prefix=TEST_TABLE_PREFIX, bucket=TEST_DATA_BUCKET, region="us-east-1",
    )
    await store.connect()
    try:
        yield store.for_owner(TEST_OWNER)
    finally:
        await store.close()


@pytest.fixture(autouse=True)
def _fail_fast_on_real_aws(monkeypatch):
    """Make an accidental real-AWS call fail immediately, for EVERY test.

    Applies to tests that never asked for a mocked account, which is where the
    accident happens: a call escapes to the real endpoint, botocore retries the
    expired-token error with backoff, and the test hangs for tens of seconds. The
    suite then looks slow rather than misconfigured, which sent me looking at moto's
    provisioning cost (178 ms — not the problem) instead of at a missing fixture.

    Deliberately does not clear credentials: a test that legitimately mocks boto3
    still needs the environment to look plausible. This only removes the retrying.
    """
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("AWS_METADATA_SERVICE_TIMEOUT", "1")
    monkeypatch.setenv("AWS_METADATA_SERVICE_NUM_ATTEMPTS", "1")
