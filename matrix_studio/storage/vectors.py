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
from typing import Any, Dict, List, Optional, Sequence

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


async def ensure_index_for_kb(store: Any, kb_id: str) -> bool:
    """Create a KB's vector index if absent, using **this store's** credentials.

    The credentials are the whole point, and getting them wrong is not a permissions error
    you can read off a traceback — it is a collection that exists and can never be written
    to. The tenant role is granted `s3vectors:GetIndex` and deliberately **not**
    `CreateIndex`, so this must be called with an UNSCOPED store (the API's or the CLI's own
    client), never one returned by `for_owner`.

    One implementation, because there are now two callers — the knowledge-base routes and
    research target allocation — and a second copy of "which credentials create an index"
    is the kind of duplication that gets one of them wrong silently.

    Returns False when there is no vector bucket configured, which is the local
    non-vector deployment rather than a failure: the caller reports it if it matters.
    """
    import os

    bucket = os.environ.get("VECTOR_BUCKET", "")
    if not bucket:
        return False
    await ensure_kb_index(
        store._vectors_client(), bucket, kb_index_name(kb_id, store.table_prefix)
    )
    return True


def index_parameters() -> dict:
    """The immutable three, for tests and for logging what an index was built with."""
    return {
        "dataType": DATA_TYPE,
        "dimension": EMBEDDING_DIMENSION,
        "distanceMetric": DISTANCE_METRIC,
        "nonFilterableMetadataKeys": list(NON_FILTERABLE_METADATA_KEYS),
    }


def merge_with_source_floor(
    rows: List[dict],
    k: int,
    *,
    key: str = "kb_id",
    floor: int = 1,
    prefer: Optional[Sequence[Any]] = None,
) -> List[dict]:
    """Best ``k`` rows overall, but reserving ``floor`` slots per source first.

    ## The failure this exists to prevent

    Run `2d2ac45b` bound six personas to six private collections plus one cast-wide
    collection holding the proposal under discussion. **All 24 turns retrieved from the
    shared collection and none from any persona's own**, with correct bindings and every
    index queried. A plain global top-k did it: at ``k=3`` the shared collection took
    every slot on every turn.

    The ranking was not wrong. Measured against those live indexes, a query about a
    persona's own material returns 3/3 from that persona's collection. But a turn's query
    is a term bag drawn from recent conversation text, and in a conversation *about* the
    proposal that query is proposal-shaped on **every** turn — so a collection whose
    wording mirrors the topic wins by construction rather than by relevance, permanently.

    The symptom was personas saying "I don't have a citation in front of me," which was
    true and read as careful behaviour rather than as retrieval failure. A binding that
    can never win a slot is indistinguishable from no binding at all.

    ## What this deliberately gives up

    Global top-k is **exact** — the union of per-index top-k contains the global top-k, so
    merging and trimming returns precisely what one index holding everything would have.
    A floor breaks that on purpose: it can promote a row that global rank would have cut,
    in exchange for every bound collection being able to contribute. That trade is only
    defensible because the alternative is a collection contributing *nothing, ever*.

    Set ``floor=0`` for the exact behaviour — `scripts/verify_kb_fanout_equivalence.py`
    does, because the exactness of the merge is still a property worth verifying
    separately from the policy layered on top.

    ## Degrading when there are more sources than slots

    With ``k=3`` and six sources, three sources get their best row and three get nothing;
    which three is decided by rank among the reserved rows, so the outcome stays
    deterministic and the best passage overall is never dropped. Reserving is capped at
    ``k`` in total, so this never returns more rows than asked for.

    ## ``prefer``: whose material wins when the slots run out

    Without it, reservation is egalitarian and ties break on distance — so the collection
    whose wording matches the query keeps winning, which is the same pressure the floor
    exists to resist, merely one level up. ``prefer`` names the sources that take
    precedence: their best rows are reserved before any other source's.

    Passed by `retrieve_for_turn` as **the speaker's own collections**, so a persona's
    private material is not outranked out of the prompt by a cast-wide collection that
    mirrors the topic. It matters only when reserved candidates exceed ``k`` — at ``k=1``
    with one shared and one private collection, or with several shared collections — and
    is a no-op otherwise.

    It cannot promote a passage past a threshold: `retrieve_for_turn` applies
    ``min_similarity`` and ``score_ratio`` BEFORE this selection, so a preferred source
    with nothing relevant contributes nothing rather than filling its slot with noise.
    """
    if k <= 0 or not rows:
        return []
    ordered = sorted(rows, key=lambda r: float(r.get("score") or 0.0))
    if floor <= 0:
        return ordered[:k]

    reserved: List[dict] = []
    per_source: Dict[Any, int] = {}
    for row in ordered:
        source = row.get(key)
        # A row with no source is not a collection. Run-scoped passages carry no
        # `kb_id`, and reserving a slot for them would change behaviour this bug says
        # nothing about — `test_the_trim_happens_after_the_merge_not_per_source` asserts
        # that a KB passage outranking a run passage wins, and it should keep winning.
        # They still compete for every unreserved slot.
        if source is None:
            continue
        if per_source.get(source, 0) < floor:
            per_source[source] = per_source.get(source, 0) + 1
            reserved.append(row)
    # Reserved rows are in distance order. Trimming to `k` therefore keeps the best
    # sources rather than whichever happened to be iterated first — unless `prefer` says
    # otherwise, in which case preferred sources are kept first and distance decides
    # within each group. A stable sort, so the ordering above survives inside a group.
    if prefer:
        preferred = set(prefer)
        reserved.sort(key=lambda r: r.get(key) not in preferred)
    reserved = reserved[:k]
    chosen = {id(row) for row in reserved}
    # Fill what is left by global rank, which is the ordinary behaviour for every slot
    # the floor did not claim.
    for row in ordered:
        if len(reserved) >= k:
            break
        if id(row) not in chosen:
            reserved.append(row)
            chosen.add(id(row))
    reserved.sort(key=lambda r: float(r.get("score") or 0.0))
    return reserved


#: The authority tier that gets a reserved slot. See `merge_with_authority_floor`.
CONTROLLING = "controlling"


def merge_with_authority_floor(
    rows: List[dict],
    k: int,
    *,
    floor: int = 1,
    key: str = "authority",
) -> List[dict]:
    """Best ``k`` rows overall, but reserving ``floor`` slots for controlling authority.

    ## Why this is not `merge_with_source_floor`

    That one reserves per COLLECTION, so every bound collection can contribute. This reserves
    per AUTHORITY, and the two do not substitute for each other: research ingests into the
    collection already bound at a scope, so a statute and thirty commentary chunks live in the
    *same* collection and compete for the *same* reserved slot. A source floor is satisfied the
    moment any one of them wins it, and the one that wins is whichever matches the query — which
    for a term bag drawn from conversation text is the commentary, because commentary is written
    in the conversation's vocabulary and a statute is not.

    That is the measured shape of the original source-floor bug (`2d2ac45b`: all 24 turns from the
    shared collection, none from a persona's own) reappearing one level down. The wording that
    mirrors the topic wins, permanently, and being right about relevance is exactly how it hides.

    ## What it buys, in the terms the feature is for

    Personas state what would change their mind — "a state statute defining specialty plans as
    regulated-only". Across five replicate runs none ever got it. If research finds that statute
    and it then loses every slot to a law-firm article discussing it, **the feature found the answer
    and hid it**, which is worse than not having searched: the corpus would show a controlling
    authority that no turn ever saw.

    ## What it gives up

    The same trade `merge_with_source_floor` makes, and stated the same way: global top-k is exact,
    and this breaks that on purpose. A controlling passage can be promoted past a commentary passage
    that ranked higher. Defensible only because the alternative is a controlling authority that can
    never win a slot, which is indistinguishable from not having found it.

    It cannot promote a passage past a threshold. `retrieve_for_turn` applies ``min_similarity`` and
    ``score_ratio`` first, so a statute with nothing relevant to say contributes nothing rather than
    filling its slot with noise — an irrelevant statute in every prompt would be a worse failure
    than a missing one, because it would read as the room ignoring the law.

    Rows with no ``authority`` — every passage written before the field existed, and every ordinary
    upload — are neither reserved nor penalised. They compete for unreserved slots exactly as now,
    so enabling this changes nothing for a run without research.
    """
    if k <= 0 or not rows:
        return []
    # Ascending distance: lower is nearer, matching `merge_with_source_floor`.
    ordered = sorted(rows, key=lambda r: float(r.get("score") or 0.0))
    if floor <= 0:
        return ordered[:k]

    reserved: List[dict] = []
    for row in ordered:
        if len(reserved) >= min(floor, k):
            break
        if str(row.get(key) or "") == CONTROLLING:
            reserved.append(row)

    chosen = {id(row) for row in reserved}
    for row in ordered:
        if len(reserved) >= k:
            break
        if id(row) not in chosen:
            reserved.append(row)
            chosen.add(id(row))
    reserved.sort(key=lambda r: float(r.get("score") or 0.0))
    return reserved


def apply_floors(
    rows: List[dict],
    k: int,
    *,
    kb_floor: int = 1,
    authority_floor: int = 0,
    prefer: Optional[Sequence[Any]] = None,
) -> List[dict]:
    """Both floors, composed. The one place their interaction is decided.

    ## Why not simply run them in sequence

    Running the authority floor over the full row set would discard the source floor's reservations
    and could leave a collection contributing nothing — the `2d2ac45b` bug, reintroduced by the fix
    for a different one. Running it over the source floor's OUTPUT would do nothing, because if a
    controlling passage were in that output there would be nothing to fix.

    So: the source floor selects, and **only if its selection contains no controlling authority** is
    one slot bought — by dropping the worst-ranked row and inserting the best controlling row. Every
    other reservation survives.

    ## Why the source floor wins the tie

    It costs at most one slot, and it spends it on the row global rank valued least. A collection that
    can never contribute is INVISIBLE — the original bug's symptom was personas saying "I don't have a
    citation in front of me", which read as caution — whereas a missing statute is visible in the
    corpus as an authority no turn cited. Given a choice of failures, take the one somebody notices.

    `authority_floor=0` is the default and is exactly the previous behaviour, because a run without
    research has nothing to reserve for and this must cost it nothing.
    """
    selected = merge_with_source_floor(rows, k, key="kb_id", floor=kb_floor, prefer=prefer)
    if authority_floor <= 0 or not selected:
        return selected

    def is_controlling(row: dict) -> bool:
        return str(row.get("authority") or "") == CONTROLLING

    if any(is_controlling(r) for r in selected):
        return selected

    chosen = {id(r) for r in selected}
    candidates = [
        r for r in sorted(rows, key=lambda r: float(r.get("score") or 0.0))
        if is_controlling(r) and id(r) not in chosen
    ]
    if not candidates:
        # Nothing controlling survived the similarity and score thresholds, which is the correct
        # outcome rather than a failure: an irrelevant statute in every prompt would read as the
        # room ignoring the law.
        return selected

    for _ in range(min(authority_floor, k, len(candidates))):
        # Only a SURPLUS row may pay — one whose collection has more than one passage in the
        # selection. Anything else is a per-collection reservation, and taking it is precisely the
        # `2d2ac45b` failure this composition exists to avoid.
        #
        # An earlier version dropped the worst-ranked row instead, which reads as fair and is not:
        # the worst-ranked row is usually the RESERVED one, because a collection wins its slot on
        # its own best passage rather than on global rank. On the measured shape it evicted a
        # persona's only passage to make room for a statute from the shared collection — trading an
        # invisible failure for a visible one, in the wrong direction. A test caught it.
        per_kb: Dict[Any, int] = {}
        for r in selected:
            per_kb[r.get("kb_id")] = per_kb.get(r.get("kb_id"), 0) + 1
        droppable = [
            r for r in selected
            if not is_controlling(r) and per_kb.get(r.get("kb_id"), 0) > 1
        ]
        if not droppable:
            # Every remaining slot is somebody's only contribution. The statute does not get one:
            # a collection contributing nothing at all is invisible, while a missing authority is
            # visible in the corpus as one no turn cited.
            break
        worst = max(droppable, key=lambda r: float(r.get("score") or 0.0))
        selected.remove(worst)
        selected.append(candidates.pop(0))

    selected.sort(key=lambda r: float(r.get("score") or 0.0))
    return selected
