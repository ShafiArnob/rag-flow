"""The routes anyone may call without a session, and the (method, path) pairs an app serves, for
tests that check every other route demands one."""

import re
import uuid

from fastapi import FastAPI
from fastapi.routing import iter_route_contexts

from app.authentication import CSRF_PROTECTED_METHODS

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_DOCUMENTATION_PATHS = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")
# Starlette serves FastAPI's documentation routes for HEAD as well as GET.
_DOCUMENTATION_METHODS = ("GET", "HEAD")

# Every route that needs no session, with the reason. Any other route must answer 401 without a
# session cookie, and 403 without the CSRF token when its method can change state.
PUBLIC_ROUTES = {
    ("GET", "/healthz"): "the liveness probe; it reads nothing",
    ("GET", "/readyz"): "the readiness probe; it names dependencies, not data",
    ("POST", "/auth/signup"): "creates the session, so no session can exist yet",
    ("POST", "/auth/login"): "creates the session, so no session can exist yet",
    **{
        (method, path): "FastAPI's documentation of the public API contract"
        for path in _DOCUMENTATION_PATHS
        for method in _DOCUMENTATION_METHODS
    },
}

_PATH_PARAMETER = re.compile(r"\{[^}]+\}")


class UnclassifiedRouteError(AssertionError):
    pass


def served_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every (method, path) pair ``app`` serves, through included routers too.

    Raises UnclassifiedRouteError for a route these tests cannot check: one without a path or a
    method list (a mount or a websocket, say), or one with a method that is neither safe nor CSRF
    protected.
    """
    routes = []
    for route in iter_route_contexts(app.routes):
        if route.path is None or route.methods is None:
            raise UnclassifiedRouteError(f"route {route.original_route!r} has no path or methods")
        for method in sorted(route.methods):
            if method not in SAFE_METHODS | CSRF_PROTECTED_METHODS:
                raise UnclassifiedRouteError(f"{method} {route.path} is neither safe nor protected")
            routes.append((method, route.path))
    return routes


def protected_routes(app: FastAPI) -> list[tuple[str, str]]:
    """The routes of ``app`` that need a session. Raises UnclassifiedRouteError as served_routes
    does, and when PUBLIC_ROUTES names a route ``app`` does not serve, so the list cannot go
    stale."""
    served = served_routes(app)
    stale = set(PUBLIC_ROUTES) - set(served)
    if stale:
        raise UnclassifiedRouteError(f"PUBLIC_ROUTES names routes the app lacks: {sorted(stale)}")
    return [route for route in served if route not in PUBLIC_ROUTES]


def concrete_path(path: str) -> str:
    """``path`` with a random UUID in place of each path parameter."""
    return _PATH_PARAMETER.sub(lambda _: str(uuid.uuid4()), path)
