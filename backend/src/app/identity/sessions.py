"""Session tokens, the session cookie's value, and the statements that store sessions.

The cookie carries the workspace id beside the token so the session can be looked up in an ordinary
workspace transaction. Only the SHA-256 of the token is stored, so a copy of the database cannot be
replayed as cookies.
"""

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

SESSION_TOKEN_BYTES = 32
CSRF_TOKEN_BYTES = 32
SESSION_LIFETIME = timedelta(days=14)
# Expiry slides at most this often, so an active session does not cost a write on every request.
SESSION_REFRESH_INTERVAL = timedelta(hours=1)

_COOKIE_VALUE = re.compile(
    r"(?P<workspace_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    # 43 characters: SESSION_TOKEN_BYTES in unpadded base64url, as secrets.token_urlsafe writes it.
    r"\.(?P<token>[A-Za-z0-9_-]{43})"
)

_INSERT_SESSION = text(
    "INSERT INTO sessions (workspace_id, user_id, token_hash, csrf_token, expires_at)"
    " VALUES (:workspace_id, :user_id, :token_hash, :csrf_token,"
    " now() + CAST(:lifetime AS interval))"
)
_FIND_SESSION = text(
    "SELECT id, user_id, csrf_token,"
    " expires_at - now() < CAST(:refresh_threshold AS interval) AS is_refresh_due"
    " FROM sessions WHERE token_hash = :token_hash AND expires_at > now()"
)
_EXTEND_SESSION = text(
    "UPDATE sessions SET expires_at = now() + CAST(:lifetime AS interval) WHERE id = :id"
)
_DELETE_SESSION = text("DELETE FROM sessions WHERE id = :id")


@dataclass(frozen=True)
class SessionCookie:
    workspace_id: uuid.UUID
    token: str


@dataclass(frozen=True)
class StoredSession:
    id: uuid.UUID
    user_id: uuid.UUID
    csrf_token: str
    is_refresh_due: bool


def new_session_token() -> str:
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_BYTES)


def hash_session_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def encode_session_cookie(cookie: SessionCookie) -> str:
    return f"{cookie.workspace_id}.{cookie.token}"


def decode_session_cookie(value: str) -> SessionCookie | None:
    """None when ``value`` is not a cookie value this service could have issued."""
    match = _COOKIE_VALUE.fullmatch(value)
    if match is None:
        return None
    return SessionCookie(workspace_id=uuid.UUID(match["workspace_id"]), token=match["token"])


async def create_session(
    connection: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    token_hash: bytes,
    csrf_token: str,
) -> None:
    await connection.execute(
        _INSERT_SESSION,
        {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "token_hash": token_hash,
            "csrf_token": csrf_token,
            "lifetime": SESSION_LIFETIME,
        },
    )


async def find_session(connection: AsyncConnection, token_hash: bytes) -> StoredSession | None:
    """The unexpired session with ``token_hash`` in the connection's workspace, if there is one."""
    row = (
        await connection.execute(
            _FIND_SESSION,
            {
                "token_hash": token_hash,
                "refresh_threshold": SESSION_LIFETIME - SESSION_REFRESH_INTERVAL,
            },
        )
    ).one_or_none()
    if row is None:
        return None
    return StoredSession(
        id=row.id, user_id=row.user_id, csrf_token=row.csrf_token, is_refresh_due=row.is_refresh_due
    )


async def extend_session(connection: AsyncConnection, session_id: uuid.UUID) -> None:
    await connection.execute(_EXTEND_SESSION, {"id": session_id, "lifetime": SESSION_LIFETIME})


async def delete_session(connection: AsyncConnection, session_id: uuid.UUID) -> bool:
    """False when there was no such session, as when a concurrent request deleted it first."""
    result = await connection.execute(_DELETE_SESSION, {"id": session_id})
    return result.rowcount == 1
