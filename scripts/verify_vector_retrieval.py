#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Verify S3 Vectors k-NN and its metadata filter against the real service.

`moto` implements `CreateIndex`, `PutVectors` and `ListVectors` but **not
`QueryVectors`** — it falls through to real AWS and fails on the credentials. So the
unit suite can assert everything about what is *written* (keys, scoping metadata,
passage text) and nothing about whether a query honours the filter.

That gap matters more here than it would elsewhere. Phase 3 uses ONE shared vector
index with a metadata filter, because index-per-run would cap the install at 10,000
conversations (§8b's index-per-KB ceiling does not transfer to runs). So until Phase 6
introduces per-KB indexes and an IAM boundary, **this filter is the entire isolation
mechanism for retrieval** — and "the filter works" is an assumption about AWS that
should be checked rather than believed.

Usage:
    AWS_PROFILE=... python scripts/verify_vector_retrieval.py

Exits non-zero on any failure. Cleans up the vectors it writes.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

USER_A = "verify-vec-aaaa"
USER_B = "verify-vec-bbbb"
RUN_1 = "verify-vec-run-1"
RUN_2 = "verify-vec-run-2"


def unit(*components: float) -> List[float]:
    """A unit vector padded to the index dimension.

    Unit length matters: the index is cosine, and `retrieval.apply_similarity_floor`
    converts distance to cosine similarity only for unit vectors (`is_unit_norm`
    guards it and skips the floor otherwise). A non-unit fixture would make the
    distances here unrepresentative of what the engine produces.
    """
    import math

    dim = int(os.environ.get("VERIFY_DIM", "1024"))
    vec = [0.0] * dim
    for i, value in enumerate(components):
        vec[i] = value
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", default="matrix-studio-stack")
    parser.add_argument("--region", default=None)
    args = parser.parse_args()

    import boto3

    region = (args.region or os.environ.get("AWS_REGION")
              or os.environ.get("AWS_DEFAULT_REGION"))
    if not region:
        print("FAIL: no region. Pass --region or set AWS_REGION.")
        return 1

    cfn = boto3.client("cloudformation", region_name=region)
    out = {
        o["OutputKey"]: o["OutputValue"]
        for o in cfn.describe_stacks(StackName=args.stack)["Stacks"][0]["Outputs"]
    }
    bucket = out.get("VectorBucketName")
    if not bucket:
        print("FAIL: the stack has no VectorBucketName output.")
        return 1

    client = boto3.client("s3vectors", region_name=region)
    indexes = client.list_indexes(vectorBucketName=bucket).get("indexes", [])
    if not indexes:
        print(f"FAIL: no index in {bucket}.")
        return 1
    index = indexes[0]["indexName"]
    described = client.get_index(vectorBucketName=bucket, indexName=index)["index"]
    dim = int(described["dimension"])
    os.environ["VERIFY_DIM"] = str(dim)

    print(f"vector bucket {bucket} · index {index} · dimension {dim} · "
          f"{described['distanceMetric']}")
    print(f"non-filterable keys: "
          f"{described.get('metadataConfiguration', {}).get('nonFilterableMetadataKeys')}\n")

    failures: List[str] = []

    def check(name: str, ok: bool, detail: str = "", hint: str = "") -> None:
        """`detail` is context shown either way; `hint` explains a FAILURE only.

        Kept separate because the first version printed a failure explanation next to
        a PASS — "PASS  a persona sees cast-wide passages — the cast_wide arm of the
        filter is not working" — which reads as a contradiction and is exactly the
        wrong thing for a verification script to be ambiguous about.
        """
        suffix = f" — {detail}" if detail else ""
        if not ok and hint:
            suffix += f" — {hint}"
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{suffix}")
        if not ok:
            failures.append(name)

    # Four vectors: A/run1 own-persona, A/run1 cast-wide, A/run2, and B's. All point
    # in the SAME direction, so the only thing that can separate them is the filter —
    # if a check passes because of distance rather than scoping, it proves nothing.
    same_direction = unit(1.0)
    fixtures = [
        (f"{USER_A}:r1:dana", {"owner_sub": USER_A, "run_id": RUN_1,
                               "persona_name": "Dana", "document_id": "d1",
                               "ordinal": 0, "text": "Dana's own passage."}),
        (f"{USER_A}:r1:all", {"owner_sub": USER_A, "run_id": RUN_1,
                              "cast_wide": True, "document_id": "d2",
                              "ordinal": 0, "text": "A cast-wide passage."}),
        (f"{USER_A}:r1:marcus", {"owner_sub": USER_A, "run_id": RUN_1,
                                 "persona_name": "Marcus", "document_id": "d3",
                                 "ordinal": 0, "text": "Marcus's own passage."}),
        (f"{USER_A}:r2:x", {"owner_sub": USER_A, "run_id": RUN_2,
                            "document_id": "d4", "ordinal": 0,
                            "text": "Another run's passage."}),
        (f"{USER_B}:r1:x", {"owner_sub": USER_B, "run_id": RUN_1,
                            "document_id": "d5", "ordinal": 0,
                            "text": "Another tenant's passage."}),
    ]
    client.put_vectors(
        vectorBucketName=bucket, indexName=index,
        vectors=[{"key": k, "data": {"float32": same_direction}, "metadata": m}
                 for k, m in fixtures],
    )

    from matrix_studio.storage.dynamo import DynamoStorage

    store = DynamoStorage(region=region)

    def query(run_id: str, persona, owner: str, k: int = 10):
        return client.query_vectors(
            vectorBucketName=bucket, indexName=index,
            queryVector={"float32": same_direction}, topK=k,
            filter=store._slice_filter(run_id, persona, owner),
            returnMetadata=True, returnDistance=True,
        ).get("vectors", [])

    def texts(hits) -> set:
        return {h["metadata"].get("text", "") for h in hits}

    print("QueryVectors + metadata filter")

    # Positive first: a filter that returns nothing makes every negative vacuous.
    own = texts(query(RUN_1, None, USER_A))
    check("A sees its own run's passages", len(own) >= 3, f"{len(own)} found")
    if not own:
        print("\nThe positive case is empty, so the exclusions below prove nothing.")
        _cleanup(client, bucket, index, [k for k, _ in fixtures])
        return 1

    check("passage text comes back inline",
          any("Dana's own passage." == t for t in own))
    check("another TENANT's passage is excluded",
          "Another tenant's passage." not in own)
    check("another RUN's passage is excluded",
          "Another run's passage." not in own)

    dana = texts(query(RUN_1, "Dana", USER_A))
    check("a persona sees its own passage", "Dana's own passage." in dana)
    check("a persona sees cast-wide passages", "A cast-wide passage." in dana,
          hint="the cast_wide arm of the filter is not working")
    check("a persona does NOT see another persona's",
          "Marcus's own passage." not in dana)

    b_view = texts(query(RUN_1, None, USER_B))
    check("B sees only its own", b_view == {"Another tenant's passage."},
          str(b_view))

    _cleanup(client, bucket, index, [k for k, _ in fixtures])

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures)}")
        print("Retrieval scoping does not hold. With one shared index this filter is "
              "the whole isolation mechanism, so a failure here is a cross-tenant "
              "disclosure in the retrieval path.")
        return 1
    print("All checks passed: the metadata filter scopes retrieval by tenant, run "
          "and persona, and passages return inline.")
    return 0


def _cleanup(client, bucket: str, index: str, keys: List[str]) -> None:
    try:
        client.delete_vectors(vectorBucketName=bucket, indexName=index, keys=keys)
    except Exception as exc:  # noqa: BLE001
        print(f"  note: cleanup failed ({exc})", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
