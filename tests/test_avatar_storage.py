# SPDX-License-Identifier: Apache-2.0
"""
Avatars live in S3 under their owner, so the API can serve what a worker generated.

Found 2026-09-29 while capturing README screenshots: every avatar on the deployed stack was generated,
charged at $0.08, recorded — and served as a 404. `blobs.put` wrote to the generating worker's local
directory, which on Lambda is that function's own /tmp; the API is a different function and never saw
the file. Pinned here against the mocked account:

- a run's portraits are written to `avatars/{owner}/…` in the data bucket, and the key the events carry
  is unchanged;
- the route serves them to their owner, and another owner's key is not found — the ownership check the
  route's own docstring said it lacked;
- a key that is not an avatar key cannot name any other object.
"""

import base64
from unittest.mock import MagicMock, patch

import boto3
import pytest

from tests.conftest import TEST_DATA_BUCKET, TEST_OWNER

pytestmark = pytest.mark.asyncio

PNG = b"\x89PNG\r\n\x1a\n" + b"portrait bytes" * 10


async def test_a_portrait_is_stored_under_its_owner_and_only_its_owner_reads_it(db):
    key = await db.put_avatar(PNG)
    assert key.startswith("avatars/") and key.endswith(".png") and TEST_OWNER not in key
    objects = boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket=TEST_DATA_BUCKET)["Contents"]
    assert any(o["Key"] == f"avatars/{TEST_OWNER}/{key.split('/', 1)[1]}" for o in objects)
    assert await db.get_avatar(key) == PNG
    assert await db.get_avatar(key, owner_sub="someone-else") is None


async def test_a_key_that_is_not_an_avatar_key_names_nothing(db):
    from matrix_studio.storage.dynamo import StorageError

    for bad in ("snapshots/../x.png", "docs/" + "a" * 64 + ".png", "avatars/../../secret.png"):
        with pytest.raises(StorageError):
            await db.get_avatar(bad)


class _Resp:
    def __init__(self, content):
        self.choices = [MagicMock(message=MagicMock(content=content), finish_reason="stop")]
        self.usage = MagicMock(prompt_tokens=1, completion_tokens=1)
        self._hidden_params = {"response_cost": 0.0}


async def test_a_run_s_portraits_are_servable_from_s3(db):
    from matrix_studio.engine import run_simulation

    fake = base64.b64encode(PNG).decode()
    req = {"topic": "t", "config": {"max_messages": 1, "generate_avatars": True},
           "cast": [{"name": "Ada", "persona": "p", "goals": []}]}
    with patch("matrix_studio.engine.simulator.litellm.acompletion", side_effect=[_Resp("Ada"), _Resp("Hello.")]), \
         patch("matrix_studio.engine.simulator.generate_avatar", return_value=fake):
        await run_simulation(req, db=db, run_id="av1")
    ready = [e for e in await db.get_events("av1") if e["event_type"] == "avatar.ready"]
    payload = ready[0]["payload"]
    key = (payload if isinstance(payload, dict) else __import__("json").loads(payload))["portrait_key"]
    assert await db.get_avatar(key) == PNG, "the portrait the run recorded is in S3, where the API reads"
