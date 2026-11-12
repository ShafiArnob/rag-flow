"""Login and its limits over HTTP, against the real database and the Redis at REDIS_URL.

Every test logs in from its own random peer address and on its own accounts, so no two tests share
a limiter key and nothing needs flushing.
"""

import asyncio
import ipaddress
import json
import logging
import secrets
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import create_app
from tenants import create_tenant
from test_auth import (
    AUDIT_ENTRIES,
    BASE_URL,
    LOOPBACK_ADDRESS,
    PEER_PORT,
    SECURE_COOKIE_ATTRIBUTES,
    SIMPLE_CONTENT_TYPES,
    SignedUp,
    assert_errors_carry_no_input,
    client_for,
    new_email,
    new_password,
    session_cookie_of,
    sessions_in_workspace,
    sign_up,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("schema_at_head")]

# The limits the plan sets, written out so the tests check them rather than echo the code's values.
ACCOUNT_ATTEMPTS_ALLOWED = 5
IP_ATTEMPTS_ALLOWED = 20
FIRST_LOCKOUT_SECONDS = 60
SECOND_LOCKOUT_SECONDS = 120
CONCURRENT_ATTEMPTS = 10
# The IPv6 documentation prefix (RFC 3849): large enough that random addresses never collide.
DOCUMENTATION_NETWORK = ipaddress.IPv6Network("2001:db8::/32")
FORWARDED_FOR_ADDRESS = "203.0.113.9"
# Nothing listens on port 1 in the test container, so a connection is refused at once.
CLOSED_PORT = 1
UNREACHABLE_REDIS_TIMEOUT_SECONDS = 0.5

USER_EMAIL = text("SELECT email FROM users WHERE id = :id")


def new_peer_address() -> str:
    return str(DOCUMENTATION_NETWORK[secrets.randbelow(DOCUMENTATION_NETWORK.num_addresses)])


def account_keys_prefix(user_id: uuid.UUID) -> str:
    return f"login:account:user:{user_id}"


@pytest.fixture
async def signed_up(app_engine: AsyncEngine, redis: Redis) -> SignedUp:
    async with client_for(app_engine, redis, peer_address=new_peer_address()) as client:
        return await sign_up(client)


@pytest.fixture
async def client(app_engine: AsyncEngine, redis: Redis) -> AsyncIterator[httpx.AsyncClient]:
    """A client with no session, from a peer address no other test uses."""
    async with client_for(app_engine, redis, peer_address=new_peer_address()) as client:
        yield client


async def log_in(client: httpx.AsyncClient, email: str, password: str) -> httpx.Response:
    return await client.post("/auth/login", json={"email": email, "password": password})


def assert_invalid_credentials(response: httpx.Response) -> None:
    assert response.status_code == 401, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_credentials"
    assert isinstance(detail["message"], str)
    assert response.headers.get_list("set-cookie") == []


def retry_after_of_rate_limited(response: httpx.Response) -> int:
    assert response.status_code == 429, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "login_rate_limited"
    assert isinstance(detail["message"], str)
    assert response.headers.get_list("set-cookie") == []
    return int(response.headers["Retry-After"])


async def fail_until_locked_out(client: httpx.AsyncClient, signed_up: SignedUp) -> int:
    """Uses up the account's allowed attempts with wrong passwords; returns the Retry-After of the
    attempt after them."""
    for _ in range(ACCOUNT_ATTEMPTS_ALLOWED):
        assert_invalid_credentials(await log_in(client, signed_up.email, new_password()))
    return retry_after_of_rate_limited(await log_in(client, signed_up.email, new_password()))


async def test_login_starts_a_session_that_me_accepts(
    client: httpx.AsyncClient, signed_up: SignedUp, superuser_engine: AsyncEngine
) -> None:
    response = await log_in(client, signed_up.email, signed_up.password)

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "user": {"id": str(signed_up.user_id), "email": signed_up.email},
        "workspace": {"id": str(signed_up.workspace_id), "name": "Personal"},
        "csrf_token": body["csrf_token"],
    }
    assert body["csrf_token"] != signed_up.csrf_token
    cookie, attributes = session_cookie_of(response)
    assert cookie.workspace_id == signed_up.workspace_id
    assert cookie.token != signed_up.cookie.token
    assert attributes == SECURE_COOKIE_ATTRIBUTES
    assert signed_up.password not in response.text
    me = await client.get("/me")
    assert me.status_code == 200
    assert me.json() == body
    assert await sessions_in_workspace(superuser_engine, signed_up.workspace_id) == 2


async def test_login_matches_the_email_case_insensitively_and_returns_it_as_stored(
    client: httpx.AsyncClient, signed_up: SignedUp
) -> None:
    respelled = signed_up.email.upper()
    assert respelled != signed_up.email

    response = await log_in(client, respelled, signed_up.password)

    assert response.status_code == 200
    assert response.json()["user"] == {"id": str(signed_up.user_id), "email": signed_up.email}


async def test_wrong_password_unknown_email_and_no_password_get_the_same_401(
    client: httpx.AsyncClient,
    signed_up: SignedUp,
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
) -> None:
    without_password = await create_tenant(app_engine)
    async with superuser_engine.connect() as connection:
        without_password_email = (
            await connection.execute(USER_EMAIL, {"id": without_password.user_id})
        ).scalar_one()

    responses = [
        await log_in(client, signed_up.email, new_password()),
        await log_in(client, new_email(), new_password()),
        await log_in(client, without_password_email, new_password()),
    ]

    for response in responses:
        assert_invalid_credentials(response)
    assert len({response.text for response in responses}) == 1
    assert await sessions_in_workspace(superuser_engine, signed_up.workspace_id) == 1
    assert await sessions_in_workspace(superuser_engine, without_password.workspace_id) == 1


async def test_login_with_an_empty_password_is_422_without_echoing_the_email(
    client: httpx.AsyncClient,
) -> None:
    email = new_email()

    response = await client.post("/auth/login", json={"email": email, "password": ""})

    assert_errors_carry_no_input(response, email)
    assert [error["loc"] for error in response.json()["detail"]] == [["body", "password"]]


@pytest.mark.parametrize("content_type", [*SIMPLE_CONTENT_TYPES, None])
async def test_login_rejects_a_json_body_not_sent_as_json(
    client: httpx.AsyncClient,
    signed_up: SignedUp,
    superuser_engine: AsyncEngine,
    content_type: str | None,
) -> None:
    """Login takes no CSRF token, so this is what stops a cross-site form, which can post these
    content types without a preflight, from logging a victim in to the attacker's account."""
    headers = {} if content_type is None else {"Content-Type": content_type}

    response = await client.post(
        "/auth/login",
        content=json.dumps({"email": signed_up.email, "password": signed_up.password}),
        headers=headers,
    )

    assert_errors_carry_no_input(response, signed_up.email, signed_up.password)
    assert response.headers.get_list("set-cookie") == []
    assert await sessions_in_workspace(superuser_engine, signed_up.workspace_id) == 1


async def test_login_records_the_user_and_the_peer_address_never_the_forwarded_one(
    app_engine: AsyncEngine, redis: Redis, superuser_engine: AsyncEngine, signed_up: SignedUp
) -> None:
    peer_address = new_peer_address()
    async with client_for(app_engine, redis, peer_address=peer_address) as client:
        response = await client.post(
            "/auth/login",
            json={"email": signed_up.email, "password": signed_up.password},
            headers={"X-Forwarded-For": FORWARDED_FOR_ADDRESS},
        )

    assert response.status_code == 200
    async with superuser_engine.connect() as connection:
        entries = (
            await connection.execute(AUDIT_ENTRIES, {"workspace_id": signed_up.workspace_id})
        ).all()
    assert [tuple(entry) for entry in entries] == [
        (
            signed_up.workspace_id,
            signed_up.user_id,
            "login",
            None,
            ipaddress.ip_address(peer_address),
        )
    ]


async def test_the_attempt_after_the_allowed_ones_is_429_for_a_minute(
    client: httpx.AsyncClient, signed_up: SignedUp
) -> None:
    assert await fail_until_locked_out(client, signed_up) == FIRST_LOCKOUT_SECONDS


async def test_the_right_password_is_429_while_locked_out_and_starts_no_session(
    client: httpx.AsyncClient, signed_up: SignedUp, superuser_engine: AsyncEngine
) -> None:
    await fail_until_locked_out(client, signed_up)

    retry_after = retry_after_of_rate_limited(
        await log_in(client, signed_up.email, signed_up.password)
    )

    assert 0 < retry_after <= FIRST_LOCKOUT_SECONDS
    assert await sessions_in_workspace(superuser_engine, signed_up.workspace_id) == 1


async def test_the_next_lockout_lasts_twice_as_long(
    client: httpx.AsyncClient, signed_up: SignedUp, redis: Redis
) -> None:
    await fail_until_locked_out(client, signed_up)
    # Ends the first lockout without waiting a minute for it.
    assert await redis.delete(f"{account_keys_prefix(signed_up.user_id)}:lock") == 1

    assert await fail_until_locked_out(client, signed_up) == SECOND_LOCKOUT_SECONDS


async def test_a_successful_login_resets_the_attempts_and_the_lockouts(
    client: httpx.AsyncClient, signed_up: SignedUp, redis: Redis
) -> None:
    await fail_until_locked_out(client, signed_up)
    assert await redis.delete(f"{account_keys_prefix(signed_up.user_id)}:lock") == 1
    for _ in range(ACCOUNT_ATTEMPTS_ALLOWED - 1):
        assert_invalid_credentials(await log_in(client, signed_up.email, new_password()))
    # The last allowed attempt.
    assert (await log_in(client, signed_up.email, signed_up.password)).status_code == 200

    # Without the reset, the first of these would be refused and the lockout would be the second.
    assert await fail_until_locked_out(client, signed_up) == FIRST_LOCKOUT_SECONDS


async def test_the_attempt_after_the_allowed_ones_from_one_address_is_429_on_any_account(
    client: httpx.AsyncClient,
    signed_up: SignedUp,
    app_engine: AsyncEngine,
    redis: Redis,
) -> None:
    for _ in range(IP_ATTEMPTS_ALLOWED):
        assert_invalid_credentials(await log_in(client, new_email(), new_password()))

    retry_after = retry_after_of_rate_limited(
        await log_in(client, signed_up.email, signed_up.password)
    )

    assert 0 < retry_after <= FIRST_LOCKOUT_SECONDS
    # The refused attempt did not count against the account, which logs in from elsewhere.
    assert await redis.exists(f"{account_keys_prefix(signed_up.user_id)}:attempts") == 0
    async with client_for(app_engine, redis, peer_address=new_peer_address()) as elsewhere:
        assert (await log_in(elsewhere, signed_up.email, signed_up.password)).status_code == 200


async def test_an_address_over_its_limit_is_429_even_when_its_window_reports_no_time_left(
    app_engine: AsyncEngine, redis: Redis, signed_up: SignedUp, superuser_engine: AsyncEngine
) -> None:
    """In the last millisecond of a window Redis reports a PTTL of 0 for a key that still exists.
    That millisecond cannot be timed, so a count without an expiry, whose PTTL of -1 takes the same
    path, stands in for it."""
    peer_address = new_peer_address()
    assert await redis.set(f"login:ip:{peer_address}", IP_ATTEMPTS_ALLOWED)

    async with client_for(app_engine, redis, peer_address=peer_address) as client:
        response = await log_in(client, signed_up.email, signed_up.password)

    assert retry_after_of_rate_limited(response) >= 1
    assert await redis.exists(f"{account_keys_prefix(signed_up.user_id)}:attempts") == 0
    assert await sessions_in_workspace(superuser_engine, signed_up.workspace_id) == 1


async def test_concurrent_wrong_passwords_get_exactly_the_allowed_attempts(
    client: httpx.AsyncClient, signed_up: SignedUp
) -> None:
    """Every attempt is counted before any is verified, so the attempts in flight together cannot
    all pass the check before the count catches up."""
    async with asyncio.TaskGroup() as group:
        attempts = [
            group.create_task(log_in(client, signed_up.email, new_password()))
            for _ in range(CONCURRENT_ATTEMPTS)
        ]

    statuses = sorted(attempt.result().status_code for attempt in attempts)
    assert statuses == [401] * ACCOUNT_ATTEMPTS_ALLOWED + [429] * (
        CONCURRENT_ATTEMPTS - ACCOUNT_ATTEMPTS_ALLOWED
    )


async def test_login_is_500_and_starts_no_session_when_redis_is_unreachable(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, signed_up: SignedUp
) -> None:
    unreachable = Redis(
        host=LOOPBACK_ADDRESS,
        port=CLOSED_PORT,
        socket_connect_timeout=UNREACHABLE_REDIS_TIMEOUT_SECONDS,
        socket_timeout=UNREACHABLE_REDIS_TIMEOUT_SECONDS,
    )
    app = create_app(TracerProvider(), {}, app_engine, unreachable, session_cookie_secure=True)
    transport = httpx.ASGITransport(
        app=app, raise_app_exceptions=False, client=(new_peer_address(), PEER_PORT)
    )
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        response = await log_in(client, signed_up.email, signed_up.password)
    await unreachable.aclose()

    assert response.status_code == 500
    assert response.headers.get_list("set-cookie") == []
    assert await sessions_in_workspace(superuser_engine, signed_up.workspace_id) == 1


async def test_failed_logins_log_neither_the_email_nor_the_password(
    client: httpx.AsyncClient, signed_up: SignedUp, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    unknown_email = new_email()
    wrong_password = new_password()
    unknown_email_password = new_password()

    assert_invalid_credentials(await log_in(client, signed_up.email, wrong_password))
    assert_invalid_credentials(await log_in(client, unknown_email, unknown_email_password))

    # The negative control: the failures were logged.
    failures = [record for record in caplog.records if record.name == "app.auth"]
    assert [record.levelno for record in failures] == [logging.WARNING, logging.WARNING]
    logged = "\n".join(str(vars(record)) for record in caplog.records)
    for value in (signed_up.email, unknown_email, wrong_password, unknown_email_password):
        assert value not in logged
        assert value.lower() not in logged.lower()
