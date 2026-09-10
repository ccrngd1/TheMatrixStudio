# SPDX-License-Identifier: Apache-2.0
"""Shared test constants.

Its own module rather than living in `conftest.py`, because a test that needs one of
these has to *import* it, and importing `conftest` directly is both discouraged and
unreliable (it is loaded by pytest as a plugin, not as a module on the path).
"""

TEST_TABLE_PREFIX = "matrix-studio-test"
TEST_DATA_BUCKET = "matrix-studio-test-data"
TEST_VECTOR_BUCKET = "matrix-studio-test-vectors"
TEST_VECTOR_INDEX = "matrix-studio-test-chunks"

#: Dimension of the test vector index. Matches production (1024, measured — see
#: docs/EMBEDDING-DIMENSION-MEASUREMENT.md), because an index's dimension is immutable
#: after creation and a test fixture that disagreed with the real one would let a
#: width bug through.
TEST_VECTOR_DIM = 1024

#: The owner every storage fixture is bound to.
#:
#: A real-looking sub rather than `LOCAL_USER_SUB`, deliberately: if a test passes only
#: because the storage layer fell back to the local single-user identity, that is a
#: hole the suite should not paper over.
TEST_OWNER = "sub-test-0000-1111"
