"""User isolation as the application role: which rows of ``users`` and ``workspace_members`` a
transaction can read and write, depending on who it acts for. Also pins how Postgres applies these
policies to INSERT, which the seed and signup rely on."""

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.database.engine import create_database_engine
from app.database.tenancy import (
    login_transaction,
    member_transaction,
    user_transaction,
    workspace_transaction,
)
from support.tenants import TwoTenants

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("schema_at_head")]

VISIBLE_USERS = text("SELECT id FROM users")
VISIBLE_MEMBERSHIPS = text("SELECT workspace_id, user_id FROM workspace_members")
USER_EMAIL = text("SELECT email FROM users WHERE id = :id")
USER_PASSWORD_HASH = text("SELECT password_hash FROM users WHERE id = :id")
SET_PASSWORD_HASH = text("UPDATE users SET password_hash = 'changed' WHERE id = :id")
INSERT_MEMBER = text(
    "INSERT INTO workspace_members (workspace_id, user_id, role)"
    " VALUES (:workspace_id, :user_id, 'owner')"
)
BACKEND_PID = text("SELECT pg_backend_pid()")

INSERT_USER = "INSERT INTO users (id, email) VALUES (:id, :email)"
# Each of these makes Postgres apply the SELECT policy to the proposed row.
INSERTS_THAT_READ_THE_NEW_ROW = {
    "returning": f"{INSERT_USER} RETURNING id",
    "on-conflict-id": f"{INSERT_USER} ON CONFLICT (id) DO NOTHING",
}
ROW_SECURITY_VIOLATION = 'new row violates row-level security policy for table "users"'


async def visible_users(connection: AsyncConnection) -> set[uuid.UUID]:
    return set((await connection.execute(VISIBLE_USERS)).scalars().all())


async def visible_memberships(connection: AsyncConnection) -> set[tuple[uuid.UUID, uuid.UUID]]:
    return {
        (row.workspace_id, row.user_id) for row in await connection.execute(VISIBLE_MEMBERSHIPS)
    }


async def email_of(superuser_engine: AsyncEngine, user_id: uuid.UUID) -> str:
    async with superuser_engine.connect() as connection:
        email: str = (await connection.execute(USER_EMAIL, {"id": user_id})).scalar_one()
    return email


async def password_hash_of(superuser_engine: AsyncEngine, user_id: uuid.UUID) -> str | None:
    async with superuser_engine.connect() as connection:
        password_hash: str | None = (
            await connection.execute(USER_PASSWORD_HASH, {"id": user_id})
        ).scalar_one()
    return password_hash


async def user_exists(superuser_engine: AsyncEngine, user_id: uuid.UUID) -> bool:
    async with superuser_engine.connect() as connection:
        return (await connection.execute(USER_EMAIL, {"id": user_id})).first() is not None


def user_values(user_id: uuid.UUID) -> dict[str, object]:
    return {"id": user_id, "email": f"user-{user_id}@example.test"}


@pytest.fixture
async def single_connection_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_database_engine(SecretStr(os.environ["DATABASE_URL"]), pool_size=1)
    yield engine
    await engine.dispose()


async def test_no_user_is_visible_without_acting_for_one(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    # The negative control: both users really exist.
    assert await user_exists(superuser_engine, two_tenants.first.user_id)
    assert await user_exists(superuser_engine, two_tenants.second.user_id)

    async with app_engine.connect() as connection:
        assert await visible_users(connection) == set()
    # A workspace does not reveal its members' user rows.
    async with workspace_transaction(app_engine, two_tenants.first.workspace_id) as connection:
        assert await visible_users(connection) == set()


async def test_user_sees_and_updates_only_its_own_row(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    acting, other = two_tenants.first, two_tenants.second

    async with user_transaction(app_engine, acting.user_id) as connection:
        assert await visible_users(connection) == {acting.user_id}
        assert (await connection.execute(USER_EMAIL, {"id": other.user_id})).all() == []
        assert (await connection.execute(SET_PASSWORD_HASH, {"id": other.user_id})).rowcount == 0
        # The negative control: the same statement does change the user's own row.
        assert (await connection.execute(SET_PASSWORD_HASH, {"id": acting.user_id})).rowcount == 1

    assert await password_hash_of(superuser_engine, other.user_id) is None
    assert await password_hash_of(superuser_engine, acting.user_id) == "changed"


async def test_member_transaction_sees_only_its_own_user_row(
    app_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    acting = two_tenants.first
    async with member_transaction(app_engine, acting.workspace_id, acting.user_id) as connection:
        assert await visible_users(connection) == {acting.user_id}


async def test_user_cannot_take_another_id(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    acting = two_tenants.first
    with pytest.raises(DBAPIError, match=ROW_SECURITY_VIOLATION):
        async with user_transaction(app_engine, acting.user_id) as connection:
            await connection.execute(
                text("UPDATE users SET id = :new_id WHERE id = :id"),
                {"new_id": uuid.uuid4(), "id": acting.user_id},
            )
    assert await user_exists(superuser_engine, acting.user_id)


async def test_login_sees_exactly_the_user_with_that_email_and_cannot_update_it(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    user_id = two_tenants.first.user_id
    email = await email_of(superuser_engine, user_id)
    differently_cased = email.upper()
    assert differently_cased != email

    for login_email in (email, differently_cased):
        async with login_transaction(app_engine, login_email) as connection:
            assert await visible_users(connection) == {user_id}
            assert (await connection.execute(SET_PASSWORD_HASH, {"id": user_id})).rowcount == 0
            # Knowing an email reveals the user, not the workspaces the user belongs to.
            assert await visible_memberships(connection) == set()
    assert await password_hash_of(superuser_engine, user_id) is None

    async with login_transaction(app_engine, f"nobody-{uuid.uuid4()}@example.test") as connection:
        assert await visible_users(connection) == set()


async def test_user_sees_its_own_memberships_and_a_workspace_sees_only_its_members(
    app_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    guest, host = two_tenants.first, two_tenants.second
    async with workspace_transaction(app_engine, host.workspace_id) as connection:
        await connection.execute(
            INSERT_MEMBER, {"workspace_id": host.workspace_id, "user_id": guest.user_id}
        )
    guest_at_home = (guest.workspace_id, guest.user_id)
    guest_at_host = (host.workspace_id, guest.user_id)
    host_at_home = (host.workspace_id, host.user_id)

    async with user_transaction(app_engine, guest.user_id) as connection:
        assert await visible_memberships(connection) == {guest_at_home, guest_at_host}
    # Sharing a workspace with the guest does not show the host the guest's other memberships.
    async with user_transaction(app_engine, host.user_id) as connection:
        assert await visible_memberships(connection) == {host_at_home}

    async with workspace_transaction(app_engine, guest.workspace_id) as connection:
        assert await visible_memberships(connection) == {guest_at_home}
    async with workspace_transaction(app_engine, host.workspace_id) as connection:
        assert await visible_memberships(connection) == {host_at_home, guest_at_host}

    # Acting for both adds the user's memberships elsewhere, and no other workspace's members.
    async with member_transaction(app_engine, guest.workspace_id, guest.user_id) as connection:
        assert await visible_memberships(connection) == {guest_at_home, guest_at_host}
    async with member_transaction(app_engine, host.workspace_id, host.user_id) as connection:
        assert await visible_memberships(connection) == {host_at_home, guest_at_host}


async def test_user_cannot_write_memberships_without_the_workspace(
    app_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    acting, other = two_tenants.first, two_tenants.second
    with pytest.raises(
        DBAPIError, match='new row violates row-level security policy for table "workspace_members"'
    ):
        async with user_transaction(app_engine, acting.user_id) as connection:
            await connection.execute(
                INSERT_MEMBER, {"workspace_id": other.workspace_id, "user_id": acting.user_id}
            )


async def test_duplicate_email_of_an_invisible_user_violates_the_unique_constraint(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    existing_email = await email_of(superuser_engine, two_tenants.first.user_id)
    duplicate_id = uuid.uuid4()
    duplicate = {"id": duplicate_id, "email": existing_email.upper()}

    with pytest.raises(IntegrityError, match='violates unique constraint "users_email_key"'):
        async with workspace_transaction(app_engine, two_tenants.second.workspace_id) as connection:
            assert await visible_users(connection) == set()
            await connection.execute(text(INSERT_USER), duplicate)
    assert not await user_exists(superuser_engine, duplicate_id)


async def test_plain_insert_needs_no_user(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine
) -> None:
    user_id = uuid.uuid4()
    async with app_engine.begin() as connection:
        inserted = await connection.execute(text(INSERT_USER), user_values(user_id))
        assert inserted.rowcount == 1
        # The transaction cannot read back the row it just inserted.
        assert await visible_users(connection) == set()
    assert await user_exists(superuser_engine, user_id)


@pytest.mark.parametrize(
    "statement", INSERTS_THAT_READ_THE_NEW_ROW.values(), ids=INSERTS_THAT_READ_THE_NEW_ROW
)
async def test_insert_that_reads_the_new_row_is_rejected_unless_acting_for_that_user(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, two_tenants: TwoTenants, statement: str
) -> None:
    user_id = uuid.uuid4()
    user = user_values(user_id)
    workspace_id = two_tenants.first.workspace_id

    with pytest.raises(DBAPIError, match=ROW_SECURITY_VIOLATION):
        async with app_engine.begin() as connection:
            await connection.execute(text(statement), user)
    with pytest.raises(DBAPIError, match=ROW_SECURITY_VIOLATION):
        async with workspace_transaction(app_engine, workspace_id) as connection:
            await connection.execute(text(statement), user)
    with pytest.raises(DBAPIError, match=ROW_SECURITY_VIOLATION):
        async with user_transaction(app_engine, two_tenants.first.user_id) as connection:
            await connection.execute(text(statement), user)
    assert not await user_exists(superuser_engine, user_id)

    async with member_transaction(app_engine, workspace_id, user_id) as connection:
        await connection.execute(text(statement), user)
    assert await user_exists(superuser_engine, user_id)


async def test_on_conflict_id_do_nothing_is_checked_even_when_the_user_already_exists(
    app_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    """What the seed relies on: its second run must also act for the demo user."""
    statement = text(INSERTS_THAT_READ_THE_NEW_ROW["on-conflict-id"])
    user_id = uuid.uuid4()
    user = user_values(user_id)

    async with user_transaction(app_engine, user_id) as connection:
        assert (await connection.execute(statement, user)).rowcount == 1
        assert (await connection.execute(statement, user)).rowcount == 0
    with pytest.raises(DBAPIError, match=ROW_SECURITY_VIOLATION):
        async with workspace_transaction(app_engine, two_tenants.first.workspace_id) as connection:
            await connection.execute(statement, user)


async def test_on_conflict_do_nothing_without_a_target_is_not_checked_against_the_select_policy(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine
) -> None:
    statement = text(f"{INSERT_USER} ON CONFLICT DO NOTHING")
    user = user_values(uuid.uuid4())
    other_id = uuid.uuid4()
    same_email_new_id = {**user, "id": other_id}

    async with app_engine.begin() as connection:
        assert (await connection.execute(statement, user)).rowcount == 1
    # The conflicting row is invisible, and the statement still reports that nothing was inserted.
    async with app_engine.begin() as connection:
        assert (await connection.execute(statement, user)).rowcount == 0
        assert (await connection.execute(statement, same_email_new_id)).rowcount == 0
    assert not await user_exists(superuser_engine, other_id)


async def test_who_a_transaction_acts_for_does_not_survive_it_on_a_pooled_connection(
    single_connection_engine: AsyncEngine, superuser_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    engine = single_connection_engine
    tenant = two_tenants.first
    email = await email_of(superuser_engine, tenant.user_id)

    async def visible_afterwards() -> tuple[int, set[uuid.UUID], set[tuple[uuid.UUID, uuid.UUID]]]:
        async with engine.connect() as connection:
            backend_pid = (await connection.execute(BACKEND_PID)).scalar_one()
            return (
                backend_pid,
                await visible_users(connection),
                await visible_memberships(connection),
            )

    async with user_transaction(engine, tenant.user_id) as connection:
        backend_pid = (await connection.execute(BACKEND_PID)).scalar_one()
        # The negative control: on this connection the user really was visible.
        assert await visible_users(connection) == {tenant.user_id}
        assert await visible_memberships(connection) == {(tenant.workspace_id, tenant.user_id)}
    assert await visible_afterwards() == (backend_pid, set(), set())

    async with login_transaction(engine, email) as connection:
        assert await visible_users(connection) == {tenant.user_id}
    assert await visible_afterwards() == (backend_pid, set(), set())

    async with member_transaction(engine, tenant.workspace_id, tenant.user_id) as connection:
        assert await visible_users(connection) == {tenant.user_id}
    assert await visible_afterwards() == (backend_pid, set(), set())
