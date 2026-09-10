# SPDX-License-Identifier: Apache-2.0
"""
Who is making this request.

Every run belongs to exactly one user, identified by the stable subject claim
Cognito issues (``sub``) — not by email or username, both of which an admin can
change under an account, which would orphan that user's history.

This module is the *only* place the identity is derived, so there is one thing to
get right and one thing to test. Routes take it as a FastAPI dependency and pass
it to the storage layer, which scopes the query. The scoping is not the route's
job: see ``Database.get_run_by_ref``, where ``owner_sub`` is a required
keyword-only argument precisely so that a route which forgets it fails with a
``TypeError`` at call time rather than serving somebody else's conversation.

**Phase 0.2 scope.** The domain model, the storage scoping and the route
authorisation are real and complete here. What is deliberately *not* here is
authentication: nothing verifies a signature yet. In ``single-user`` mode — the
default, and what the local tool has always effectively been — every request is
the same user. Phase 1 adds the ``jwt`` mode with a real Cognito pool in front,
and the only thing that changes is where ``sub`` comes from.

That split is intentional. Getting ownership into the schema and onto every route
is the part that is expensive to retrofit and easy to get subtly wrong; verifying
a JWT is a well-trodden problem that API Gateway solves for us.
"""

from typing import Any, Dict, Optional, Union

from fastapi import HTTPException, Request, WebSocket

# Re-exported so callers have one obvious import for anything tenancy-related.
# They live in `matrix_studio.tenancy` so the storage layer and `settings` can use
# them without importing FastAPI.
from matrix_studio.tenancy import AUTH_MODES, LOCAL_USER_SUB  # noqa: F401


def claims_from_scope(
    connection: Union[Request, WebSocket]
) -> Optional[Dict[str, Any]]:
    """
    JWT claims injected by an API Gateway authorizer, if this request came through one.

    API Gateway validates the token (signature, issuer, audience, expiry) *before*
    invoking the Lambda and passes the verified claims in the request context, so
    by the time they reach here they are trustworthy. Mangum surfaces the raw
    event under ``aws.event`` in the ASGI scope.

    Takes either an HTTP request or a WebSocket because both are ASGI connections
    carrying the same scope, and the WS route needs the identity for exactly the
    same reason the HTTP routes do.

    Returns None when the request did not arrive through an authorizer — running
    under plain uvicorn, for instance — which is the single-user case.
    """
    event = connection.scope.get("aws.event")
    if not isinstance(event, dict):
        return None
    authorizer = (
        event.get("requestContext", {}).get("authorizer", {})
        if isinstance(event.get("requestContext"), dict)
        else {}
    )
    if not isinstance(authorizer, dict):
        return None
    # HTTP API (payload v2.0) nests JWT claims one level down; REST API custom
    # authorizers put them at the top. Accept both rather than guess.
    jwt = authorizer.get("jwt")
    if isinstance(jwt, dict) and isinstance(jwt.get("claims"), dict):
        return jwt["claims"]
    if isinstance(authorizer.get("claims"), dict):
        return authorizer["claims"]
    return None


def _resolve(connection: Union[Request, WebSocket]) -> str:
    """
    Identity resolution, shared by the HTTP and WebSocket dependencies.

    Order matters: verified claims win whenever they are present, even in
    single-user mode. A deployment that grows an authorizer must not keep
    attributing every request to the local identity just because a setting was
    left behind — that would quietly merge every user's history into one bucket
    while appearing to work.

    Raises ``HTTPException(401)``; the WebSocket wrapper translates it, because a
    WS handshake cannot answer with an HTTP status once accepted.
    """
    claims = claims_from_scope(connection)
    if claims:
        sub = str(claims.get("sub") or "").strip()
        if sub:
            return sub
        # An authorizer that ran but produced no subject is a misconfiguration, not
        # an anonymous user. Falling back would grant access on a broken config.
        raise HTTPException(
            status_code=401, detail="Authenticated request carried no subject claim"
        )

    from matrix_studio.settings import get_settings

    if get_settings().auth_mode == "single-user":
        return LOCAL_USER_SUB

    raise HTTPException(status_code=401, detail="Authentication required")


async def current_user(request: Request) -> str:
    """
    The caller's ``owner_sub``, or 401. The dependency every HTTP route takes.

    Tests override this dependency rather than forge a header. There is
    deliberately no "act as this user" header: one that production honoured would
    be a complete authorisation bypass, and one production ignored would mean the
    negative tests exercise a path that does not ship.
    """
    return _resolve(request)


async def current_user_ws(websocket: WebSocket) -> str:
    """
    The same identity, for the run event stream.

    A separate dependency because FastAPI cannot inject a ``Request`` into a
    WebSocket route — the connection object is a different type, even though the
    scope it carries is the same.

    Known Phase 1 problem, recorded here because this is where it will bite: the
    browser WebSocket API cannot set request headers, so an SPA has no way to send
    a bearer token on the handshake. Under an API Gateway JWT authorizer the WS
    route therefore needs a different mechanism (a short-lived ticket, or moving
    the stream to polling, which §5 already contemplates). Nothing about that
    changes the scoping here, which is why it is not a blocker for 0.2.
    """
    return _resolve(websocket)
