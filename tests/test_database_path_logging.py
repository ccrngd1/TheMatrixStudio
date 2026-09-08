# SPDX-License-Identifier: Apache-2.0
"""
The database announces which file it opened, and warns when it made a new one.

Motivating incident: the server was started from a subdirectory, a relative
``data_dir`` resolved against cwd, and SQLite silently created a second empty
database. The UI honestly reported no previous conversations while the real runs
sat untouched in another file. Nothing in the logs distinguished that from data
loss, so the startup log now states the absolute path either way and raises the
level to WARNING when the file did not previously exist.
"""

import logging

import pytest

from matrix_studio.storage import Database


async def _connected(path, caplog, level=logging.INFO):
    with caplog.at_level(level, logger="matrix_studio.storage.database"):
        db = Database(str(path))
        await db.connect()
        await db.close()
    return caplog.records


@pytest.mark.asyncio
async def test_new_database_warns_and_names_the_absolute_path(tmp_path, caplog):
    """A database created from nothing is a WARNING, not a routine info line."""
    target = tmp_path / "sub" / "matrix_studio.db"
    records = await _connected(target, caplog, logging.WARNING)

    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in records]
    message = warnings[0].getMessage()
    assert str(target.resolve()) in message
    assert "CREATED NEW AND EMPTY" in message
    # Points at the actual cause so the reader knows what to check.
    assert "DATA_DIR" in message


@pytest.mark.asyncio
async def test_reopening_logs_the_path_and_the_run_count(tmp_path, caplog):
    """
    The second open reports how many runs are really there.

    The count is what would have settled the incident in one line: 37 runs in the
    logged file means the data is fine and the *path* is wrong.
    """
    target = tmp_path / "matrix_studio.db"
    db = Database(str(target))
    await db.connect()
    await db.create_run(run_id="run-1", topic="does it persist?", cast=[{"name": "A"}])
    await db.create_run(run_id="run-2", topic="and again", cast=[{"name": "B"}])
    await db.close()

    caplog.clear()
    records = await _connected(target, caplog)

    messages = [r.getMessage() for r in records]
    matching = [m for m in messages if str(target.resolve()) in m]
    assert matching, messages
    assert "2 existing run(s)" in matching[0]
    # Not a warning: opening an existing database is the normal case.
    assert not [r for r in records if r.levelno >= logging.WARNING], messages


@pytest.mark.asyncio
async def test_the_logged_path_is_absolute_even_when_given_relatively(
    tmp_path, monkeypatch, caplog
):
    """
    A relative path is logged resolved.

    Logging back the same relative string the caller passed would have reprinted
    ``data/matrix_studio.db`` — exactly the ambiguity that hid the incident.
    """
    monkeypatch.chdir(tmp_path)
    records = await _connected("relative.db", caplog, logging.WARNING)

    message = records[0].getMessage()
    assert str((tmp_path / "relative.db").resolve()) in message
    assert "relative.db" in message
