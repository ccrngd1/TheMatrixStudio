# SPDX-License-Identifier: Apache-2.0
"""
Storage layer for TheMatrix Simulation Studio.

`Database` is DynamoDB + S3 + S3 Vectors.

The SQLite implementation it replaced lived in `database.py` and was retained for one
purpose: reading a pre-migration `.db` file. Those 38 runs were dropped as test data on
2026-09-13, so the reason expired and the file was deleted with it — 1,893 lines, plus the
`aiosqlite` dependency it was the only user of. Git history has it if a `.db` file ever
needs reading again.

The name stays `Database` rather than becoming `DynamoStorage` at every call site,
because the backend is not something a caller should have an opinion about: there is
one implementation, and the alias is what keeps that true at the import level too.
"""

from matrix_studio.storage.dynamo import (
    DuplicateNameError,
    DynamoStorage,
    DynamoStorage as Database,
    StorageError,
)

__all__ = ["Database", "DynamoStorage", "StorageError", "DuplicateNameError"]
