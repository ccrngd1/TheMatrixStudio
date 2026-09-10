# SPDX-License-Identifier: Apache-2.0
"""
Storage announces what it is pointed at, and WARNS when it holds nothing.

Motivating incident, from the SQLite era: the server was started from a
subdirectory, a relative ``data_dir`` resolved against the working directory, and
SQLite silently created a second empty database. The UI honestly reported no
previous conversations while the real runs sat untouched in another file — and
nothing in the logs distinguished that from data loss.

**The analogue on DynamoDB is exact**, which is why this file survived the port
rather than being deleted with it. A wrong ``TABLE_PREFIX``, region or account
points at tables that are absent or empty, and the symptom is identical: an empty
history and no error anywhere. So the remedy is the same — name what was opened,
and raise the log level when there is nothing in it.

What changed is only the mechanism: an absolute file path became a table prefix,
region and bucket, and "the file did not previously exist" became two
distinguishable cases — the table is missing, or the table is empty. Those are
different mistakes with different fixes, so they get different messages.
"""

import logging

import pytest

from matrix_studio.storage import Database
from tests.support import (
    TEST_DATA_BUCKET,
    TEST_OWNER,
    TEST_TABLE_PREFIX,
)


@pytest.fixture(autouse=True)
def _storage_backend(aws_backend):
    """These tests connect, so they need the mocked account."""


async def _connect(prefix=TEST_TABLE_PREFIX, bucket=TEST_DATA_BUCKET):
    store = Database(table_prefix=prefix, bucket=bucket, region="us-east-1")
    await store.connect()
    return store


@pytest.mark.asyncio
async def test_an_empty_store_warns_and_names_what_it_opened(caplog):
    """The whole point: "empty" must be loud, because it looks like data loss.

    At INFO an operator scanning a startup log sees nothing unusual and concludes the
    data is gone. WARNING is what makes them look at the configuration instead.
    """
    with caplog.at_level(logging.INFO):
        store = await _connect()
    await store.close()

    records = [r for r in caplog.records if "Storage:" in r.message]
    assert records, "connect() logged nothing about what it opened"
    warning = [r for r in records if r.levelno >= logging.WARNING]
    assert warning, f"an empty store must WARN, not inform: {[r.levelname for r in records]}"
    assert "EMPTY" in warning[0].getMessage()
    # And it must name what to check, or the warning is just alarming.
    assert "TABLE_PREFIX" in warning[0].getMessage()


@pytest.mark.asyncio
async def test_the_log_names_the_prefix_region_and_bucket(caplog):
    """All three can be wrong independently, so all three have to be stated.

    The SQLite version printed one absolute path because there was one thing to get
    wrong. There are three now, and an operator comparing "what I meant" against "what
    it opened" needs to see each of them.
    """
    with caplog.at_level(logging.INFO):
        store = await _connect()
    await store.close()

    message = " ".join(
        r.getMessage() for r in caplog.records if "Storage:" in r.message
    )
    assert TEST_TABLE_PREFIX in message
    assert "us-east-1" in message
    assert TEST_DATA_BUCKET in message


@pytest.mark.asyncio
async def test_a_store_holding_runs_informs_rather_than_warns(caplog):
    """A populated store is the normal case and must not cry wolf.

    Paired with the empty-store test deliberately: a change that warned unconditionally
    would satisfy that one and make the warning worthless, because an operator learns
    to ignore a line that always appears.
    """
    store = await _connect()
    await store.create_run(run_id="r1", topic="t", cast=[], owner_sub=TEST_OWNER)
    await store.close()

    # Only the REOPEN's records matter. The first connect saw an empty store and
    # correctly warned; leaving that in the capture made this assert against the wrong
    # line, which is the sort of thing that looks like a product bug for ten minutes.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        reopened = await _connect()
    await reopened.close()

    records = [r for r in caplog.records if "Storage:" in r.message]
    assert records
    assert all(r.levelno < logging.WARNING for r in records), (
        f"a populated store should not warn: {[r.getMessage() for r in records]}"
    )
    assert "run item" in records[0].getMessage(), records[0].getMessage()


@pytest.mark.asyncio
async def test_a_missing_table_is_a_different_message_from_an_empty_one(caplog):
    """"Wrong prefix" and "no data yet" are different mistakes with different fixes.

    The SQLite version could not tell them apart — a missing file simply became a new
    empty one, which is how the original incident happened. `DescribeTable`
    distinguishes them, so the log can too.
    """
    with caplog.at_level(logging.INFO):
        store = await _connect(prefix="a-prefix-that-does-not-exist")
    await store.close()

    warning = [
        r for r in caplog.records
        if "Storage:" in r.message and r.levelno >= logging.WARNING
    ]
    assert warning, "a missing table must warn"
    text = warning[0].getMessage()
    assert "DOES NOT EXIST" in text, text
    assert "EMPTY" not in text, "a missing table is not an empty one"
    assert "TABLE_PREFIX" in text


@pytest.mark.asyncio
async def test_an_unset_bucket_is_named_in_the_log(caplog):
    """A missing DATA_BUCKET fails only on the first snapshot write, which is late.

    Snapshots and document text go to S3, so an unset bucket is a store that accepts
    runs and events and then refuses the first checkpoint. Saying so at startup is the
    difference between a configuration error and a mysterious failure mid-run.
    """
    caplog.clear()
    with caplog.at_level(logging.INFO):
        store = await _connect(bucket="")
    await store.close()

    message = " ".join(
        r.getMessage() for r in caplog.records if "Storage:" in r.message
    )
    assert "UNSET" in message, message
