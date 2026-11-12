"""Signup and /me over HTTP, against the real database as the application role."""

import asyncio
import hashlib
import ipaddress
import json
import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import timedelta

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.authentication import CSRF_HEADER_NAME, SESSION_COOKIE_NAME
from app.main import create_app
from app.sessions import (
    SESSION_LIFETIME,
    SESSION_REFRESH_INTERVAL,
    encode_session_cookie,
    new_csrf_token,
    new_session_token,
)
from auth_client import (
    AUDIT_ENTRIES,
    BASE_URL,
    PEER_PORT,
    SECURE_COOKIE_ATTRIBUTES,
    SIMPLE_CONTENT_TYPES,
    SignedUp,
    assert_csrf_token_invalid,
    assert_errors_carry_no_input,
    assert_not_authenticated,
    client_for,
    cookie_header,
    new_email,
    new_password,
    parse_set_cookie,
    session_cookie_of,
    sessions_in_workspace,
    sign_up,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("schema_at_head")]

CLOCK_TOLERANCE = timedelta(minutes=1)
ROW_COUNTS = text(
    "SELECT (SELECT count(*) FROM users) AS users,"
    " (SELECT count(*) FROM workspaces) AS workspaces,"
    " (SELECT count(*) FROM workspace_members) AS memberships,"
    " (SELECT count(*) FROM sessions) AS sessions"
)
STORED_SIGNUP = text(
    "SELECT users.email, users.password_hash, workspaces.name AS workspace_name,"
    " workspace_members.role, sessions.token_hash, sessions.csrf_token,"
    " sessions.expires_at - now() AS remaining"
    " FROM users"
    " JOIN workspace_members ON workspace_members.user_id = users.id"
    " JOIN workspaces ON workspaces.id = workspace_members.workspace_id"
    " JOIN sessions ON sessions.workspace_id = workspaces.id AND sessions.user_id = users.id"
    " WHERE users.id = :user_id AND workspaces.id = :workspace_id"
)
USERS_WITH_EMAIL = text("SELECT count(*) FROM users WHERE email = :email")
SESSION_EXPIRY = text("SELECT expires_at FROM sessions WHERE workspace_id = :workspace_id")
SESSION_REMAINING = text(
    "SELECT expires_at - now() FROM sessions WHERE workspace_id = :workspace_id"
)
SET_SESSION_REMAINING = text(
    "UPDATE sessions SET expires_at = now() + :remaining WHERE workspace_id = :workspace_id"
)
# Documentation addresses (RFC 5737), so neither can be mistaken for a real client.
PEER_ADDRESS = "198.51.100.7"
FORWARDED_FOR_ADDRESS = "203.0.113.9"
CLEARED_COOKIE_ATTRIBUTES = {
    "httponly": "",
    "max-age": "0",
    "path": "/",
    "samesite": "lax",
    "secure": "",
}
LOCK_SESSIONS_IN_WORKSPACE = text(
    "SELECT id FROM sessions WHERE workspace_id = :workspace_id FOR UPDATE"
)
BACKENDS_WAITING_ON_A_LOCK = text(
    "SELECT count(*) FROM pg_stat_activity"
    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
)
LOCK_WAIT_DEADLINE_SECONDS = 10.0
LOCK_POLL_INTERVAL_SECONDS = 0.01
CONCURRENT_LOGOUTS = 2


@pytest.fixture
async def client(app_engine: AsyncEngine, redis: Redis) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(app_engine, redis) as client:
        yield client


async def row_counts(superuser_engine: AsyncEngine) -> tuple[int, ...]:
    async with superuser_engine.connect() as connection:
        return tuple((await connection.execute(ROW_COUNTS)).one())


async def users_with_email(superuser_engine: AsyncEngine, email: str) -> int:
    async with superuser_engine.connect() as connection:
        count: int = (await connection.execute(USERS_WITH_EMAIL, {"email": email})).scalar_one()
    return count


async def session_remaining(superuser_engine: AsyncEngine, workspace_id: uuid.UUID) -> timedelta:
    async with superuser_engine.connect() as connection:
        remaining: timedelta = (
            await connection.execute(SESSION_REMAINING, {"workspace_id": workspace_id})
        ).scalar_one()
    return remaining


async def set_session_remaining(
    superuser_engine: AsyncEngine, workspace_id: uuid.UUID, remaining: timedelta
) -> None:
    async with superuser_engine.begin() as connection:
        await connection.execute(
            SET_SESSION_REMAINING, {"workspace_id": workspace_id, "remaining": remaining}
        )


async def log_out(client: httpx.AsyncClient, signed_up: SignedUp) -> httpx.Response:
    return await client.post("/auth/logout", headers={CSRF_HEADER_NAME: signed_up.csrf_token})


async def wait_for_backends_waiting_on_a_lock(superuser_engine: AsyncEngine, count: int) -> None:
    deadline = asyncio.get_running_loop().time() + LOCK_WAIT_DEADLINE_SECONDS
    while True:
        # A connection per poll: a transaction reads pg_stat_activity once and keeps that snapshot.
        async with superuser_engine.connect() as connection:
            waiting = (await connection.execute(BACKENDS_WAITING_ON_A_LOCK)).scalar_one()
        if waiting >= count:
            return
        assert asyncio.get_running_loop().time() < deadline, (
            f"{waiting} of {count} backends waited on a lock within {LOCK_WAIT_DEADLINE_SECONDS}s"
        )
        await asyncio.sleep(LOCK_POLL_INTERVAL_SECONDS)


async def test_signup_creates_the_account_and_a_session_and_sets_the_cookie(
    client: httpx.AsyncClient, superuser_engine: AsyncEngine
) -> None:
    email, password = new_email(), new_password()

    response = await client.post("/auth/signup", json={"email": email, "password": password})

    assert response.status_code == 201
    body = response.json()
    assert body == {
        "user": {"id": body["user"]["id"], "email": email},
        "workspace": {"id": body["workspace"]["id"], "name": "Personal"},
        "csrf_token": body["csrf_token"],
    }
    user_id = uuid.UUID(body["user"]["id"])
    workspace_id = uuid.UUID(body["workspace"]["id"])
    cookie, attributes = session_cookie_of(response)
    assert cookie.workspace_id == workspace_id
    assert attributes == SECURE_COOKIE_ATTRIBUTES
    assert password not in response.text

    async with superuser_engine.connect() as connection:
        stored = (
            await connection.execute(
                STORED_SIGNUP, {"user_id": user_id, "workspace_id": workspace_id}
            )
        ).one()
    assert stored.email == email
    assert stored.password_hash.startswith("$argon2id$v=19$m=65536,t=3,")
    assert stored.workspace_name == "Personal"
    assert stored.role == "owner"
    assert stored.token_hash == hashlib.sha256(cookie.token.encode()).digest()
    assert stored.csrf_token == body["csrf_token"]
    assert SESSION_LIFETIME - CLOCK_TOLERANCE < stored.remaining <= SESSION_LIFETIME


async def test_signup_cookie_is_not_secure_when_the_setting_is_off(
    app_engine: AsyncEngine, redis: Redis
) -> None:
    async with client_for(app_engine, redis, session_cookie_secure=False) as client:
        response = await client.post(
            "/auth/signup", json={"email": new_email(), "password": new_password()}
        )

    assert response.status_code == 201
    _, attributes = session_cookie_of(response)
    assert attributes == {
        name: value for name, value in SECURE_COOKIE_ATTRIBUTES.items() if name != "secure"
    }


@pytest.mark.parametrize("respell", [str, str.upper], ids=["same-case", "upper-case"])
async def test_signup_with_a_registered_email_is_409_and_writes_nothing(
    client: httpx.AsyncClient, superuser_engine: AsyncEngine, respell: Callable[[str], str]
) -> None:
    existing = await sign_up(client)
    duplicate_email = respell(existing.email)
    counts_before = await row_counts(superuser_engine)

    response = await client.post(
        "/auth/signup", json={"email": duplicate_email, "password": new_password()}
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "email_already_registered"
    assert isinstance(detail["message"], str)
    assert response.headers.get_list("set-cookie") == []
    assert await row_counts(superuser_engine) == counts_before


async def test_signup_with_an_11_character_password_is_422_without_echoing_it(
    client: httpx.AsyncClient, superuser_engine: AsyncEngine
) -> None:
    email = new_email()
    short_password = secrets.token_urlsafe(16)[:11]

    response = await client.post("/auth/signup", json={"email": email, "password": short_password})

    assert_errors_carry_no_input(response, short_password)
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "password"]
    # Pydantic reports a SecretStr's length with the generic type, not string_too_short.
    assert error["type"] == "too_short"
    assert await users_with_email(superuser_engine, email) == 0


async def test_signup_with_a_12_character_password_succeeds(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/auth/signup",
        json={"email": new_email(), "password": secrets.token_urlsafe(16)[:12]},
    )

    assert response.status_code == 201


async def test_signup_with_a_1025_character_password_is_422_without_echoing_it(
    client: httpx.AsyncClient,
) -> None:
    long_password = "x" * 1025

    response = await client.post(
        "/auth/signup", json={"email": new_email(), "password": long_password}
    )

    assert_errors_carry_no_input(response, long_password)
    assert [error["loc"] for error in response.json()["detail"]] == [["body", "password"]]


async def test_signup_with_a_missing_field_is_422_without_echoing_the_body(
    client: httpx.AsyncClient,
) -> None:
    email = new_email()

    response = await client.post("/auth/signup", json={"email": email})

    assert_errors_carry_no_input(response, email)
    assert [(error["loc"], error["type"]) for error in response.json()["detail"]] == [
        (["body", "password"], "missing")
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"email": 1234567890123, "password": 9876543210987},
        {"email": ["listed@example.test"], "password": {"nested": "sekrit-value-0001"}},
        ["listed@example.test", "sekrit-value-0001"],
        "sekrit-value-0001",
    ],
    ids=["numbers", "containers", "array-body", "string-body"],
)
async def test_signup_with_wrong_types_is_422_without_echoing_the_values(
    client: httpx.AsyncClient, body: object
) -> None:
    response = await client.post("/auth/signup", json=body)

    assert_errors_carry_no_input(
        response, "1234567890123", "9876543210987", "listed@example.test", "sekrit-value-0001"
    )


@pytest.mark.parametrize(
    "email",
    [
        "no-at-sign.example.test",
        "two@@example.test",
        "a b@c.d",
        # Postgres cannot store NUL in text, so letting it through turns bad input into a 500.
        "nul\x00char@example.test",
        "control\x01char@example.test",
        "delete\x7fchar@example.test",
    ],
)
async def test_signup_with_an_invalid_email_is_422(client: httpx.AsyncClient, email: str) -> None:
    password = new_password()

    response = await client.post("/auth/signup", json={"email": email, "password": password})

    assert_errors_carry_no_input(response, email, password)
    assert [error["loc"] for error in response.json()["detail"]] == [["body", "email"]]


@pytest.mark.parametrize("content_type", [*SIMPLE_CONTENT_TYPES, None])
async def test_signup_rejects_a_json_body_not_sent_as_json(
    client: httpx.AsyncClient, superuser_engine: AsyncEngine, content_type: str | None
) -> None:
    """A cross-site form can post these content types without a preflight; FastAPI parses a body
    as JSON only when it is labelled JSON."""
    email, password = new_email(), new_password()
    headers = {} if content_type is None else {"Content-Type": content_type}

    response = await client.post(
        "/auth/signup",
        content=json.dumps({"email": email, "password": password}),
        headers=headers,
    )

    assert_errors_carry_no_input(response, email, password)
    assert await users_with_email(superuser_engine, email) == 0


async def test_me_returns_the_callers_identity(client: httpx.AsyncClient) -> None:
    signed_up = await sign_up(client)

    response = await client.get("/me")

    assert response.status_code == 200
    assert response.json() == {
        "user": {"id": str(signed_up.user_id), "email": signed_up.email},
        "workspace": {"id": str(signed_up.workspace_id), "name": "Personal"},
        "csrf_token": signed_up.csrf_token,
    }


@pytest.mark.parametrize(
    "cookie_value",
    [
        None,
        "",
        "garbage",
        str(uuid.uuid4()),
        f"{uuid.uuid4()}.too-short",
        f"not-a-uuid.{new_session_token()}",
    ],
    ids=["no-cookie", "empty", "garbage", "workspace-only", "short-token", "bad-workspace"],
)
async def test_me_without_a_well_formed_cookie_is_401(
    client: httpx.AsyncClient, cookie_value: str | None
) -> None:
    headers = {} if cookie_value is None else cookie_header(cookie_value)

    assert_not_authenticated(await client.get("/me", headers=headers))


async def test_me_with_an_unknown_token_is_401(
    client: httpx.AsyncClient, app_engine: AsyncEngine, redis: Redis
) -> None:
    signed_up = await sign_up(client)
    unknown = f"{signed_up.workspace_id}.{new_session_token()}"

    async with client_for(app_engine, redis) as stranger:
        assert_not_authenticated(await stranger.get("/me", headers=cookie_header(unknown)))


async def test_me_with_an_expired_session_is_401(
    client: httpx.AsyncClient, superuser_engine: AsyncEngine
) -> None:
    signed_up = await sign_up(client)
    # The negative control: the same cookie works until the session expires.
    assert (await client.get("/me")).status_code == 200

    await set_session_remaining(superuser_engine, signed_up.workspace_id, timedelta(seconds=-1))

    assert_not_authenticated(await client.get("/me"))


async def test_me_with_a_token_moved_to_another_workspace_is_401(
    app_engine: AsyncEngine, redis: Redis
) -> None:
    async with (
        client_for(app_engine, redis) as first_client,
        client_for(app_engine, redis) as second_client,
    ):
        first = await sign_up(first_client)
        second = await sign_up(second_client)
    own = f"{first.workspace_id}.{first.cookie.token}"
    spliced = f"{second.workspace_id}.{first.cookie.token}"

    async with client_for(app_engine, redis) as client:
        # The negative control: the token works with its own workspace.
        response = await client.get("/me", headers=cookie_header(own))
        assert response.status_code == 200
        assert response.json()["user"]["id"] == str(first.user_id)

        assert_not_authenticated(await client.get("/me", headers=cookie_header(spliced)))


async def test_me_slides_the_expiry_and_reissues_the_cookie_once_a_refresh_is_due(
    client: httpx.AsyncClient, superuser_engine: AsyncEngine
) -> None:
    signed_up = await sign_up(client)
    due = SESSION_LIFETIME - SESSION_REFRESH_INTERVAL - CLOCK_TOLERANCE
    await set_session_remaining(superuser_engine, signed_up.workspace_id, due)

    response = await client.get("/me")

    assert response.status_code == 200
    cookie, attributes = session_cookie_of(response)
    assert cookie == signed_up.cookie
    assert attributes == SECURE_COOKIE_ATTRIBUTES
    remaining = await session_remaining(superuser_engine, signed_up.workspace_id)
    assert SESSION_LIFETIME - CLOCK_TOLERANCE < remaining <= SESSION_LIFETIME


async def test_me_leaves_the_expiry_alone_until_a_refresh_is_due(
    client: httpx.AsyncClient, superuser_engine: AsyncEngine
) -> None:
    signed_up = await sign_up(client)
    not_yet_due = SESSION_LIFETIME - SESSION_REFRESH_INTERVAL + CLOCK_TOLERANCE
    await set_session_remaining(superuser_engine, signed_up.workspace_id, not_yet_due)
    async with superuser_engine.connect() as connection:
        expiry_before = (
            await connection.execute(SESSION_EXPIRY, {"workspace_id": signed_up.workspace_id})
        ).scalar_one()

    response = await client.get("/me")

    assert response.status_code == 200
    assert response.headers.get_list("set-cookie") == []
    async with superuser_engine.connect() as connection:
        expiry_after = (
            await connection.execute(SESSION_EXPIRY, {"workspace_id": signed_up.workspace_id})
        ).scalar_one()
    assert expiry_after == expiry_before


@pytest.mark.parametrize(
    "csrf_token", [None, new_csrf_token()], ids=["no-csrf-header", "wrong-csrf-header"]
)
async def test_me_needs_no_csrf_token(client: httpx.AsyncClient, csrf_token: str | None) -> None:
    await sign_up(client)
    headers = {} if csrf_token is None else {CSRF_HEADER_NAME: csrf_token}

    response = await client.get("/me", headers=headers)

    assert response.status_code == 200


async def test_logout_ends_the_session_and_clears_the_cookie(
    client: httpx.AsyncClient,
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    redis: Redis,
) -> None:
    signed_up = await sign_up(client)

    response = await log_out(client, signed_up)

    assert response.status_code == 204
    assert response.content == b""
    [header] = response.headers.get_list("set-cookie")
    name, value, attributes = parse_set_cookie(header)
    assert (name, value) == (SESSION_COOKIE_NAME, '""')
    # The time of the response, which has passed by the time a browser reads it.
    assert attributes.pop("expires").endswith(" GMT")
    assert attributes == CLEARED_COOKIE_ATTRIBUTES
    assert await sessions_in_workspace(superuser_engine, signed_up.workspace_id) == 0
    # A client that kept the old cookie, as one that ignored the deletion would.
    old_cookie = cookie_header(encode_session_cookie(signed_up.cookie))
    async with client_for(app_engine, redis) as stale_client:
        assert_not_authenticated(await stale_client.get("/me", headers=old_cookie))


async def test_logout_clears_a_cookie_that_is_not_secure_when_the_setting_is_off(
    app_engine: AsyncEngine, redis: Redis
) -> None:
    async with client_for(app_engine, redis, session_cookie_secure=False) as client:
        signed_up = await sign_up(client)

        response = await log_out(client, signed_up)

    assert response.status_code == 204
    [header] = response.headers.get_list("set-cookie")
    _, _, attributes = parse_set_cookie(header)
    del attributes["expires"]
    assert attributes == {
        name: value for name, value in CLEARED_COOKIE_ATTRIBUTES.items() if name != "secure"
    }


async def test_logout_records_the_user_and_the_peer_address_never_the_forwarded_one(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, redis: Redis
) -> None:
    app = create_app(TracerProvider(), {}, app_engine, redis, session_cookie_secure=True)
    transport = httpx.ASGITransport(app=app, client=(PEER_ADDRESS, PEER_PORT))
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        signed_up = await sign_up(client)

        response = await client.post(
            "/auth/logout",
            headers={
                CSRF_HEADER_NAME: signed_up.csrf_token,
                "X-Forwarded-For": FORWARDED_FOR_ADDRESS,
            },
        )

    assert response.status_code == 204
    async with superuser_engine.connect() as connection:
        entries = (
            await connection.execute(AUDIT_ENTRIES, {"workspace_id": signed_up.workspace_id})
        ).all()
    assert [tuple(entry) for entry in entries] == [
        (
            signed_up.workspace_id,
            signed_up.user_id,
            "logout",
            None,
            ipaddress.ip_address(PEER_ADDRESS),
        )
    ]


async def test_concurrent_logouts_of_one_session_end_it_and_record_it_once(
    client: httpx.AsyncClient, superuser_engine: AsyncEngine
) -> None:
    """Both requests find the session before either deletes it, as a double-click can. A row lock
    held here keeps both deletes waiting until both have passed the session lookup."""
    signed_up = await sign_up(client)

    async with asyncio.TaskGroup() as group, superuser_engine.connect() as lock_holder:
        await lock_holder.execute(
            LOCK_SESSIONS_IN_WORKSPACE, {"workspace_id": signed_up.workspace_id}
        )
        logouts = [group.create_task(log_out(client, signed_up)) for _ in range(CONCURRENT_LOGOUTS)]
        await wait_for_backends_waiting_on_a_lock(superuser_engine, CONCURRENT_LOGOUTS)
        await lock_holder.commit()

    responses = sorted(
        (logout.result() for logout in logouts), key=lambda response: response.status_code
    )
    assert [response.status_code for response in responses] == [204, 401]
    assert_not_authenticated(responses[1])
    async with superuser_engine.connect() as connection:
        entries = (
            await connection.execute(AUDIT_ENTRIES, {"workspace_id": signed_up.workspace_id})
        ).all()
    assert [entry.action for entry in entries] == ["logout"]
    assert await sessions_in_workspace(superuser_engine, signed_up.workspace_id) == 0


async def test_logout_when_a_refresh_is_due_sends_only_the_deletion(
    client: httpx.AsyncClient, superuser_engine: AsyncEngine
) -> None:
    signed_up = await sign_up(client)
    due = SESSION_LIFETIME - SESSION_REFRESH_INTERVAL - CLOCK_TOLERANCE
    await set_session_remaining(superuser_engine, signed_up.workspace_id, due)

    response = await log_out(client, signed_up)

    assert response.status_code == 204
    [header] = response.headers.get_list("set-cookie")
    _, _, attributes = parse_set_cookie(header)
    assert attributes["max-age"] == "0"
    assert await sessions_in_workspace(superuser_engine, signed_up.workspace_id) == 0


async def test_logout_with_another_users_csrf_token_is_403_and_the_session_still_works(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, redis: Redis
) -> None:
    async with (
        client_for(app_engine, redis) as first_client,
        client_for(app_engine, redis) as second_client,
    ):
        first = await sign_up(first_client)
        second = await sign_up(second_client)

        assert_csrf_token_invalid(
            await first_client.post("/auth/logout", headers={CSRF_HEADER_NAME: second.csrf_token})
        )

        response = await first_client.get("/me")
        assert response.status_code == 200
        assert response.json()["user"]["id"] == str(first.user_id)
    assert await sessions_in_workspace(superuser_engine, first.workspace_id) == 1


@pytest.mark.parametrize(
    "csrf_token",
    [
        None,
        b"",
        new_csrf_token().encode(),
        # Starlette decodes header bytes as Latin-1, so these arrive as non-ASCII characters.
        "éÿ".encode("latin-1"),
    ],
    ids=["no-header", "empty", "wrong", "non-ascii"],
)
async def test_logout_without_the_sessions_csrf_token_is_403_and_keeps_the_session(
    client: httpx.AsyncClient, superuser_engine: AsyncEngine, csrf_token: bytes | None
) -> None:
    signed_up = await sign_up(client)
    headers = {} if csrf_token is None else {CSRF_HEADER_NAME.encode(): csrf_token}

    assert_csrf_token_invalid(await client.post("/auth/logout", headers=headers))

    assert await sessions_in_workspace(superuser_engine, signed_up.workspace_id) == 1
    assert (await client.get("/me")).status_code == 200


async def test_logout_with_an_unknown_session_and_no_csrf_token_is_401(
    client: httpx.AsyncClient, app_engine: AsyncEngine, redis: Redis
) -> None:
    """The session is checked before the CSRF token."""
    signed_up = await sign_up(client)
    unknown = f"{signed_up.workspace_id}.{new_session_token()}"

    async with client_for(app_engine, redis) as stranger:
        response = await stranger.post("/auth/logout", headers=cookie_header(unknown))

    assert_not_authenticated(response)
