"""The catalog at head: every table is protected by row-level security or declared exempt, and every
privilege, owner and function matches schema_expectations."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.database_roles import DATABASE_APP_ROLE, DATABASE_OWNER_ROLE
from schema_expectations import (
    APP_EXECUTABLE_FUNCTIONS,
    APP_PRIVILEGES,
    EXTRA_POLICIES,
    RLS_EXEMPT_TABLES,
    GrantedPrivileges,
    tenant_policy_expression,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("schema_at_head")]

PARTITIONED_TABLES = ("runs", "run_steps")
# create_run_partitions() covers the current month and the 12 after it.
MINIMUM_PARTITIONS_PER_TABLE = 13

# Partitions are excluded: they carry no policy of their own, and are checked separately.
TABLES = text(
    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class"
    " WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p') AND NOT relispartition"
    " ORDER BY relname"
)
POLICIES = text(
    "SELECT tablename, policyname, permissive, roles::text, cmd, qual, with_check"
    " FROM pg_policies WHERE schemaname = 'public'"
)
# Index partitions are pg_inherits children too, but they hold no rows.
PARTITIONS = text(
    """
    SELECT c.relname, i.inhparent::regclass::text AS parent, c.relrowsecurity,
        has_table_privilege(CAST(:app_role AS name), c.oid,
            'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
        OR has_any_column_privilege(CAST(:app_role AS name), c.oid,
            'SELECT, INSERT, UPDATE, REFERENCES') AS app_has_privilege,
        EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid) AS has_policy
    FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
    WHERE c.relkind IN ('r', 'p')
    ORDER BY c.relname
    """
)
# Grants to anyone but the relation's owner, including PUBLIC, which prints as '-'.
TABLE_GRANTS = text(
    """
    SELECT privilege.grantee::regrole::text AS grantee, c.relname, privilege.privilege_type
    FROM pg_class c, aclexplode(c.relacl) AS privilege
    WHERE c.relnamespace = 'public'::regnamespace AND privilege.grantee <> c.relowner
    """
)
COLUMN_GRANTS = text(
    """
    SELECT privilege.grantee::regrole::text AS grantee, c.relname, a.attname,
        privilege.privilege_type
    FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid, aclexplode(a.attacl) AS privilege
    WHERE c.relnamespace = 'public'::regnamespace AND privilege.grantee <> c.relowner
    """
)
DEFAULT_PRIVILEGES = text("SELECT pg_get_userbyid(defaclrole), defaclobjtype FROM pg_default_acl")
# Both skip extension members (vector, citext), which the extension owns and maintains.
APP_EXECUTABLE = text(
    """
    SELECT p.proname FROM pg_proc p
    WHERE p.pronamespace = 'public'::regnamespace
        AND NOT EXISTS (
            SELECT 1 FROM pg_depend d
            WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
        )
        AND has_function_privilege(CAST(:app_role AS name), p.oid, 'EXECUTE')
    ORDER BY 1
    """
)
FUNCTION_OWNERS = text(
    """
    SELECT p.proname, pg_get_userbyid(p.proowner) FROM pg_proc p
    WHERE p.pronamespace = 'public'::regnamespace
        AND NOT EXISTS (
            SELECT 1 FROM pg_depend d
            WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
        )
    """
)
VIEWS = text(
    "SELECT relname FROM pg_class"
    " WHERE relnamespace = 'public'::regnamespace AND relkind IN ('v', 'm')"
)
SECURITY_DEFINER_FUNCTIONS = text(
    "SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND prosecdef"
)
RELATION_OWNERS = text(
    "SELECT relname, pg_get_userbyid(relowner) FROM pg_class"
    " WHERE relnamespace = 'public'::regnamespace"
)


async def test_every_relation_is_rls_protected_or_declared_exempt(
    owner_engine: AsyncEngine,
) -> None:
    async with owner_engine.connect() as connection:
        tables = (await connection.execute(TABLES)).all()
        policies = (await connection.execute(POLICIES)).all()

    table_names = {table.relname for table in tables}
    assert set(RLS_EXEMPT_TABLES) <= table_names, "an exempt table no longer exists"
    protected = sorted(table_names - set(RLS_EXEMPT_TABLES))
    assert [
        table.relname
        for table in tables
        if table.relname in protected and not (table.relrowsecurity and table.relforcerowsecurity)
    ] == []

    tenant_policies = {
        tuple(policy)
        for policy in policies
        if policy.policyname not in EXTRA_POLICIES.get(policy.tablename, frozenset())
    }
    assert tenant_policies == {
        (
            table,
            f"{table}_tenant",
            "PERMISSIVE",
            "{public}",
            "ALL",
            tenant_policy_expression(table),
            tenant_policy_expression(table),
        )
        for table in protected
    }


async def test_partitions_enable_rls_and_grant_app_nothing(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as connection:
        partitions = (await connection.execute(PARTITIONS, {"app_role": DATABASE_APP_ROLE})).all()

    partitions_per_table = {
        table: sum(partition.parent == table for partition in partitions)
        for table in PARTITIONED_TABLES
    }
    assert {partition.parent for partition in partitions} == set(PARTITIONED_TABLES)
    assert all(count >= MINIMUM_PARTITIONS_PER_TABLE for count in partitions_per_table.values()), (
        partitions_per_table
    )
    # Row-level security with no policy denies every row to anyone who queries a partition directly.
    assert [
        partition.relname
        for partition in partitions
        if not partition.relrowsecurity or partition.app_has_privilege or partition.has_policy
    ] == []


async def test_app_privileges_match_declaration(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as connection:
        table_grants = (await connection.execute(TABLE_GRANTS)).all()
        column_grants = (await connection.execute(COLUMN_GRANTS)).all()
        executable = (
            (await connection.execute(APP_EXECUTABLE, {"app_role": DATABASE_APP_ROLE}))
            .scalars()
            .all()
        )
        default_privileges = (await connection.execute(DEFAULT_PRIVILEGES)).all()

    assert {grant.grantee for grant in (*table_grants, *column_grants)} == {DATABASE_APP_ROLE}
    granted: dict[str, GrantedPrivileges] = {}
    for grant in table_grants:
        privileges = granted.get(grant.relname, GrantedPrivileges(frozenset()))
        granted[grant.relname] = GrantedPrivileges(
            privileges.on_table | {grant.privilege_type}, privileges.on_columns
        )
    for grant in column_grants:
        privileges = granted.get(grant.relname, GrantedPrivileges(frozenset()))
        granted[grant.relname] = GrantedPrivileges(
            privileges.on_table, privileges.on_columns | {(grant.attname, grant.privilege_type)}
        )
    assert granted == APP_PRIVILEGES
    assert executable == sorted(APP_EXECUTABLE_FUNCTIONS)
    assert "create_run_partitions" not in executable
    # Default privileges would reach every new partition, which has no policy of its own [B6].
    assert default_privileges == []


async def test_public_has_no_views_or_security_definer_functions(
    owner_engine: AsyncEngine,
) -> None:
    async with owner_engine.connect() as connection:
        views = (await connection.execute(VIEWS)).scalars().all()
        security_definers = (await connection.execute(SECURITY_DEFINER_FUNCTIONS)).scalars().all()
    # A view or SECURITY DEFINER function runs with its owner's rights, which is how a query can
    # escape the caller's row-level security [A7].
    assert views == []
    assert security_definers == []


async def test_owner_role_owns_every_object(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as connection:
        relation_owners = (await connection.execute(RELATION_OWNERS)).all()
        function_owners = (await connection.execute(FUNCTION_OWNERS)).all()

    assert {relation.relname for relation in relation_owners} >= {"workspaces", "runs"}
    assert {function.proname for function in function_owners} >= {"create_run_partitions"}
    assert [
        tuple(owned)
        for owned in (*relation_owners, *function_owners)
        if owned[1] != DATABASE_OWNER_ROLE
    ] == []
