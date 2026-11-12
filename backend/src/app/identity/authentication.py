"""Resolves the session cookie into the session a request acts for, checks the CSRF token of
requests that can change state, and writes that cookie."""

import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Cookie, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncEngine

from app.database.tenancy import workspace_transaction
from app.identity.sessions import (
    SESSION_LIFETIME,
    SessionCookie,
    decode_session_cookie,
    encode_session_cookie,
    extend_session,
    find_session,
    hash_session_token,
)

SESSION_COOKIE_NAME = "flowbuilder_session"
CSRF_HEADER_NAME = "X-CSRF-Token"
# A cross-site page can make the browser send these with the session cookie attached, so each
# must also carry the token, which only a page of this site can read.
CSRF_PROTECTED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_COOKIE_PATH = "/"
_COOKIE_SAME_SITE: Literal["lax"] = "lax"


@dataclass(frozen=True)
class AuthenticatedSession:
    session_id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    csrf_token: str


SessionDependency = Callable[..., Awaitable[AuthenticatedSession]]


def set_session_cookie(response: Response, cookie: SessionCookie, *, secure: bool) -> None:
    # No Domain attribute, so the browser sends the cookie back only to the host that set it.
    response.set_cookie(
        SESSION_COOKIE_NAME,
        encode_session_cookie(cookie),
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path=_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite=_COOKIE_SAME_SITE,
    )


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    # The attributes set_session_cookie uses: a browser replaces a cookie only when its name, Path
    # and Domain match.
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path=_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite=_COOKIE_SAME_SITE,
    )


def not_authenticated() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "not_authenticated", "message": "Sign in to continue."},
    )


def _csrf_token_invalid() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "csrf_token_invalid",
            "message": f"Send this session's CSRF token in the {CSRF_HEADER_NAME} header.",
        },
    )


def _is_csrf_token_valid(sent: str | None, expected: str) -> bool:
    if sent is None:
        return False
    # Compared as bytes: compare_digest raises on a str with non-ASCII characters, which a header
    # value may hold.
    return secrets.compare_digest(sent.encode(), expected.encode())


def build_session_dependency(
    engine: AsyncEngine, *, session_cookie_secure: bool
) -> SessionDependency:
    """A dependency that yields the request's session, or responds 401 when there is none and 403
    when a request with a method in CSRF_PROTECTED_METHODS lacks the session's CSRF token.

    A cookie whose workspace id is not the session's own finds nothing, because the lookup runs in
    that workspace's transaction and row-level security hides every other workspace's sessions.
    """

    async def authenticated_session(
        request: Request,
        response: Response,
        session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
    ) -> AuthenticatedSession:
        cookie = None if session_cookie is None else decode_session_cookie(session_cookie)
        if cookie is None:
            raise not_authenticated()
        async with workspace_transaction(engine, cookie.workspace_id) as connection:
            stored = await find_session(connection, hash_session_token(cookie.token))
            if stored is None:
                raise not_authenticated()
            # Read from the request rather than declared as a parameter, so the documentation of
            # safe routes does not list a header they ignore.
            sent_csrf_token = request.headers.get(CSRF_HEADER_NAME)
            if request.method in CSRF_PROTECTED_METHODS and not _is_csrf_token_valid(
                sent_csrf_token, stored.csrf_token
            ):
                # Before the refresh, so a forged request cannot keep a session alive.
                raise _csrf_token_invalid()
            if stored.is_refresh_due:
                await extend_session(connection, stored.id)
        if stored.is_refresh_due:
            # The browser's copy would otherwise expire on the original schedule.
            set_session_cookie(response, cookie, secure=session_cookie_secure)
        return AuthenticatedSession(
            session_id=stored.id,
            workspace_id=cookie.workspace_id,
            user_id=stored.user_id,
            csrf_token=stored.csrf_token,
        )

    return authenticated_session
