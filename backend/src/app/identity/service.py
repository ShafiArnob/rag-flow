"""Signup, login, logout and the identity a session acts for."""

import logging
import uuid
from dataclasses import dataclass

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import EmulatedDBAPIException, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.audit import AuditAction, IPAddress, record_audit_event
from app.database.tenancy import (
    login_transaction,
    member_transaction,
    user_transaction,
    workspace_transaction,
)
from app.identity.authentication import AuthenticatedSession
from app.identity.login_rate_limit import LoginRateLimiter, email_account_key, user_account_key
from app.identity.passwords import PasswordHasher
from app.identity.sessions import (
    SessionCookie,
    create_session,
    delete_session,
    hash_session_token,
    new_csrf_token,
    new_session_token,
)

logger = logging.getLogger(__name__)

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


class EmailAlreadyRegisteredError(Exception):
    def __init__(self) -> None:
        super().__init__("signup failed: an account with this email already exists")


class InvalidCredentialsError(Exception):
    def __init__(self) -> None:
        super().__init__("login failed: the email or password is incorrect")


class LoginRateLimitedError(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"login failed: rate limited for {retry_after_seconds} more seconds")
        self.retry_after_seconds = retry_after_seconds


class SessionAlreadyEndedError(Exception):
    def __init__(self, session_id: uuid.UUID) -> None:
        super().__init__(f"logout failed: session {session_id} has already ended")


@dataclass(frozen=True)
class Identity:
    user_id: uuid.UUID
    email: str
    workspace_id: uuid.UUID
    workspace_name: str


@dataclass(frozen=True)
class StartedSession:
    identity: Identity
    cookie: SessionCookie
    csrf_token: str


def _violated_constraint(error: IntegrityError) -> str | None:
    if not isinstance(error.orig, EmulatedDBAPIException):
        return None
    # asyncpg reports the constraint on its own exception, which SQLAlchemy keeps as the driver's.
    constraint: str | None = getattr(error.orig.driver_exception, "constraint_name", None)
    return constraint


async def sign_up(
    engine: AsyncEngine, password_hasher: PasswordHasher, *, email: str, password: SecretStr
) -> StartedSession:
    """Creates the user, a personal workspace they own, and a session, all or nothing."""
    password_hash = await password_hasher.hash(password.get_secret_value())
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
                _INSERT_USER, {"id": user_id, "email": email, "password_hash": password_hash}
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
        raise EmailAlreadyRegisteredError from None
    return StartedSession(
        identity=Identity(
            user_id=user_id,
            email=email,
            workspace_id=workspace_id,
            workspace_name=PERSONAL_WORKSPACE_NAME,
        ),
        cookie=SessionCookie(workspace_id=workspace_id, token=token),
        csrf_token=csrf_token,
    )


async def log_in(
    engine: AsyncEngine,
    password_hasher: PasswordHasher,
    login_rate_limiter: LoginRateLimiter,
    *,
    email: str,
    password: SecretStr,
    ip: IPAddress | None,
) -> StartedSession:
    """Starts a session for the account with this email and password, within the login limits.

    An unknown email, a wrong password and an account without a password all raise the same
    InvalidCredentialsError, after the same work, so the caller cannot tell which accounts exist.
    """
    async with login_transaction(engine, email) as connection:
        account = (await connection.execute(_ACCOUNT_WITH_EMAIL, {"email": email})).one_or_none()
    account_key = email_account_key(email) if account is None else user_account_key(account.id)
    # Counted before the password is verified, so parallel guesses cannot exceed the limit.
    retry_after_seconds = await login_rate_limiter.register_attempt(ip, account_key)
    if retry_after_seconds is not None:
        raise LoginRateLimitedError(retry_after_seconds)
    is_password_valid = await password_hasher.verify(
        None if account is None else account.password_hash, password.get_secret_value()
    )
    if account is None or not is_password_valid:
        logger.warning(
            "login failed: invalid credentials",
            extra={"user_id": None if account is None else str(account.id)},
        )
        raise InvalidCredentialsError
    # Before any write, so a Redis failure here leaves neither a session nor a login audit row.
    await login_rate_limiter.clear_account(account_key)

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
    return StartedSession(
        identity=Identity(
            user_id=account.id,
            email=account.email,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
        ),
        cookie=SessionCookie(workspace_id=workspace_id, token=token),
        csrf_token=csrf_token,
    )


async def log_out(
    engine: AsyncEngine, session: AuthenticatedSession, *, ip: IPAddress | None
) -> None:
    """Ends the session and records the logout, together."""
    async with workspace_transaction(engine, session.workspace_id) as connection:
        if not await delete_session(connection, session.session_id):
            # A concurrent logout deleted it after the lookup and has recorded that logout.
            raise SessionAlreadyEndedError(session.session_id)
        await record_audit_event(
            connection,
            workspace_id=session.workspace_id,
            actor_id=session.user_id,
            action=AuditAction.LOGOUT,
            target=None,
            ip=ip,
        )


async def read_identity(engine: AsyncEngine, session: AuthenticatedSession) -> Identity:
    async with member_transaction(engine, session.workspace_id, session.user_id) as connection:
        row = (
            await connection.execute(
                _USER_EMAIL_AND_WORKSPACE_NAME,
                {"user_id": session.user_id, "workspace_id": session.workspace_id},
            )
        ).one()
    return Identity(
        user_id=session.user_id,
        email=row.email,
        workspace_id=session.workspace_id,
        workspace_name=row.name,
    )
