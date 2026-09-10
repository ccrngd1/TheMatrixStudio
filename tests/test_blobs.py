# SPDX-License-Identifier: Apache-2.0
"""
Content-addressed blob storage, and the size property it exists to guarantee.

The motivating measurement: `avatar.ready` carried its PNG as base64 in the event payload —
2,272,785 bytes in one real instance against a 1,165-byte mean for every other event type —
and `AgentState.portrait` carried the same image into every snapshot, accounting for 99% of
the largest snapshot in a real database. So a megabyte of image sat in the append-only log
that every replay and every `reconstruct_at_turn` reads.

These tests pin the key format (a key reaches this module from an event payload and from a
URL query parameter, so it must not be able to escape the blob root), the content-addressing
that makes regeneration cache-bust itself, and the never-raises contract that keeps an
avatar from failing a run.
"""

import base64

import pytest

from matrix_studio import blobs
from matrix_studio.avatar import store_avatar

PNG = b"\x89PNG\r\n\x1a\nsome image bytes"


@pytest.fixture(autouse=True)
def blob_root(tmp_path, monkeypatch):
    """Point the blob root at a temp dir via the real settings path."""
    from matrix_studio.settings import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path), raising=False)
    return tmp_path / "blobs"


def test_put_then_get_round_trips():
    key = blobs.put(PNG, namespace="avatars", suffix="png")
    assert blobs.get(key) == PNG
    assert blobs.exists(key)


def test_key_is_content_addressed():
    """
    Identical bytes give one key; different bytes give different keys.

    This is what makes a URL built from the key cache-bust itself when an avatar is
    regenerated, and what makes storing the same image twice cost one file.
    """
    a = blobs.put(PNG, namespace="avatars", suffix="png")
    b = blobs.put(PNG, namespace="avatars", suffix="png")
    c = blobs.put(PNG + b"different", namespace="avatars", suffix="png")
    assert a == b
    assert a != c
    # And the pure form agrees with what was stored, so a caller can predict the key.
    assert blobs.make_key(PNG, namespace="avatars", suffix="png") == a


def test_key_shape_is_namespaced_hex():
    key = blobs.put(PNG, namespace="avatars", suffix="png")
    namespace, _, filename = key.partition("/")
    assert namespace == "avatars"
    stem, _, ext = filename.partition(".")
    assert ext == "png"
    assert len(stem) == 64 and all(ch in "0123456789abcdef" for ch in stem)


@pytest.mark.parametrize("bad", [
    "../../../etc/passwd",
    "avatars/../../etc/passwd",
    "avatars/..%2f..%2fetc",
    "/etc/passwd",
    "avatars/not-hex.png",
    "avatars/deadbeef.png",          # too short to be a sha256
    "avatars/" + "a" * 64,           # no suffix
    "avatars/" + "a" * 64 + ".png/x",
    "",
    None,
])
def test_malformed_keys_are_refused_not_resolved(bad):
    """
    A key arrives from an event payload and from a URL query parameter.

    Both are attacker-influenced in a deployed system, so the key must be validated
    rather than joined onto a path and hoped about. Returning None rather than raising
    keeps the caller's handling in one branch — a missing avatar and a hostile key are
    both "no image", which the UI already renders as a placeholder.
    """
    assert blobs.get(bad) is None
    assert blobs.exists(bad) is False


def test_get_returns_none_for_a_wellformed_but_absent_key():
    absent = blobs.make_key(b"never stored", namespace="avatars", suffix="png")
    assert blobs.get(absent) is None
    assert blobs.exists(absent) is False


def test_no_temp_files_accumulate(blob_root):
    blobs.put(PNG, namespace="avatars", suffix="png")
    blobs.put(PNG + b"x", namespace="avatars", suffix="png")
    stored = sorted(p.name for p in (blob_root / "avatars").iterdir())
    assert len(stored) == 2
    assert not [n for n in stored if n.startswith(".") or n.endswith(".tmp")]


def test_a_failed_write_leaves_nothing_readable(monkeypatch):
    """
    The atomicity guarantee, tested by its observable consequence.

    Writing straight to the final path means a crash mid-write leaves a truncated file at
    the key a reader will ask for — and because the key is a content hash, that truncated
    body would be served forever as if it were the real image, cached `immutable`. Writing
    to a temp name and renaming makes a failed write leave *no* blob, so the caller gets a
    clean miss and the placeholder.
    """
    real_write = type(blob_root_path()).write_bytes

    def half_then_fail(self, data):
        real_write(self, data[: len(data) // 2])
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_bytes", half_then_fail)

    key = blobs.make_key(PNG, namespace="avatars", suffix="png")
    with pytest.raises(OSError):
        blobs.put(PNG, namespace="avatars", suffix="png")

    assert blobs.get(key) is None, "a failed write must not leave a readable blob"
    assert blobs.exists(key) is False


def blob_root_path():
    """The Path type, indirected so monkeypatching Path.write_bytes cannot break it."""
    from pathlib import Path

    return Path(".")


# ---------------------------- the avatar seam ---------------------------- #

def test_store_avatar_decodes_base64_and_returns_a_key():
    key = store_avatar(base64.b64encode(PNG).decode())
    assert key and key.startswith("avatars/")
    assert blobs.get(key) == PNG


def test_store_avatar_passes_none_through():
    """Avatars disabled, no credentials, content filter, or an error — all give None."""
    assert store_avatar(None) is None
    assert store_avatar("") is None


def test_store_avatar_never_raises_on_bad_input():
    """
    An avatar is eye-candy and must never be able to fail a run.

    A non-base64 payload from the provider degrades to "no avatar", exactly as a
    generation failure does.
    """
    assert store_avatar("this is not base64!!") is None


def test_the_stored_reference_is_orders_of_magnitude_smaller_than_the_image():
    """
    The whole point, stated as a measurement rather than an intention.

    The real avatar that motivated this was 2,272,785 bytes of base64 in one event
    payload. A key is under a hundred.
    """
    big = b"\x89PNG\r\n\x1a\n" + b"x" * 2_000_000
    key = store_avatar(base64.b64encode(big).decode())
    assert len(key) < 100
    assert len(key) * 1000 < len(big), "a reference must be orders of magnitude smaller"
