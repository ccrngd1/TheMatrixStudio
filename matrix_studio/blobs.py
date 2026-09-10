# SPDX-License-Identifier: Apache-2.0
"""
Content-addressed storage for binary payloads that must not live in the event log.

Why this exists: `avatar.ready` carried its PNG as base64 **inside the event payload**.
Measured on real data, one avatar was 2,272,785 bytes against a 1,165-byte mean for every
other event type, and the same image also rode along in `AgentState.portrait` into every
snapshot — 99% of the largest snapshot in the database was one portrait. So a megabyte of
image sat in the append-only log that every replay and every `reconstruct_at_turn` reads,
and in a store whose item limit is 400 KB on the platform this is heading for.

The fix is to keep bytes out of both and put a **key** in the event instead.

**Content-addressed on purpose.** The key is derived from the bytes, which buys three
things without any bookkeeping:

- Regenerating an avatar yields a different key, so a URL built from it cache-busts itself
  and can be served `immutable`.
- Writing the same image twice is idempotent and costs one file.
- A key cannot be forged into a path: it is hex plus a known suffix, validated on read.

**This is the seam that becomes S3.** `put`/`get` over a key namespace is deliberately the
smallest interface that maps onto `PutObject`/`GetObject` with the key as the object key.
Nothing else about the caller changes when the backend does.
"""

import hashlib
import logging
import re
from pathlib import Path
from typing import Optional

from matrix_studio.settings import get_settings

logger = logging.getLogger(__name__)

# A key is `<namespace>/<64 hex chars><suffix>`. Anchored, and the only characters
# permitted are ones this module generates — so a key from an event payload or a URL can
# never traverse out of the blob root.
_KEY_RE = re.compile(r"^[a-z0-9_-]{1,32}/[0-9a-f]{64}\.[a-z0-9]{1,8}$")


def _root() -> Path:
    """Blob root, under the resolved data directory so it follows DATA_DIR."""
    return get_settings().resolved_data_dir / "blobs"


def make_key(data: bytes, *, namespace: str, suffix: str) -> str:
    """The key `data` will be stored under. Pure — useful for asserting in tests."""
    return f"{namespace}/{hashlib.sha256(data).hexdigest()}.{suffix.lstrip('.')}"


def put(data: bytes, *, namespace: str, suffix: str) -> str:
    """Store `data` and return its key. Idempotent: identical bytes reuse the key."""
    key = make_key(data, namespace=namespace, suffix=suffix)
    path = _root() / key
    if path.exists():
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp name in the same directory then rename, so a reader never sees a
    # half-written blob and two concurrent writers of the same bytes cannot interleave.
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return key


def get(key: str) -> Optional[bytes]:
    """Bytes for `key`, or None if the key is malformed or absent.

    Returning None rather than raising for a malformed key keeps the caller's error
    handling in one branch: a missing avatar and a nonsense key are both "no image",
    which is a case the UI already handles with a placeholder.
    """
    if not _KEY_RE.match(key or ""):
        logger.warning("Rejected malformed blob key: %r", key)
        return None
    path = _root() / key
    try:
        return path.read_bytes()
    except OSError:
        return None


def exists(key: str) -> bool:
    return bool(_KEY_RE.match(key or "")) and (_root() / key).is_file()
