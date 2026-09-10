# SPDX-License-Identifier: Apache-2.0
"""Stack configuration, read from CDK context so nothing is edited to deploy."""

from dataclasses import dataclass
from typing import Any, List, Optional

# Titan Text Embeddings v2 at 1024 dimensions. Measured, not chosen by default:
# see docs/EMBEDDING-DIMENSION-MEASUREMENT.md. 256 would be a 4x saving in vector
# storage and cost and holds recall on natural queries, but loses 0.117 recall@1
# on paraphrased ones (15 of 128 queries, monotonic across four metrics) — and
# paraphrase robustness is the entire reason vector retrieval was chosen over
# lexical.
#
# This number is effectively permanent: an S3 Vectors index's dimension is FIXED
# AT CREATION, so changing it means creating new indexes and re-embedding every
# document. That is why it was measured before any infrastructure was written.
EMBEDDING_DIMENSION = 1024

# Passage text rides along as vector metadata so a k-NN query returns ids, scores
# and the passages themselves — one round trip on the per-turn hot path instead of
# two. It must be declared NON-FILTERABLE at index creation: filterable metadata
# is the constrained kind (a 2 KB budget per vector), and nothing ever filters on
# the text. Only `owner_sub` and `kb_id` are filtered on, so they stay filterable.
NON_FILTERABLE_METADATA_KEYS = ["text"]


@dataclass(frozen=True)
class StackConfig:
    """Everything that differs between a demo deployment and a real one."""

    #: Prefix for resource names, so two deployments can share an account.
    prefix: str = "matrix-studio"

    #: Region for the text models and every stateful resource.
    region: Optional[str] = None

    #: Whether data stores survive `cdk destroy`.
    #:
    #: Defaults to True — RETAIN. A stack delete that silently takes every
    #: conversation with it is not a default anyone should get by omission, and the
    #: cost of the safe default is one orphaned bucket to clean up by hand. Set
    #: `-c retain_data=false` for a throwaway demo, deliberately.
    retain_data: bool = True

    #: Extra OAuth callback URLs (a local dev SPA, say). The CloudFront URL is
    #: always included.
    extra_callback_urls: Optional[List[str]] = None

    #: Email address for the admin-created first user. No user is created without
    #: one, because a pool with a hardcoded default account would be worse.
    admin_email: Optional[str] = None

    @staticmethod
    def from_context(node: Any) -> "StackConfig":
        """Build from `cdk.json` context and `-c key=value` overrides."""

        def get(key: str, default: Any = None) -> Any:
            value = node.try_get_context(key)
            return default if value is None else value

        def flag(key: str, default: bool) -> bool:
            value = node.try_get_context(key)
            if value is None:
                return default
            # `-c retain_data=false` arrives as the STRING "false", which is
            # truthy. Getting this wrong would turn a safety default into its
            # opposite while looking like it was read correctly.
            if isinstance(value, str):
                return value.strip().lower() not in ("false", "0", "no", "off")
            return bool(value)

        extra = get("extra_callback_urls") or []
        if isinstance(extra, str):
            extra = [u.strip() for u in extra.split(",") if u.strip()]

        return StackConfig(
            prefix=get("prefix", "matrix-studio"),
            region=get("region"),
            retain_data=flag("retain_data", True),
            extra_callback_urls=list(extra),
            admin_email=get("admin_email"),
        )
