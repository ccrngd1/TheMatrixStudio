# SPDX-License-Identifier: Apache-2.0
"""
API package for TheMatrix Simulation Studio (Phase 1).

Exposes the FastAPI control-room server: REST endpoints, a WebSocket live event
stream, and static frontend serving — all over the unchanged Phase 0 engine.
"""

from matrix_studio.api.app import create_app

__all__ = ["create_app"]
