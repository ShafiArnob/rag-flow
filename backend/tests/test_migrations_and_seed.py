"""Migrations run as the owner role, through env.py, which runs its own event loop. These tests are
therefore synchronous and call their async helpers through asyncio.run."""

import asyncio
import itertools
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import SecretStr
from sqlalchemy import text

from app.database import create_database_engine
from app.seed import DEMO_USER_EMAIL, DEMO_WORKSPACE_ID, DEMO_WORKSPACE_NAME, seed_demo_data
from app.tenancy import workspace_transaction

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).parent.parent / "alembic.ini"

# Every revision in order, from base. Add each new revision here; the step test fails until you do.
REVISION_STEPS = ("base", "0001", "0002", "0003", "0004")

SEEDED_TABLES = ("workspaces", "users", "workspace_members")

CatalogSnapshot = dict[str, list[tuple[object, ...]]]

# Everything the migrations create in schema public, in a form that does not depend on object ids.
# Privileges are expanded with acldefault, so a NULL ACL equals the explicit default it stands for.
CATALOG_QUERIES = {
    "relations": """
        SELECT c.relname, c.relkind, c.relrowsecurity, c.relforcerowsecurity,
            pg_get_userbyid(c.relowner)
        FROM pg_class c
        WHERE c.relnamespace = 'public'::regnamespace
        ORDER BY 1
    """,
    "columns": """
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
            pg_get_expr(d.adbin, d.adrelid)
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE c.relnamespace = 'public'::regnamespace AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY c.relname, a.attnum
    """,
    "constraints": """
        SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE connamespace = 'public'::regnamespace
        ORDER BY 1, 2
    """,
    "indexes": """
        SELECT tablename, indexname, indexdef FROM pg_indexes
        WHERE schemaname = 'public'
        ORDER BY 1, 2
    """,
    "policies": """
        SELECT tablename, policyname, permissive, roles::text, cmd, qual, with_check
        FROM pg_policies
        WHERE schemaname = 'public'
        ORDER BY 1, 2
    """,
    "table_privileges": """
        SELECT c.relname, privilege.grantee::regrole::text, privilege.privilege_type
        FROM pg_class c,
            aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) AS privilege
        WHERE c.relnamespace = 'public'::regnamespace
        ORDER BY 1, 2, 3
    """,
    "column_privileges": """
        SELECT c.relname, a.attname, privilege.grantee::regrole::text, privilege.privilege_type
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid,
            aclexplode(a.attacl) AS privilege
        WHERE c.relnamespace = 'public'::regnamespace AND a.attacl IS NOT NULL
        ORDER BY 1, 2, 3, 4
    """,
    "functions": """
        SELECT pg_get_function_identity_arguments(p.oid), pg_get_functiondef(p.oid),
            pg_get_userbyid(p.proowner)
        FROM pg_proc p
        WHERE p.pronamespace = 'public'::regnamespace
            AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
        ORDER BY 2
    """,
    "function_privileges": """
        SELECT p.proname, privilege.grantee::regrole::text, privilege.privilege_type
        FROM pg_proc p,
            aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) AS privilege
        WHERE p.pronamespace = 'public'::regnamespace
            AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
        ORDER BY 1, 2, 3
    """,
    "default_privileges": """
        SELECT pg_get_userbyid(defaclrole), defaclobjtype, defaclacl::text FROM pg_default_acl
        ORDER BY 1, 2
    """,
}


def alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


async def catalog_snapshot() -> CatalogSnapshot:
    engine = create_database_engine(SecretStr(os.environ["MIGRATION_DATABASE_URL"]))
    try:
        async with engine.connect() as connection:
            return {
                name: [tuple(row) for row in (await connection.execute(text(query))).all()]
                for name, query in CATALOG_QUERIES.items()
            }
    finally:
        await engine.dispose()


async def row_counts() -> dict[str, int]:
    # Counted as the superuser: FORCE ROW LEVEL SECURITY hides every tenant row from the owner too,
    # and only a role that bypasses row-level security sees all workspaces at once.
    engine = create_database_engine(SecretStr(os.environ["BOOTSTRAP_DATABASE_URL"]))
    try:
        async with engine.connect() as connection:
            return {
                table: (
                    await connection.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
                ).scalar_one()
                for table in SEEDED_TABLES
            }
    finally:
        await engine.dispose()


async def seed_twice_as_app_role() -> list[tuple[object, ...]]:
    engine = create_database_engine(SecretStr(os.environ["DATABASE_URL"]))
    try:
        await seed_demo_data(engine)
        await seed_demo_data(engine)
        async with workspace_transaction(engine, DEMO_WORKSPACE_ID) as connection:
            members = await connection.execute(
                text(
                    "SELECT u.email, w.name FROM workspace_members m"
                    " JOIN users u ON u.id = m.user_id"
                    " JOIN workspaces w ON w.id = m.workspace_id"
                )
            )
            return [tuple(member) for member in members.all()]
    finally:
        await engine.dispose()


def test_migrations_round_trip_base_to_head_as_owner_and_seed_is_idempotent() -> None:
    config = alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    assert asyncio.run(seed_twice_as_app_role()) == [(DEMO_USER_EMAIL, DEMO_WORKSPACE_NAME)]
    assert asyncio.run(row_counts()) == dict.fromkeys(SEEDED_TABLES, 1)

    # Downgrading with rows present proves every downgrade drops what holds them.
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    assert asyncio.run(row_counts()) == dict.fromkeys(SEEDED_TABLES, 0)


def test_each_core_revision_downgrades_and_reapplies() -> None:
    config = alembic_config()
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head == REVISION_STEPS[-1], f"add revision {head} to REVISION_STEPS"

    # Start from base: which run partitions exist at head, and the names of the foreign keys cloned
    # for them, depend on when and in what order they were created, so only a fresh upgrade gives a
    # head snapshot that a re-upgrade reproduces.
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    snapshots = {head: asyncio.run(catalog_snapshot())}
    for revision in reversed(REVISION_STEPS[:-1]):
        command.downgrade(config, revision)
        snapshots[revision] = asyncio.run(catalog_snapshot())

    for below, revision in itertools.pairwise(REVISION_STEPS):
        # Each revision must change the catalog, or the comparison below proves nothing.
        assert snapshots[below] != snapshots[revision], f"revision {revision} changed nothing"
        command.upgrade(config, revision)
        assert asyncio.run(catalog_snapshot()) == snapshots[revision], f"revision {revision}"
