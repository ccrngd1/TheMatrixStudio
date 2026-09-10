# SPDX-License-Identifier: Apache-2.0
"""
Tenancy constants, shared by the storage layer and the web layer.

Its own module purely for layering: ``storage/database.py`` needs
``LOCAL_USER_SUB`` for its back-fill migration, and ``settings.py`` needs
``AUTH_MODES`` for its validator, but neither should have to import
``api/identity.py`` and drag FastAPI in behind it.
"""

# The identity every request carries in single-user mode, and what pre-tenancy
# rows are backfilled to on migration — those runs were created by the one person
# using the tool locally, so attributing them to that identity is what preserves
# their history.
#
# Deliberately not shaped like a real Cognito ``sub`` (which is a UUID). If this
# value ever appears in a multi-user deployment's data it means something ran
# without an authenticated identity, and it should be obvious at a glance rather
# than blend in with legitimate subs.
LOCAL_USER_SUB = "local-single-user"

#: Recognised values for ``Settings.auth_mode``.
AUTH_MODES = ("single-user", "jwt")
