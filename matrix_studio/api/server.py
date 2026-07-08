# SPDX-License-Identifier: Apache-2.0
"""Uvicorn launcher for the FastAPI control-room server (``serve`` subcommand)."""

import logging
from typing import Optional

import uvicorn

from matrix_studio.api.app import create_app
from matrix_studio.settings import get_settings

logger = logging.getLogger(__name__)


def serve(host: Optional[str] = None, port: Optional[int] = None) -> None:
    """
    Start the FastAPI app with uvicorn.

    Host/port default to the Phase 0 settings (``MATRIX_HOST``/``MATRIX_PORT``).
    """
    settings = get_settings()
    host = host or settings.matrix_host
    port = port or settings.matrix_port

    app = create_app()
    logger.info("Serving TheMatrix Simulation Studio on http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
