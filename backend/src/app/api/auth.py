"""HTTP routes for signup, login, logout and the current user's identity."""

import ipaddress
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, SecretStr, StringConstraints
from sqlalchemy.ext.asyncio import AsyncEngine

from app.audit import IPAddress
from app.identity.authentication import (
    AuthenticatedSession,
    SessionDependency,
    clear_session_cookie,
    not_authenticated,
    set_session_cookie,
)
from app.identity.login_rate_limit import LoginRateLimiter
from app.identity.passwords import PasswordHasher
from app.identity.service import (
    EmailAlreadyRegisteredError,
    Identity,
    InvalidCredentialsError,
    LoginRateLimitedError,
    SessionAlreadyEndedError,
    log_in,
    log_out,
    read_identity,
    sign_up,
)

MAXIMUM_EMAIL_LENGTH = 254
MINIMUM_PASSWORD_LENGTH = 12
# Bounds the work an attacker can make the hasher do with one request.
MAXIMUM_PASSWORD_LENGTH = 1024

# Control characters are refused here because Postgres cannot store NUL, which would otherwise
# surface as a 500 after the hash has already been paid for.
EMAIL_PART = r"[^@\s\x00-\x1f\x7f]+"
Email = Annotated[
    str,
    StringConstraints(max_length=MAXIMUM_EMAIL_LENGTH, pattern=rf"^{EMAIL_PART}@{EMAIL_PART}$"),
]


class SignupRequest(BaseModel):
    email: Email
    password: Annotated[
        SecretStr, Field(min_length=MINIMUM_PASSWORD_LENGTH, max_length=MAXIMUM_PASSWORD_LENGTH)
    ]


class LoginRequest(BaseModel):
    email: Email
    password: Annotated[SecretStr, Field(min_length=1, max_length=MAXIMUM_PASSWORD_LENGTH)]


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str


class WorkspaceResponse(BaseModel):
    id: uuid.UUID
    name: str


class SessionResponse(BaseModel):
    user: UserResponse
    workspace: WorkspaceResponse
    csrf_token: str


def _session_response(identity: Identity, csrf_token: str) -> SessionResponse:
    return SessionResponse(
        user=UserResponse(id=identity.user_id, email=identity.email),
        workspace=WorkspaceResponse(id=identity.workspace_id, name=identity.workspace_name),
        csrf_token=csrf_token,
    )


def _peer_address(request: Request) -> IPAddress | None:
    """The address of the socket's other end. X-Forwarded-For is never read, because any client
    can set it; this holds only while uvicorn runs with --no-proxy-headers, as backend/Dockerfile
    starts it, since uvicorn otherwise rewrites request.client from that header."""
    # Starlette has no client for a connection without a peer address, such as a Unix socket.
    if request.client is None:
        return None
    return ipaddress.ip_address(request.client.host)


def _email_already_registered() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "email_already_registered",
            "message": "An account with this email already exists.",
        },
    )


def _invalid_credentials() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "invalid_credentials", "message": "The email or password is incorrect."},
    )


def _login_rate_limited(retry_after_seconds: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "code": "login_rate_limited",
            "message": "Too many login attempts. Try again later.",
        },
        headers={"Retry-After": str(retry_after_seconds)},
    )


def build_auth_router(
    engine: AsyncEngine,
    password_hasher: PasswordHasher,
    login_rate_limiter: LoginRateLimiter,
    authenticated_session: SessionDependency,
    *,
    session_cookie_secure: bool,
) -> APIRouter:
    router = APIRouter()

    @router.post("/auth/signup", status_code=status.HTTP_201_CREATED)
    async def signup(body: SignupRequest, response: Response) -> SessionResponse:
        try:
            started = await sign_up(
                engine, password_hasher, email=body.email, password=body.password
            )
        except EmailAlreadyRegisteredError:
            raise _email_already_registered() from None
        set_session_cookie(response, started.cookie, secure=session_cookie_secure)
        return _session_response(started.identity, started.csrf_token)

    @router.post("/auth/login")
    async def login(body: LoginRequest, request: Request, response: Response) -> SessionResponse:
        try:
            started = await log_in(
                engine,
                password_hasher,
                login_rate_limiter,
                email=body.email,
                password=body.password,
                ip=_peer_address(request),
            )
        except LoginRateLimitedError as error:
            raise _login_rate_limited(error.retry_after_seconds) from None
        except InvalidCredentialsError:
            raise _invalid_credentials() from None
        set_session_cookie(response, started.cookie, secure=session_cookie_secure)
        return _session_response(started.identity, started.csrf_token)

    @router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        request: Request,
        session: Annotated[AuthenticatedSession, Depends(authenticated_session)],
    ) -> Response:
        try:
            await log_out(engine, session, ip=_peer_address(request))
        except SessionAlreadyEndedError:
            raise not_authenticated() from None
        # A new response rather than the injected one, so a cookie the session dependency re-issued
        # for a due refresh is not sent beside the deletion.
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        clear_session_cookie(response, secure=session_cookie_secure)
        return response

    @router.get("/me")
    async def me(
        session: Annotated[AuthenticatedSession, Depends(authenticated_session)],
    ) -> SessionResponse:
        return _session_response(await read_identity(engine, session), session.csrf_token)

    return router
