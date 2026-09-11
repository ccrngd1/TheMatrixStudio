# SPDX-License-Identifier: Apache-2.0
"""Phase 6 step 2: the per-KB vector index factory.

An index's dimension, distance metric and non-filterable metadata keys are **fixed at
creation and can never be changed**. Under index-per-KB that means a wrong value is not a
config mistake to correct but a user's corpus to rebuild — so the parameters live in one
place, are supplied by one function, and are pinned against the CDK's copy of them.
"""

import re
from pathlib import Path

import pytest

from matrix_studio.storage import vectors

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# The constants, pinned across the app/CDK boundary
# --------------------------------------------------------------------------- #


def _cdk_config() -> str:
    path = (
        Path(__file__).resolve().parent.parent
        / "infra" / "matrix_infra" / "config.py"
    )
    assert path.exists(), f"{path} is missing"
    return path.read_text()


async def test_the_dimension_matches_the_cdk_copy():
    """`aws-cdk-lib` is ~60 MB and has no business in the Lambda image, so the CDK app
    and the application cannot import each other and the constants are duplicated.

    Duplication is forced; silent drift is not, and it has already cost this project two
    bugs in the status vocabulary. A dimension mismatch would be worse than either: the
    infrastructure would create indexes at one width and the runtime would embed at
    another, so every ingest would fail at PutVectors — or, if the runtime's were smaller,
    succeed and produce nonsense.
    """
    m = re.search(r"^EMBEDDING_DIMENSION\s*=\s*(\d+)", _cdk_config(), re.M)
    assert m, "EMBEDDING_DIMENSION not found in the CDK config"
    assert int(m.group(1)) == vectors.EMBEDDING_DIMENSION


async def test_the_non_filterable_keys_match_the_cdk_copy():
    """These cannot be changed after creation, in either direction.

    A key that is filterable can never become non-filterable and vice versa, so a
    mismatch here bakes the wrong choice into every index the other side creates.
    """
    m = re.search(
        r"^NON_FILTERABLE_METADATA_KEYS\s*=\s*\[(.*?)\]", _cdk_config(), re.M | re.S
    )
    assert m, "NON_FILTERABLE_METADATA_KEYS not found in the CDK config"
    cdk = set(re.findall(r"[\"']([^\"']+)[\"']", m.group(1)))
    assert cdk == set(vectors.NON_FILTERABLE_METADATA_KEYS)


async def test_the_distance_metric_matches_the_cdk_copy():
    """Cosine. The CDK spells it as a keyword argument rather than a constant, so this
    reads the argument — a weaker pin than the other two, and better than none.

    Getting this wrong is not a degradation but a meaningless number: the shipped code
    applied sqlite-vec's L2 conversion to a cosine distance for months and the similarity
    floor was inert throughout.
    """
    stack = (
        Path(__file__).resolve().parent.parent
        / "infra" / "matrix_infra" / "stack.py"
    ).read_text()
    m = re.search(r"distance_metric\s*=\s*[\"'](\w+)[\"']", stack)
    assert m, "distance_metric not found in the CDK stack"
    assert m.group(1) == vectors.DISTANCE_METRIC


async def test_text_is_non_filterable_and_the_scoping_keys_are_not():
    """`text` must be non-filterable (filterable metadata has a 2 KB per-vector budget),
    and the keys retrieval actually filters on must NOT be — a filter on a
    non-filterable key is rejected, which would break every scoped query."""
    assert "text" in vectors.NON_FILTERABLE_METADATA_KEYS
    for filtered in ("owner_sub", "run_id", "kb_id", "persona_name", "cast_wide"):
        assert filtered not in vectors.NON_FILTERABLE_METADATA_KEYS


# --------------------------------------------------------------------------- #
# Index naming
# --------------------------------------------------------------------------- #


async def test_the_index_name_is_derived_not_stored():
    """Deterministic from the KB id, so it cannot disagree with the index that exists.

    A stored name that drifts produces queries against a nonexistent index, which returns
    nothing — indistinguishable from an empty corpus.
    """
    assert vectors.kb_index_name("abc123", prefix="matrix-studio") == \
        "matrix-studio-kb-abc123"
    assert vectors.kb_index_name("abc123", prefix="matrix-studio") == \
        vectors.kb_index_name("abc123", prefix="matrix-studio")


async def test_the_index_name_is_valid_for_the_service():
    """3–63 chars, lowercase alphanumerics and hyphens, alphanumeric at both ends.

    An invalid name fails at CREATE time — which under index-per-KB means a user's first
    upload fails, not a deploy.
    """
    for prefix in ("matrix-studio", "Matrix_Studio", "a.b c", "x" * 80):
        name = vectors.kb_index_name("deadbeef1234", prefix=prefix)
        assert 3 <= len(name) <= 63, f"{name!r} is {len(name)} chars"
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", name), name


async def test_a_long_prefix_truncates_the_prefix_not_the_kb_id():
    """The id is what makes the name unique.

    Trimming it would collide two KBs onto one index — a cross-tenant corpus leak, not a
    naming annoyance.
    """
    a = vectors.kb_index_name("aaaaaaaaaaaa", prefix="x" * 80)
    b = vectors.kb_index_name("bbbbbbbbbbbb", prefix="x" * 80)
    assert a != b, "two KBs collided onto one index name"
    assert a.endswith("aaaaaaaaaaaa") and b.endswith("bbbbbbbbbbbb")


async def test_the_prefix_comes_from_the_environment_by_default(monkeypatch):
    monkeypatch.setenv("TABLE_PREFIX", "other-stack")
    assert vectors.kb_index_name("kb1").startswith("other-stack-kb-")


# --------------------------------------------------------------------------- #
# Creation, against the mocked service
# --------------------------------------------------------------------------- #


async def test_creating_an_index_uses_the_immutable_parameters(db):
    """The reason the factory exists: these are supplied here and nowhere else.

    Asserted on what is SENT rather than read back, because `moto`'s `get_index` omits
    `metadataConfiguration` entirely — so a read-back assertion would silently skip the
    one parameter that cannot be corrected in either direction. What is sent is also the
    real subject: the factory's job is to supply these.
    """
    import boto3

    from tests.support import TEST_VECTOR_BUCKET

    client = boto3.client("s3vectors", region_name="us-east-1")
    name = vectors.kb_index_name("newkb", prefix="t")
    sent = {}
    real_create = client.create_index

    def spy(**kwargs):
        sent.update(kwargs)
        return real_create(**kwargs)

    client.create_index = spy
    assert await vectors.ensure_kb_index(client, TEST_VECTOR_BUCKET, name) is True

    assert sent["dimension"] == vectors.EMBEDDING_DIMENSION
    assert sent["distanceMetric"] == vectors.DISTANCE_METRIC
    assert sent["dataType"] == vectors.DATA_TYPE
    assert sent["metadataConfiguration"] == {
        "nonFilterableMetadataKeys": vectors.NON_FILTERABLE_METADATA_KEYS
    }
    # And it really exists afterwards, so the spy did not swallow the call.
    got = client.get_index(vectorBucketName=TEST_VECTOR_BUCKET, indexName=name)["index"]
    assert got["dimension"] == vectors.EMBEDDING_DIMENSION
    assert got["distanceMetric"] == vectors.DISTANCE_METRIC


async def test_creating_an_existing_index_is_success_not_failure(db):
    """Called on the INGEST path, not at deploy time. A second upload to the same KB
    must not fail, and neither must a retried Step Functions state.

    This is why the implementation CHECKS before creating rather than only catching a
    conflict: `moto` does not raise on a duplicate `create_index`, so a conflict-only
    version would pass no test here and be exercised first in production.
    """
    import boto3

    from tests.support import TEST_VECTOR_BUCKET

    client = boto3.client("s3vectors", region_name="us-east-1")
    name = vectors.kb_index_name("twice", prefix="t")
    assert await vectors.ensure_kb_index(client, TEST_VECTOR_BUCKET, name) is True
    assert await vectors.ensure_kb_index(client, TEST_VECTOR_BUCKET, name) is False


async def test_a_real_failure_still_raises(db):
    """Only a conflict is swallowed. A missing bucket or a denied call must surface —
    otherwise a KB silently has no index and every query returns nothing."""
    import boto3

    client = boto3.client("s3vectors", region_name="us-east-1")
    with pytest.raises(Exception):
        await vectors.ensure_kb_index(client, "no-such-vector-bucket", "t-kb-x")


async def test_a_non_notfound_error_from_the_existence_check_is_not_swallowed(db):
    """The EXISTENCE check must only swallow NotFound, and this is what proves it.

    The missing-bucket test above cannot: that fails at both `get_index` AND
    `create_index`, so it passes even when the first error is discarded. Mutation testing
    caught exactly that — swallowing every error from the check left the whole file green.

    An AccessDenied here is the case that matters in production: the function would
    proceed to create, that call would also be denied, and the operator would see the
    second error. Worse, if creation were somehow permitted while reading were not, the
    function would create an index that already exists.
    """
    import boto3

    from tests.support import TEST_VECTOR_BUCKET

    client = boto3.client("s3vectors", region_name="us-east-1")

    class Denied(Exception):
        pass

    def denied(**_kwargs):
        raise Denied("AccessDeniedException: not allowed to read indexes")

    client.get_index = denied
    with pytest.raises(Denied):
        await vectors.ensure_kb_index(client, TEST_VECTOR_BUCKET, "t-kb-denied")
