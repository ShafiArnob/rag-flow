"""Clients for the API, signups through it, and assertions on its auth responses, for the tests that
drive auth over HTTP."""

import ipaddress
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from opentelemetry.sdk.trace import TracerProvider
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.identity.authentication import SESSION_COOKIE_NAME
from app.identity.sessions import SessionCookie, decode_session_cookie
from app.main import create_app

# https, so the client's cookie jar stores and returns Secure cookies.
BASE_URL = "https://test"
SESSION_COOKIE_MAX_AGE = "1209600"
SECURE_COOKIE_ATTRIBUTES = {
    "httponly": "",
    "max-age": SESSION_COOKIE_MAX_AGE,
    "path": "/",
    "samesite": "lax",
    "secure": "",
}
# The content types a cross-site HTML form can send without a CORS preflight.
SIMPLE_CONTENT_TYPES = ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data"]
SESSIONS_IN_WORKSPACE = text("SELECT count(*) FROM sessions WHERE workspace_id = :workspace_id")
AUDIT_ENTRIES = text(
    "SELECT workspace_id, actor_id, action, target, ip FROM audit_log"
    " WHERE workspace_id = :workspace_id"
)
PEER_PORT = 50123
LOOPBACK_ADDRESS = "127.0.0.1"
# The IPv6 documentation prefix (RFC 3849): large enough that random addresses never collide.
DOCUMENTATION_NETWORK = ipaddress.IPv6Network("2001:db8::/32")


@dataclass(frozen=True)
class SignedUp:
    email: str
    password: str
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    csrf_token: str
    cookie: SessionCookie


@asynccontextmanager
async def client_for(
    engine: AsyncEngine,
    redis: Redis,
    *,
    session_cookie_secure: bool = True,
    peer_address: str = LOOPBACK_ADDRESS,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(TracerProvider(), {}, engine, redis, session_cookie_secure)
    transport = httpx.ASGITransport(app=app, client=(peer_address, PEER_PORT))
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        yield client


def new_email() -> str:
    return f"signup-{uuid.uuid4()}@example.test"


def new_password() -> str:
    return secrets.token_urlsafe(16)


def new_peer_address() -> str:
    """A random address, so a client's login attempts share no limiter key with another's."""
    return str(DOCUMENTATION_NETWORK[secrets.randbelow(DOCUMENTATION_NETWORK.num_addresses)])


def parse_set_cookie(header: str) -> tuple[str, str, dict[str, str]]:
    """The cookie's name, value, and attributes keyed by lowercase name."""
    name_value, *attributes = header.split("; ")
    name, _, value = name_value.partition("=")
    parsed = {}
    for attribute in attributes:
        key, _, attribute_value = attribute.partition("=")
        parsed[key.lower()] = attribute_value
    return name, value, parsed


def session_cookie_of(response: httpx.Response) -> tuple[SessionCookie, dict[str, str]]:
    [header] = response.headers.get_list("set-cookie")
    name, value, attributes = parse_set_cookie(header)
    assert name == SESSION_COOKIE_NAME
    cookie = decode_session_cookie(value)
    assert cookie is not None
    return cookie, attributes


async def sign_up(client: httpx.AsyncClient) -> SignedUp:
    email, password = new_email(), new_password()
    response = await client.post("/auth/signup", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    body = response.json()
    cookie, _ = session_cookie_of(response)
    return SignedUp(
        email=email,
        password=password,
        user_id=uuid.UUID(body["user"]["id"]),
        workspace_id=uuid.UUID(body["workspace"]["id"]),
        csrf_token=body["csrf_token"],
        cookie=cookie,
    )


def cookie_header(value: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE_NAME}={value}"}


def assert_not_authenticated(response: httpx.Response) -> None:
    assert response.status_code == 401
    detail = response.json()["detail"]
    assert detail["code"] == "not_authenticated"
    assert isinstance(detail["message"], str)
    assert response.headers.get_list("set-cookie") == []


def assert_csrf_token_invalid(response: httpx.Response) -> None:
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == "csrf_token_invalid"
    assert isinstance(detail["message"], str)
    assert response.headers.get_list("set-cookie") == []


async def sessions_in_workspace(superuser_engine: AsyncEngine, workspace_id: uuid.UUID) -> int:
    async with superuser_engine.connect() as connection:
        count: int = (
            await connection.execute(SESSIONS_IN_WORKSPACE, {"workspace_id": workspace_id})
        ).scalar_one()
    return count


def assert_errors_carry_no_input(response: httpx.Response, *submitted: str) -> None:
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert errors
    for error in errors:
        assert set(error) == {"type", "loc", "msg"}
    for value in submitted:
        assert value not in response.text
