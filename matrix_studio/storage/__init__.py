# SPDX-License-Identifier: Apache-2.0
"""
Storage layer for TheMatrix Simulation Studio.

`Database` is DynamoDB + S3 + S3 Vectors. The SQLite implementation it replaced is
kept in `database.py` for one purpose only — reading a pre-migration `.db` file with
`scripts/` — and nothing in the application imports it.

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
