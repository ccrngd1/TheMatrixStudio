#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
§8b end to end, through the deployed HTTP API with a real Cognito token:
a knowledge base is created, a document becomes retrievable, and a persona quotes it.

## Why this is separate from `verify_kb_grants.py`

That script proves the *authorisation* half — a binding is not permission, and a revoked
grant stops working at query time — and it does so by calling the storage layer with
Admin credentials. Everything it exercises would still pass if no route worked, because
the three KB defects found on 2026-09-13 were all in the layer it skips:

    dynamodb:Scan was not granted, so every KB route returned 500
    group grants never reached a listing
    nothing in the stack could create a KB's vector index

Each was invisible to 1,113 local tests *and* to 15/15 live grant checks, because `moto`
does not evaluate IAM and the grant checks never issued an HTTP request. So this one
starts at the outside: a Cognito user, a bearer token, CloudFront, the API Lambda's own
role, the tenant role it assumes, the state machine, and the transcript at the end.

**The identity is created and destroyed by this script.** A throwaway Cognito user with a
random password authenticates over SRP — the pool's default flow, so no client
configuration is widened to run this. The user, the collection, its index, the document
and the run are all removed in `cleanup`, whatever the verdict.

## What each assertion is protecting

The interesting ones are the two negatives:

- **Every passage must come from the document this script uploaded.** One shared index
  holds every run's chunks and there are hundreds of vectors in it from other work, so a
  filter that stopped filtering would show up here as a foreign `document_id` — and
  nowhere else, because a stray passage reads as ordinary retrieval.
- **A binding to a collection the caller cannot read must be refused at creation.** The
  boundary is the query-time re-check, but a create that accepts an unreadable binding
  produces a run that silently retrieves nothing, which reads as retrieval being bad.

Costs about $0.03: one 4-turn run plus the embedding call.

Usage:
    export AWS_REGION=us-east-1 TABLE_PREFIX=matrix-studio
    export DATA_BUCKET=... VECTOR_BUCKET=...
    python scripts/verify_kb_retrieval.py
    python scripts/verify_kb_retrieval.py --keep     # leave the fixtures for inspection
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3  # noqa: E402

# The document. Deliberately about something no model would answer this way from its own
# knowledge — invented state counts, so a persona reproducing "roughly thirty states"
# could only have read the passage.
SOURCE_TITLE = "PCR and CPLM: a state law survey"
SOURCE_TEXT = """
PROVIDER-CLIENT RELATIONSHIP (PCR): STATE REQUIREMENTS AND TELEMEDICINE

A valid PCR is the legal precondition for ordering a specialty plan. Every state
requires that the provider assume responsibility for clinical judgments about the
member's health, and that the client agree to follow the provider's instructions.

States differ sharply on how the relationship may be established. Roughly thirty states
require a physical examination of the member before a PCR exists; in those states a PCR
cannot be established by electronic means alone, and a bridge renewal issued
without an in-person examination is ordering without a valid PCR. Exactly six states
permit a PCR to be established through synchronous audio-video telemedicine.

CORPORATE PRACTICE OF LICENSED MEDICINE (CPLM)

Roughly half the states restrict who may own or control a licensed practice, on the
theory that a lay corporation's commercial interests must not influence clinical
judgment. Where CPLM applies, a retailer employing providers who make clinical
decisions about that retailer's own customers may be aiding the unlicensed practice of
licensed medicine.

RECORD-KEEPING AND DECLINE CRITERIA

Boards that have disciplined telemedicine ordering relied most often on the medical
record rather than on the modality: whether the record shows an assessment, and whether
the member was seen. Practices that document explicit decline criteria are in a
materially stronger position, because the record then shows a clinical judgment rather
than an approval process. Labwork older than six to twelve months is generally treated
as insufficient on its own for a patient whose condition is managed by plan.
""".strip()

CAST = [
    {
        "name": "Sarah Blackwood",
        "persona": (
            "You are a regulatory lawyer advising the company. When you make a legal "
            "claim you state what the source material says, and you say plainly when it "
            "does not answer a question. Two short paragraphs at most."
        ),
        "goals": ["keep the company out of a board complaint"],
    },
    {
        "name": "Jordan Reeves",
        "persona": (
            "You are the product lead who wants to launch, asking the lawyer direct "
            "questions about what the law actually requires. Two short paragraphs."
        ),
        "goals": ["find a launchable version"],
    },
]

TOPIC = (
    "Whether an online supplies retailer may have its in-house providers renew "
    "a lapsed specialty plan for a short bridge period. Ground every legal claim in the "
    "source material."
)

TURNS = 4
MIN_SIMILARITY = 0.15

results: List[Tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((ok, name, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


# --------------------------------------------------------------------------- #
# The deployment, and an identity in it
# --------------------------------------------------------------------------- #


def outputs(stack: str, region: str) -> Dict[str, str]:
    cfn = boto3.client("cloudformation", region_name=region)
    stacks = cfn.describe_stacks(StackName=stack)["Stacks"]
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


class Identity:
    """A throwaway Cognito user, authenticated over SRP.

    SRP rather than `ADMIN_USER_PASSWORD_AUTH` on purpose: the admin password flow is not
    enabled on this pool's client, and enabling it to run a verification would widen the
    deployment's auth surface permanently to check something once.
    """

    def __init__(self, pool_id: str, client_id: str, region: str) -> None:
        self.pool_id, self.client_id, self.region = pool_id, client_id, region
        self.username = f"verify-kb-{secrets.token_hex(4)}@example.invalid"
        # Cognito's default policy wants length, upper, lower, digit and symbol.
        self.password = f"Vk-{secrets.token_urlsafe(16)}-9aA!"
        self.sub: Optional[str] = None
        self.token: Optional[str] = None
        self._idp = boto3.client("cognito-idp", region_name=region)

    def create(self) -> str:
        created = self._idp.admin_create_user(
            UserPoolId=self.pool_id,
            Username=self.username,
            UserAttributes=[
                {"Name": "email", "Value": self.username},
                {"Name": "email_verified", "Value": "true"},
            ],
            # No email is sent: the address is `.invalid` by design, and a verification
            # run must not mail anybody.
            MessageAction="SUPPRESS",
        )
        self.sub = created["User"]["Username"]
        self._idp.admin_set_user_password(
            UserPoolId=self.pool_id, Username=self.username,
            Password=self.password, Permanent=True,
        )
        try:
            from pycognito import Cognito
        except ImportError:  # pragma: no cover - environment problem, not a failure mode
            raise SystemExit(
                "pycognito is required to sign in over SRP: pip install pycognito\n"
                "(it is in the `dev` extra — `pip install -e '.[dev]'`)"
            )
        user = Cognito(self.pool_id, self.client_id, username=self.username)
        user.authenticate(password=self.password)
        self.token = user.id_token
        return self.token

    def destroy(self) -> None:
        self._idp.admin_delete_user(UserPoolId=self.pool_id, Username=self.username)


class Api:
    """The deployed API, reached the way the SPA reaches it — through CloudFront."""

    def __init__(self, base: str, token: str) -> None:
        import httpx

        self.base = base.rstrip("/")
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {token}"}, timeout=120.0,
        )

    def call(self, method: str, path: str, **kwargs: Any):
        response = self._client.request(method, f"{self.base}{path}", **kwargs)
        # A route the API does not know falls through to the SPA's catch-all and answers
        # 200 text/html, so "did it work" cannot be read off the status alone.
        if "text/html" in response.headers.get("content-type", ""):
            raise AssertionError(
                f"{method} {path} returned the SPA, not JSON — the route does not exist"
            )
        return response

    def close(self) -> None:
        self._client.close()


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #


def verify_kb_creation(api: Api, vectors, bucket: str, prefix: str) -> Optional[str]:
    print("\n1. A collection is created, and its vector index exists")
    response = api.call(
        "POST", "/api/knowledge-bases",
        json={"name": f"verify-kb-{secrets.token_hex(3)}",
              "description": "created by scripts/verify_kb_retrieval.py"},
    )
    if not check("POST /api/knowledge-bases returns 201", response.status_code == 201,
                 f"{response.status_code}: {response.text[:200]}"):
        return None
    kb_id = str(response.json()["id"])

    from matrix_studio.storage.vectors import kb_index_name

    # Asserted against the SERVICE rather than inferred from the 201. Index creation is
    # the one KB operation the tenant role may not perform, so it runs on the API
    # Lambda's own role — an asymmetry no local test can see, and the reason a KB route
    # once returned 500 with everything green.
    index = kb_index_name(kb_id, prefix)
    try:
        vectors.get_index(vectorBucketName=bucket, indexName=index)
        check(f"its index {index} exists", True)
    except Exception as exc:  # noqa: BLE001
        check(f"its index {index} exists", False, str(exc)[:200])
        return kb_id
    return kb_id


def verify_upload(api: Api, kb_id: str) -> Optional[str]:
    print("\n2. A document is stored AND embedded, in one call")
    response = api.call(
        "POST", f"/api/knowledge-bases/{kb_id}/documents",
        json={"title": SOURCE_TITLE, "text": SOURCE_TEXT},
    )
    if not check("POST …/documents returns 201", response.status_code == 201,
                 f"{response.status_code}: {response.text[:300]}"):
        return None
    body = response.json()
    check("chunks were embedded", int(body.get("embedded") or 0) > 0,
          f"{body.get('embedded')} chunk(s) on {body.get('model')}")
    # Embedding is inline for a reason: a document that is stored but unembedded is listed
    # in the collection and permanently unretrievable, which looks like retrieval being
    # bad rather than an upload having half-failed.
    check("the embedding cost was reported", float(body.get("cost_usd") or 0) > 0,
          f"${body.get('cost_usd')}")

    listing = api.call("GET", f"/api/knowledge-bases/{kb_id}").json()
    check("the collection lists exactly one document",
          listing.get("document_count") == 1, str(listing.get("document_count")))
    return str(body["document_id"])


def verify_binding_is_checked(api: Api) -> None:
    print("\n3. A binding to a collection this caller cannot read is refused")
    response = api.call("POST", "/api/runs", json={
        "topic": TOPIC, "cast": CAST,
        "config": {"max_messages": 2, "generate_avatars": False,
                   "knowledge_bases": ["deadbeefdead"],
                   "retrieval": {"enabled": True}},
    })
    # 422, and nothing created. The query-time re-check is the real boundary — a grant can
    # be revoked after a run exists — but a run created with a bad binding retrieves
    # nothing, silently, and that reads as an empty collection rather than a lost binding.
    check("POST /api/runs refuses an unreadable binding with 422",
          response.status_code == 422, f"{response.status_code}: {response.text[:160]}")


def verify_retrieval_in_a_run(
    api: Api, kb_id: str, document_id: str, timeout_s: int
) -> Optional[str]:
    print(f"\n4. A {TURNS}-turn run bound to the collection retrieves from it")
    response = api.call("POST", "/api/runs", json={
        "topic": TOPIC,
        "name": f"kb-retrieval-check-{secrets.token_hex(3)}",
        "cast": CAST,
        "config": {
            "max_messages": TURNS,
            "generate_avatars": False,
            "knowledge_bases": [kb_id],
            "retrieval": {"enabled": True, "k": 3, "max_chars": 1200, "mode": "vector",
                          "min_similarity": MIN_SIMILARITY},
        },
    })
    if not check("POST /api/runs returns the run", response.status_code in (200, 201),
                 f"{response.status_code}: {response.text[:300]}"):
        return None
    run_id = str(response.json()["run_id"])
    print(f"   run {run_id}", flush=True)

    deadline = time.time() + timeout_s
    status, events = "pending", []
    while time.time() < deadline:
        time.sleep(8)
        detail = api.call("GET", f"/api/runs/{run_id}").json()
        status = str(detail.get("status"))
        if status in ("complete", "stopped", "capped", "failed", "interrupted"):
            break
        print(f"    … {status}, {detail.get('turn_count')} turn(s)", flush=True)
    payload = api.call("GET", f"/api/runs/{run_id}/events").json()
    events = payload["events"] if isinstance(payload, dict) else payload

    if not check(f"the run completed with {TURNS} turns", status == "complete", status):
        return run_id

    retrievals = [_payload(e) for e in events if e["event_type"] == "document.retrieved"]
    check(f"every turn retrieved ({TURNS} document.retrieved events)",
          len(retrievals) == TURNS, f"{len(retrievals)} of {TURNS}")
    check("each retrieval returned passages",
          all(r.get("passages") for r in retrievals),
          f"{sum(1 for r in retrievals if not r.get('passages'))} empty")

    passages = [p for r in retrievals for p in (r.get("passages") or [])]
    # THE filter assertion. One shared index holds every run's chunks, so a filter that
    # stopped filtering shows up here as a foreign document_id and nowhere else — a stray
    # passage reads as ordinary retrieval.
    foreign = sorted({str(p.get("document_id")) for p in passages} - {document_id})
    check("every passage came from the uploaded document", not foreign,
          f"foreign document ids: {', '.join(foreign)}" if foreign else "")
    check("every passage cleared the similarity floor",
          all(float(p.get("score") or 0) >= MIN_SIMILARITY for p in passages),
          f"min score {min((float(p.get('score') or 0) for p in passages), default=0):.3f}")
    check("the passages are titled",
          all(p.get("title") == SOURCE_TITLE for p in passages))

    # The dossier is where an operator checks what a persona actually drew on, and it
    # reads the same events rather than claiming anything — so a route that reported
    # nothing here would make a working retrieval look broken.
    dossier = api.call(
        "GET", f"/api/runs/{run_id}/agents/{CAST[0]['name'].replace(' ', '%20')}/dossier"
    ).json()
    check("the persona's dossier reports its retrievals",
          len(dossier.get("document_retrievals") or []) > 0,
          f"{len(dossier.get('document_retrievals') or [])} turn(s)")

    # Grounding. Not a quality judgment — the numbers in the source are invented, so a
    # transcript containing one is evidence the passage reached the prompt rather than
    # the model answering from its own knowledge.
    transcript = " ".join(
        _payload(e).get("message", "") for e in events
        if e["event_type"] == "agent.response"
    ).lower()
    quoted = [m for m in ("thirty states", "six states", "pcr", "cpvm", "decline criteria")
              if m in transcript]
    check("the transcript quotes the source material", bool(quoted),
          ", ".join(quoted) or "nothing from the document appears in the transcript")
    return run_id


def _payload(event: Dict[str, Any]) -> Dict[str, Any]:
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except ValueError:
            return {}
    return payload


# --------------------------------------------------------------------------- #
# Cleanup
# --------------------------------------------------------------------------- #


async def cleanup(
    *,
    region: str,
    prefix: str,
    bucket: str,
    vector_bucket: str,
    kb_id: Optional[str],
    document_id: Optional[str],
    run_id: Optional[str],
    owner_sub: Optional[str],
    vectors,
) -> None:
    """Remove everything this run created. Loud on failure, and never fatal.

    The vector INDEX is the one that matters. There is no delete-collection route, so an
    index left behind is permanent, and the ceiling is 10,000 per bucket — a check that
    leaked one per invocation would eventually be the thing that broke knowledge bases.
    """
    print("\nCleaning up")
    from matrix_studio.storage import Database
    from matrix_studio.storage.dynamo import _kb_pk, _name_sk, _run_sk, _user_pk
    from matrix_studio.storage.vectors import kb_index_name

    db = Database(table_prefix=prefix, bucket=bucket, region=region)
    await db.connect()
    try:
        if kb_id and document_id:
            # Through the storage layer rather than the route: the document's vectors and
            # its S3 body go with it, and by this point the token may have expired.
            try:
                await db.for_owner(owner_sub or "").delete_kb_document(kb_id, document_id)
                print(f"  removed document {document_id}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! document {document_id}: {exc}")
        if run_id and owner_sub:
            from scripts.delete_runs import delete_one  # noqa: PLC0415

            run = await db.for_owner(owner_sub).get_run(run_id)
            if run:
                counts = await delete_one(db, {**run, "owner_sub": owner_sub}, True)
                print(f"  removed run {run_id}: {counts}")
        if kb_id:
            try:
                await db._call(
                    db._table("knowledge-bases").delete_item,
                    Key={"pk": _kb_pk(kb_id), "sk": "META"},
                )
                print(f"  removed collection {kb_id}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! collection {kb_id}: {exc}")
            index = kb_index_name(kb_id, prefix)
            try:
                vectors.delete_index(vectorBucketName=vector_bucket, indexName=index)
                print(f"  removed index {index}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! index {index} MUST be removed by hand: {exc}")
    finally:
        await db.close()


# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", default="matrix-studio-stack")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--keep", action="store_true",
                        help="leave the user, collection and run in place")
    args = parser.parse_args()

    prefix = os.environ.get("TABLE_PREFIX", "matrix-studio")
    data_bucket = os.environ.get("DATA_BUCKET")
    if not data_bucket:
        raise SystemExit("DATA_BUCKET must be set (see the stack outputs).")

    out = outputs(args.stack, args.region)
    base = out["SpaUrl"]
    vector_bucket = out["VectorBucketName"]
    vectors = boto3.client("s3vectors", region_name=args.region)

    print(f"§8b end to end against {base}")
    identity = Identity(out["UserPoolId"], out["UserPoolClientId"], args.region)
    print(f"  identity {identity.username} ({identity.sub or 'creating…'})")
    token = identity.create()
    api = Api(base, token)

    kb_id = document_id = run_id = None
    try:
        kb_id = verify_kb_creation(api, vectors, vector_bucket, prefix)
        if kb_id:
            document_id = verify_upload(api, kb_id)
        verify_binding_is_checked(api)
        if kb_id and document_id:
            run_id = verify_retrieval_in_a_run(api, kb_id, document_id, args.timeout)
    finally:
        api.close()
        if args.keep:
            print(f"\n--keep: left user {identity.username}, collection {kb_id}, "
                  f"run {run_id}. The vector index must be deleted by hand.")
        else:
            asyncio.run(cleanup(
                region=args.region, prefix=prefix, bucket=data_bucket,
                vector_bucket=vector_bucket, kb_id=kb_id, document_id=document_id,
                run_id=run_id, owner_sub=identity.sub, vectors=vectors,
            ))
            identity.destroy()
            print(f"  removed identity {identity.username}")

    failed = [name for ok, name, _ in results if not ok]
    print("\n" + "=" * 72)
    print(f"  {len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        return 1
    if not results:
        print("  Nothing ran. That is a failure, not a pass.")
        return 1
    print("  KB creation, embedding, binding validation and retrieval-in-a-run all work "
          "through the deployed API.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
