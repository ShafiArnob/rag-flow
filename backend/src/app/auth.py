"""Signup, login, logout and the current user's identity."""

import ipaddress
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, SecretStr, StringConstraints
from sqlalchemy import text
from sqlalchemy.exc import EmulatedDBAPIException, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.audit import AuditAction, IPAddress, record_audit_event
from app.authentication import (
    AuthenticatedSession,
    SessionDependency,
    clear_session_cookie,
    not_authenticated,
    set_session_cookie,
)
from app.login_rate_limit import LoginRateLimiter, email_account_key, user_account_key
from app.passwords import PasswordHasher
from app.sessions import (
    SessionCookie,
    create_session,
    delete_session,
    hash_session_token,
    new_csrf_token,
    new_session_token,
)
from app.tenancy import (
    login_transaction,
    member_transaction,
    user_transaction,
    workspace_transaction,
)

logger = logging.getLogger(__name__)

MAXIMUM_EMAIL_LENGTH = 254
MINIMUM_PASSWORD_LENGTH = 12
# Bounds the work an attacker can make the hasher do with one request.
MAXIMUM_PASSWORD_LENGTH = 1024
PERSONAL_WORKSPACE_NAME = "Personal"
OWNER_MEMBER_ROLE = "owner"
USERS_EMAIL_CONSTRAINT = "users_email_key"

# No RETURNING and no ON CONFLICT on the user insert: either makes Postgres check the new row
# against the SELECT policy of users, which a transaction acting for no user fails.
_INSERT_WORKSPACE = text("INSERT INTO workspaces (id, name) VALUES (:id, :name)")
_INSERT_USER = text(
    "INSERT INTO users (id, email, password_hash) VALUES (:id, :email, :password_hash)"
)
_INSERT_MEMBER = text(
    "INSERT INTO workspace_members (workspace_id, user_id, role)"
    " VALUES (:workspace_id, :user_id, :role)"
)
_USER_EMAIL_AND_WORKSPACE_NAME = text(
    "SELECT users.email, workspaces.name FROM users, workspaces"
    " WHERE users.id = :user_id AND workspaces.id = :workspace_id"
)
_ACCOUNT_WITH_EMAIL = text("SELECT id, email, password_hash FROM users WHERE email = :email")
_WORKSPACE_OF_USER = text("SELECT workspace_id FROM workspace_members WHERE user_id = :user_id")
_WORKSPACE_NAME = text("SELECT name FROM workspaces WHERE id = :id")

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


def _violated_constraint(error: IntegrityError) -> str | None:
    if not isinstance(error.orig, EmulatedDBAPIException):
        return None
    # asyncpg reports the constraint on its own exception, which SQLAlchemy keeps as the driver's.
    constraint: str | None = getattr(error.orig.driver_exception, "constraint_name", None)
    return constraint


def _peer_address(request: Request) -> IPAddress | None:
    """The address of the socket's other end. X-Forwarded-For is never read, because any client
    can set it; this holds only while uvicorn runs with --no-proxy-headers, as backend/Dockerfile
    starts it, since uvicorn otherwise rewrites request.client from that header."""
    # Starlette has no client for a connection without a peer address, such as a Unix socket.
    if request.client is None:
        return None
    return ipaddress.ip_address(request.client.host)


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
        """Creates the user, a personal workspace they own, and a session, all or nothing."""
        password_hash = await password_hasher.hash(body.password.get_secret_value())
        # Generated here because the user row cannot be read back (see _INSERT_USER).
        workspace_id = uuid.uuid4()
        user_id = uuid.uuid4()
        token = new_session_token()
        csrf_token = new_csrf_token()
        try:
            async with workspace_transaction(engine, workspace_id) as connection:
                await connection.execute(
                    _INSERT_WORKSPACE, {"id": workspace_id, "name": PERSONAL_WORKSPACE_NAME}
                )
                await connection.execute(
                    _INSERT_USER,
                    {"id": user_id, "email": body.email, "password_hash": password_hash},
                )
                await connection.execute(
                    _INSERT_MEMBER,
                    {"workspace_id": workspace_id, "user_id": user_id, "role": OWNER_MEMBER_ROLE},
                )
                await create_session(
                    connection,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    token_hash=hash_session_token(token),
                    csrf_token=csrf_token,
                )
        except IntegrityError as error:
            if _violated_constraint(error) != USERS_EMAIL_CONSTRAINT:
                raise
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "email_already_registered",
                    "message": "An account with this email already exists.",
                },
            ) from None
        set_session_cookie(
            response,
            SessionCookie(workspace_id=workspace_id, token=token),
            secure=session_cookie_secure,
        )
        return SessionResponse(
            user=UserResponse(id=user_id, email=body.email),
            workspace=WorkspaceResponse(id=workspace_id, name=PERSONAL_WORKSPACE_NAME),
            csrf_token=csrf_token,
        )

    @router.post("/auth/login")
    async def login(body: LoginRequest, request: Request, response: Response) -> SessionResponse:
        """Starts a session for the account with this email and password, within the login limits.

        An unknown email, a wrong password and an account without a password all get the same 401,
        after the same work, so the response does not reveal which accounts exist.
        """
        async with login_transaction(engine, body.email) as connection:
            account = (
                await connection.execute(_ACCOUNT_WITH_EMAIL, {"email": body.email})
            ).one_or_none()
        ip = _peer_address(request)
        account_key = (
            email_account_key(body.email) if account is None else user_account_key(account.id)
        )
        # Counted before the password is verified, so parallel guesses cannot exceed the limit.
        retry_after_seconds = await login_rate_limiter.register_attempt(ip, account_key)
        if retry_after_seconds is not None:
            raise _login_rate_limited(retry_after_seconds)
        is_password_valid = await password_hasher.verify(
            None if account is None else account.password_hash, body.password.get_secret_value()
        )
        if account is None or not is_password_valid:
            logger.warning(
                "login failed: invalid credentials",
                extra={"user_id": None if account is None else str(account.id)},
            )
            raise _invalid_credentials()

        async with user_transaction(engine, account.id) as connection:
            # Every user has exactly one membership, the personal workspace signup creates.
            workspace_id: uuid.UUID = (
                await connection.execute(_WORKSPACE_OF_USER, {"user_id": account.id})
            ).scalar_one()
        token = new_session_token()
        csrf_token = new_csrf_token()
        async with workspace_transaction(engine, workspace_id) as connection:
            await create_session(
                connection,
                workspace_id=workspace_id,
                user_id=account.id,
                token_hash=hash_session_token(token),
                csrf_token=csrf_token,
            )
            await record_audit_event(
                connection,
                workspace_id=workspace_id,
                actor_id=account.id,
                action=AuditAction.LOGIN,
                target=None,
                ip=ip,
            )
            workspace_name: str = (
                await connection.execute(_WORKSPACE_NAME, {"id": workspace_id})
            ).scalar_one()
        await login_rate_limiter.clear_account(account_key)
        set_session_cookie(
            response,
            SessionCookie(workspace_id=workspace_id, token=token),
            secure=session_cookie_secure,
        )
        return SessionResponse(
            user=UserResponse(id=account.id, email=account.email),
            workspace=WorkspaceResponse(id=workspace_id, name=workspace_name),
            csrf_token=csrf_token,
        )

    @router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        request: Request,
        session: Annotated[AuthenticatedSession, Depends(authenticated_session)],
    ) -> Response:
        """Ends the session and records the logout, together."""
        async with workspace_transaction(engine, session.workspace_id) as connection:
            if not await delete_session(connection, session.session_id):
                # A concurrent logout deleted it after the lookup and has recorded that logout.
                raise not_authenticated()
            await record_audit_event(
                connection,
                workspace_id=session.workspace_id,
                actor_id=session.user_id,
                action=AuditAction.LOGOUT,
                target=None,
                ip=_peer_address(request),
            )
        # A new response rather than the injected one, so a cookie the session dependency re-issued
        # for a due refresh is not sent beside the deletion.
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        clear_session_cookie(response, secure=session_cookie_secure)
        return response

    @router.get("/me")
    async def me(
        session: Annotated[AuthenticatedSession, Depends(authenticated_session)],
    ) -> SessionResponse:
        async with member_transaction(engine, session.workspace_id, session.user_id) as connection:
            row = (
                await connection.execute(
                    _USER_EMAIL_AND_WORKSPACE_NAME,
                    {"user_id": session.user_id, "workspace_id": session.workspace_id},
                )
            ).one()
        return SessionResponse(
            user=UserResponse(id=session.user_id, email=row.email),
            workspace=WorkspaceResponse(id=session.workspace_id, name=row.name),
            csrf_token=session.csrf_token,
        )

    return router
