# SPDX-License-Identifier: Apache-2.0
"""
Lambda entry point: the same FastAPI app, behind Mangum.

Mangum translates an API Gateway event into an ASGI call, so there is one
application and one set of routes whether it is served by uvicorn or by Lambda.
No parallel implementation, and nothing about a route can behave differently in
the two — which is the property that lets the 786 local tests remain the
specification for the deployed thing.

``api_gateway_base_path`` is not set: the HTTP API is configured with the default
stage, so paths arrive as the app already expects them. If a custom domain with a
base-path mapping is added later, that is the knob to set — otherwise every route
silently 404s under the prefix.

The one thing worth knowing: Mangum leaves the raw event in the ASGI scope under
``aws.event``, and ``api/identity.py`` reads the JWT authorizer's verified claims
from exactly there. That is the whole integration between authentication and this
application — API Gateway validates the token before the Lambda is invoked, so
nothing here verifies a signature.
"""

import logging
import os

from mangum import Mangum

from matrix_studio.api.app import create_app

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Built at import time so the cost lands in the init phase, which Lambda does not
# bill at the same rate and which is reused across invocations on a warm sandbox.
# Consequence to keep in mind: a failure here is an init failure affecting every
# request, not one 500.
app = create_app()

handler = Mangum(app, lifespan="auto")
"""ASGI adapter.

The lifespan must RUN, because that is where ``db.connect()`` happens. An earlier
version of this file set ``lifespan="off"`` — reasoning that the startup stale-run
sweep has no business firing on every cold start — and the result was that every
route touching storage raised ``AttributeError: 'NoneType' object has no attribute
'execute'`` and returned a bare 500. Caught by invoking this image through the
Lambda Runtime Interface Emulator; `/api/health` passed throughout, so the phase's
own acceptance check would not have found it.

The sweep concern was real, and is handled where it belongs: ``STARTUP_SWEEP=false``
in the function's environment. Its premise is "this is the only process", which is
false with many concurrent sandboxes — two cold starts would each mark the other's
in-flight run as interrupted.

**Phase 2 still needs to revisit this.** The lifespan opens a SQLite connection to
``/tmp``, which is per-sandbox and ephemeral: two concurrent invocations see two
different databases, and neither survives the sandbox. That is not a bug to fix
here — it is the reason Phase 2 replaces the storage layer with DynamoDB and S3.
Until then this deployment can log in and serve `/api/health`, and any run history
it appears to show is per-sandbox and will vanish.
"""
