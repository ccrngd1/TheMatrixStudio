# SPDX-License-Identifier: Apache-2.0
"""TheMatrix Simulation Studio - Multi-agent conversation simulator."""

# Single source of truth for the version reported at runtime. Read from the installed
# package metadata when available so it cannot drift from pyproject.toml — which it did:
# `--version` reported 0.1.0 through four releases because it carried its own literal.
# The fallback covers running from a checkout that was never installed.
try:  # pragma: no cover - trivial, and depends on install state
    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    __version__ = _pkg_version("matrix-sim-studio")
except Exception:  # noqa: BLE001 - PackageNotFoundError or a broken metadata dir
    __version__ = "0.6.0"
