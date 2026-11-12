"""Every route outside PUBLIC_ROUTES demands a session, and every one that can change state also
demands the session's CSRF token. Routes are found by enumerating the app, so a new route is
checked without anyone listing it."""

import httpx
import pytest
from fastapi import FastAPI, WebSocket
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.authentication import CSRF_HEADER_NAME, CSRF_PROTECTED_METHODS
from app.sessions import new_csrf_token
from public_routes import UnclassifiedRouteError, concrete_path, protected_routes, served_routes
from test_auth import client_for, sign_up


def test_logout_and_me_are_protected(app_without_database: FastAPI) -> None:
    """The negative control: the enumeration finds the routes known to need a session."""
    assert {("POST", "/auth/logout"), ("GET", "/me")} <= set(protected_routes(app_without_database))


def test_a_route_that_cannot_be_classified_fails_the_enumeration(
    app_without_database: FastAPI,
) -> None:
    @app_without_database.websocket("/unclassified")
    async def unclassified(websocket: WebSocket) -> None:
        await websocket.close()

    with pytest.raises(UnclassifiedRouteError, match="/unclassified"):
        served_routes(app_without_database)


async def test_every_protected_route_is_401_without_a_session_cookie(
    app_without_database: FastAPI,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_without_database), base_url="https://test"
    ) as client:
        failures = []
        for method, path in protected_routes(app_without_database):
            response = await client.request(method, concrete_path(path))
            if (
                response.status_code != 401
                or response.json()["detail"]["code"] != "not_authenticated"
            ):
                failures.append(f"{method} {path}: {response.status_code} {response.text}")

    assert failures == []


@pytest.mark.integration
@pytest.mark.usefixtures("schema_at_head")
@pytest.mark.parametrize(
    "csrf_headers",
    [{}, {CSRF_HEADER_NAME: new_csrf_token()}],
    ids=["no-header", "wrong-header"],
)
async def test_every_protected_route_that_can_change_state_is_403_without_the_csrf_token(
    app_without_database: FastAPI,
    app_engine: AsyncEngine,
    redis: Redis,
    csrf_headers: dict[str, str],
) -> None:
    # create_app registers the same routes whatever the engine, so either app can list them.
    unsafe_routes = [
        (method, path)
        for method, path in protected_routes(app_without_database)
        if method in CSRF_PROTECTED_METHODS
    ]
    assert unsafe_routes, "no route can change state, so nothing was checked"
    async with client_for(app_engine, redis) as client:
        await sign_up(client)
        failures = []
        for method, path in unsafe_routes:
            response = await client.request(method, concrete_path(path), headers=csrf_headers)
            if (
                response.status_code != 403
                or response.json()["detail"]["code"] != "csrf_token_invalid"
            ):
                failures.append(f"{method} {path}: {response.status_code} {response.text}")
        # The session survives every refused request.
        assert (await client.get("/me")).status_code == 200

    assert failures == []
