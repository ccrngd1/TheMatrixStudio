# SPDX-License-Identifier: Apache-2.0
"""Phase 6: the parameters of a vector index, and the one place one is created.

Three properties of an S3 Vectors index are **fixed at creation and can never be
changed**: its dimension, its distance metric, and which metadata keys are
non-filterable. Changing any of them means creating a new index and re-populating it —
which, once there is one index per knowledge base, means rebuilding a user's corpus.

So an index is created by **one function with one call site**, never by a
`create_index` call written at the point of need. A KB whose index was created with the
wrong dimension cannot be repaired.

## These constants are duplicated, and that is pinned rather than tolerated

`infra/matrix_infra/config.py` holds the same three values, because the CDK app and the
application are separate environments on purpose — `aws-cdk-lib` is ~60 MB and has no
business inside the Lambda image, so neither can import the other. Duplication is
therefore forced.

What is not forced is silent drift, which has already cost this project two bugs in the
status vocabulary. `tests/test_kb_index.py` parses the CDK copy and compares it to these,
so a change to one side without the other fails a test rather than producing a KB index
that the runtime and the infrastructure disagree about.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# Measured, not chosen by default — see `docs/EMBEDDING-DIMENSION-MEASUREMENT.md`. 256 to
# 1024 is +0.117 recall@1 on paraphrased queries, and paraphrase robustness is the entire
# reason vector retrieval was chosen over lexical.
EMBEDDING_DIMENSION = 1024

# Cosine, because Titan v2 returns unit-normalised vectors and `distance_to_cosine`
# assumes exactly that. Getting this wrong is not a degradation, it is a meaningless
# number: the shipped code applied sqlite-vec's L2 conversion to a cosine distance for
# months and the similarity floor was inert the whole time.
DISTANCE_METRIC = "cosine"

# float32 is the only data type S3 Vectors accepts.
DATA_TYPE = "float32"

# Passage text rides along as metadata so a k-NN query returns ids, scores AND passages
# in one round trip. It must be non-filterable: filterable metadata has a 2 KB per-vector
# budget and nothing ever filters on the text. `owner_sub` and `kb_id` are filtered on,
# so they stay filterable.
NON_FILTERABLE_METADATA_KEYS = ["text"]

# S3 Vectors index names: 3–63 characters, lowercase letters, digits and hyphens, and they
# must start and end alphanumerically. A KB id is a 12-char hex, so the composed name is
# well inside the limit — but the sanitiser exists because the prefix is operator-supplied
# and an invalid name fails at CREATE time, which under index-per-KB means a user's first
# upload fails rather than a deploy.
_INDEX_NAME_RE = re.compile(r"[^a-z0-9-]+")
MAX_INDEX_NAME = 63


def kb_index_name(kb_id: str, prefix: Optional[str] = None) -> str:
    """The index name for a KB. Deterministic, so it need not be stored.

    Derived rather than recorded on the KB row: a stored name can disagree with the index
    that actually exists, and there is no way to detect that except by querying and
    getting nothing — which looks exactly like an empty corpus.
    """
    prefix = prefix if prefix is not None else os.environ.get(
        "TABLE_PREFIX", "matrix-studio"
    )
    name = _INDEX_NAME_RE.sub("-", f"{prefix}-kb-{kb_id}".lower()).strip("-")
    if len(name) > MAX_INDEX_NAME:
        # Truncating the PREFIX rather than the id, because the id is what makes the
        # name unique — trimming it would collide two KBs onto one index, which is a
        # cross-tenant corpus leak rather than a naming annoyance.
        keep = MAX_INDEX_NAME - len(f"-kb-{kb_id}")
        name = f"{prefix[:max(1, keep)]}-kb-{kb_id}".lower().strip("-")
    return name


async def ensure_kb_index(
    client: Any, bucket: str, index_name: str
) -> bool:
    """Create the index if it is absent. Returns True if this call created it.

    Idempotent, because it is called on the ingest path rather than at deploy time: a
    second upload to the same KB must not fail, and a retried Step Functions state must
    not either. An "already exists" conflict is therefore success.

    The three immutable parameters are supplied here and nowhere else. That is the whole
    point of the function existing.
    """
    import asyncio

    def _create() -> bool:
        # Check first, then create.
        #
        # Not merely "catch the conflict": `moto` does NOT raise on a duplicate
        # `create_index` — it silently succeeds — so a conflict-only implementation is
        # unverifiable in the test suite and would be exercised for the first time in
        # production. Asking whether the index exists is testable and is the primary
        # path; the conflict catch below stays for the genuine race the check cannot
        # close (two concurrent ingests into a new KB).
        try:
            client.get_index(vectorBucketName=bucket, indexName=index_name)
            return False
        except Exception as exc:  # noqa: BLE001
            if "NotFound" not in type(exc).__name__:
                # A denied call or a missing BUCKET must surface. Swallowing it would
                # leave the KB with no index and every query returning nothing, which
                # is indistinguishable from an empty corpus.
                raise

        try:
            client.create_index(
                vectorBucketName=bucket,
                indexName=index_name,
                dataType=DATA_TYPE,
                dimension=EMBEDDING_DIMENSION,
                distanceMetric=DISTANCE_METRIC,
                metadataConfiguration={
                    "nonFilterableMetadataKeys": NON_FILTERABLE_METADATA_KEYS
                },
            )
            return True
        except Exception as exc:  # noqa: BLE001
            # S3 Vectors reports a duplicate as `ConflictException`. Matched on the name
            # rather than an isinstance check because botocore synthesises exception
            # classes per client, so there is no stable type to import — and matching the
            # MESSAGE would break on a wording change.
            name = type(exc).__name__
            if "Conflict" in name or "AlreadyExists" in name:
                return False
            raise

    created = await asyncio.to_thread(_create)
    if created:
        logger.info(
            "Created vector index %s (dimension %d, %s)",
            index_name, EMBEDDING_DIMENSION, DISTANCE_METRIC,
        )
    return created


def index_parameters() -> dict:
    """The immutable three, for tests and for logging what an index was built with."""
    return {
        "dataType": DATA_TYPE,
        "dimension": EMBEDDING_DIMENSION,
        "distanceMetric": DISTANCE_METRIC,
        "nonFilterableMetadataKeys": list(NON_FILTERABLE_METADATA_KEYS),
    }
