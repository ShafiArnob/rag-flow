"""Tenant isolation as the application role: what one workspace can read, write and reference."""

import asyncio
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.tenancy import workspace_transaction
from schema_expectations import RLS_EXEMPT_TABLES, workspace_key_column
from tenants import SESSION_TOKEN_HASH_BYTES, Tenant, TwoTenants

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("schema_at_head")]

TABLES = text(
    "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace"
    " AND relkind IN ('r', 'p') AND NOT relispartition ORDER BY relname"
)

CONCURRENT_ROUNDS = 20
TRANSACTIONS_PER_TENANT_PER_ROUND = 5
# Long enough that each transaction is still open while the others set their workspace.
OVERLAP_SECONDS = 0.005


async def tenant_tables(engine: AsyncEngine) -> list[str]:
    """Every table that row-level security must protect, read from the catalog so a new table is
    covered without editing the tests. The catalog tests keep the exemptions honest."""
    async with engine.connect() as connection:
        tables = (await connection.execute(TABLES)).scalars().all()
    return [table for table in tables if table not in RLS_EXEMPT_TABLES]


async def test_app_role_without_workspace_setting_sees_no_rows_in_any_tenant_table(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    tables = await tenant_tables(app_engine)
    assert "sessions" in tables
    tenant_workspaces = {
        "first": two_tenants.first.workspace_id,
        "second": two_tenants.second.workspace_id,
    }

    # The negative control: both workspaces really have a row in every tenant table.
    async with superuser_engine.connect() as connection:
        fixture_rows = {
            table: (
                await connection.execute(
                    text(
                        f"SELECT count(*) FROM {table}"  # noqa: S608
                        f" WHERE {workspace_key_column(table)} IN (:first, :second)"
                    ),
                    tenant_workspaces,
                )
            ).scalar_one()
            for table in tables
        }
    assert fixture_rows == dict.fromkeys(tables, 2)

    async with app_engine.connect() as connection:
        visible_rows = {
            table: (
                await connection.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            ).scalar_one()
            for table in tables
        }
    assert visible_rows == dict.fromkeys(tables, 0)


async def test_tenant_sees_only_own_rows(app_engine: AsyncEngine, two_tenants: TwoTenants) -> None:
    tables = await tenant_tables(app_engine)
    for tenant in (two_tenants.first, two_tenants.second):
        async with workspace_transaction(app_engine, tenant.workspace_id) as connection:
            visible_workspaces = {
                table: (
                    await connection.execute(
                        text(f"SELECT DISTINCT {workspace_key_column(table)} FROM {table}")  # noqa: S608
                    )
                )
                .scalars()
                .all()
                for table in tables
            }
        assert visible_workspaces == {table: [tenant.workspace_id] for table in tables}


@dataclass(frozen=True)
class TenantWrites:
    """Statements a workspace might use to reach another workspace's rows in one table."""

    row_id: Callable[[Tenant], uuid.UUID]
    # A new row in :workspace_id, which may reference :workflow_id.
    insert: str
    # Moves row :id to :workspace_id.
    move: str
    # Changes column `changed` of row :id.
    update: str
    changed: str
    # None where the application role may not delete.
    delete: str | None


TENANT_WRITES = {
    "workflows": TenantWrites(
        row_id=lambda tenant: tenant.workflow_id,
        insert=(
            "INSERT INTO workflows (workspace_id, type, name, draft_graph, schema_version)"
            " VALUES (:workspace_id, 'chat', 'intruder', '{}', 1)"
        ),
        move="UPDATE workflows SET workspace_id = :workspace_id WHERE id = :id",
        update="UPDATE workflows SET name = 'intruder' WHERE id = :id",
        changed="name",
        delete="DELETE FROM workflows WHERE id = :id",
    ),
    "connectors": TenantWrites(
        row_id=lambda tenant: tenant.connector_id,
        insert=(
            "INSERT INTO connectors (workspace_id, type, name, config)"
            " VALUES (:workspace_id, 'http', 'intruder', '{}')"
        ),
        move="UPDATE connectors SET workspace_id = :workspace_id WHERE id = :id",
        update="UPDATE connectors SET name = 'intruder' WHERE id = :id",
        changed="name",
        delete="DELETE FROM connectors WHERE id = :id",
    ),
    "runs": TenantWrites(
        row_id=lambda tenant: tenant.run_id,
        insert=(
            "INSERT INTO runs (workspace_id, workflow_id, status)"
            " VALUES (:workspace_id, :workflow_id, 'running')"
        ),
        move="UPDATE runs SET workspace_id = :workspace_id WHERE id = :id",
        update="UPDATE runs SET error = '{}' WHERE id = :id",
        changed="error",
        delete=None,
    ),
}


async def column_value(engine: AsyncEngine, tenant: Tenant, table: str, column: str) -> object:
    """The column of the tenant's row in table, read as the tenant; fails if the row is gone."""
    writes = TENANT_WRITES[table]
    async with workspace_transaction(engine, tenant.workspace_id) as connection:
        row = await connection.execute(
            text(f"SELECT {column} FROM {table} WHERE id = :id"),  # noqa: S608
            {"id": writes.row_id(tenant)},
        )
        return row.scalar_one()


@pytest.mark.parametrize("table", TENANT_WRITES)
async def test_tenant_cannot_write_or_move_rows_into_another_workspace(
    app_engine: AsyncEngine, two_tenants: TwoTenants, table: str
) -> None:
    writes = TENANT_WRITES[table]
    acting, other = two_tenants.first, two_tenants.second
    policy_violation = f'new row violates row-level security policy for table "{table}"'
    into_other = {"workspace_id": other.workspace_id, "workflow_id": other.workflow_id}

    with pytest.raises(DBAPIError, match=policy_violation):
        async with workspace_transaction(app_engine, acting.workspace_id) as connection:
            await connection.execute(text(writes.insert), into_other)
    with pytest.raises(DBAPIError, match=policy_violation):
        async with app_engine.begin() as connection:
            await connection.execute(text(writes.insert), into_other)
    with pytest.raises(DBAPIError, match=policy_violation):
        async with workspace_transaction(app_engine, acting.workspace_id) as connection:
            await connection.execute(
                text(writes.move),
                {"workspace_id": other.workspace_id, "id": writes.row_id(acting)},
            )

    other_value = await column_value(app_engine, other, table, writes.changed)
    other_row = {"id": writes.row_id(other)}
    async with workspace_transaction(app_engine, acting.workspace_id) as connection:
        assert (await connection.execute(text(writes.update), other_row)).rowcount == 0
        if writes.delete is not None:
            assert (await connection.execute(text(writes.delete), other_row)).rowcount == 0
    assert await column_value(app_engine, other, table, writes.changed) == other_value
    assert await column_value(app_engine, acting, table, "workspace_id") == acting.workspace_id


# Each statement, run in the first workspace, references a row of the second through the named
# composite foreign key. The fixture's own rows make the same references within one workspace.
CROSS_TENANT_REFERENCES = {
    "run_steps_workspace_id_run_id_run_started_at_fkey": (
        "INSERT INTO run_steps (workspace_id, run_id, run_started_at, node_id, node_type, status)"
        " VALUES (:workspace_id, :other_run_id, :other_run_started_at, 'node-1', 'llm', 'running')"
    ),
    "runs_workspace_id_workflow_id_fkey": (
        "INSERT INTO runs (workspace_id, workflow_id, status)"
        " VALUES (:workspace_id, :other_workflow_id, 'running')"
    ),
    "runs_workspace_id_workflow_id_workflow_version_id_fkey": (
        "INSERT INTO runs (workspace_id, workflow_id, workflow_version_id, status)"
        " VALUES (:workspace_id, :workflow_id, :other_workflow_version_id, 'running')"
    ),
    "workflow_versions_workspace_id_workflow_id_fkey": (
        "INSERT INTO workflow_versions (workspace_id, workflow_id, version_no, graph, created_by)"
        " VALUES (:workspace_id, :other_workflow_id, 2, '{}', :user_id)"
    ),
    "connectors_workspace_id_secret_ref_fkey": (
        "INSERT INTO connectors (workspace_id, type, name, config, secret_ref)"
        " VALUES (:workspace_id, 'http', 'intruder', '{}', :other_secret_id)"
    ),
    "sessions_workspace_id_user_id_fkey": (
        "INSERT INTO sessions (workspace_id, user_id, token_hash, csrf_token, expires_at)"
        " VALUES (:workspace_id, :other_user_id, :token_hash, 'csrf', now() + interval '1 day')"
    ),
}


@pytest.mark.parametrize("foreign_key", CROSS_TENANT_REFERENCES)
async def test_cross_tenant_references_are_rejected(
    app_engine: AsyncEngine, two_tenants: TwoTenants, foreign_key: str
) -> None:
    acting, other = two_tenants.first, two_tenants.second
    references = {
        "workspace_id": acting.workspace_id,
        "workflow_id": acting.workflow_id,
        "user_id": acting.user_id,
        "token_hash": secrets.token_bytes(SESSION_TOKEN_HASH_BYTES),
        "other_run_id": other.run_id,
        "other_run_started_at": other.run_started_at,
        "other_workflow_id": other.workflow_id,
        "other_workflow_version_id": other.workflow_version_id,
        "other_secret_id": other.secret_id,
        "other_user_id": other.user_id,
    }
    with pytest.raises(IntegrityError, match=f'violates foreign key constraint "{foreign_key}"'):
        async with workspace_transaction(app_engine, acting.workspace_id) as connection:
            await connection.execute(text(CROSS_TENANT_REFERENCES[foreign_key]), references)


AUDIT_LOG_WRITES_DENIED = {
    "update": "UPDATE audit_log SET action = 'logout' WHERE id = :id",
    "delete": "DELETE FROM audit_log WHERE id = :id",
    "truncate": "TRUNCATE audit_log",
    "insert-id": (
        "INSERT INTO audit_log (id, workspace_id, action)"
        " VALUES (gen_random_uuid(), :workspace_id, 'login')"
    ),
    "insert-at": (
        "INSERT INTO audit_log (workspace_id, action, at) VALUES (:workspace_id, 'login', now())"
    ),
}


@pytest.mark.parametrize("statement", AUDIT_LOG_WRITES_DENIED.values(), ids=AUDIT_LOG_WRITES_DENIED)
async def test_audit_log_is_append_only_and_app_cannot_set_id_or_timestamp(
    app_engine: AsyncEngine, two_tenants: TwoTenants, statement: str
) -> None:
    tenant = two_tenants.first
    with pytest.raises(DBAPIError, match="permission denied for table audit_log"):
        async with workspace_transaction(app_engine, tenant.workspace_id) as connection:
            await connection.execute(
                text(statement), {"id": tenant.audit_entry_id, "workspace_id": tenant.workspace_id}
            )

    # The fixture's entry, inserted through the permitted columns, is still there unchanged.
    async with workspace_transaction(app_engine, tenant.workspace_id) as connection:
        entries = await connection.execute(text("SELECT id, action FROM audit_log"))
        assert [tuple(entry) for entry in entries.all()] == [
            (tenant.audit_entry_id, "connector.create")
        ]


WORKFLOW_VERSION_CHANGES = {
    "update": "UPDATE workflow_versions SET graph = '[]' WHERE id = :id",
    "delete": "DELETE FROM workflow_versions WHERE id = :id",
}


@pytest.mark.parametrize(
    "statement", WORKFLOW_VERSION_CHANGES.values(), ids=WORKFLOW_VERSION_CHANGES
)
async def test_workflow_versions_are_immutable(
    app_engine: AsyncEngine, two_tenants: TwoTenants, statement: str
) -> None:
    tenant = two_tenants.first
    with pytest.raises(DBAPIError, match="permission denied for table workflow_versions"):
        async with workspace_transaction(app_engine, tenant.workspace_id) as connection:
            await connection.execute(text(statement), {"id": tenant.workflow_version_id})

    async with workspace_transaction(app_engine, tenant.workspace_id) as connection:
        versions = await connection.execute(text("SELECT id, graph FROM workflow_versions"))
        assert [tuple(version) for version in versions.all()] == [(tenant.workflow_version_id, {})]


DEPENDENT_ROWS = {
    "workflow_versions": "SELECT count(*) FROM workflow_versions WHERE workflow_id = :workflow_id",
    "runs": "SELECT count(*) FROM runs WHERE workflow_id = :workflow_id",
    "run_steps": "SELECT count(*) FROM run_steps WHERE run_id = :run_id",
}


async def dependent_row_counts(engine: AsyncEngine, tenant: Tenant) -> dict[str, int]:
    async with workspace_transaction(engine, tenant.workspace_id) as connection:
        return {
            table: (
                await connection.execute(
                    text(query), {"workflow_id": tenant.workflow_id, "run_id": tenant.run_id}
                )
            ).scalar_one()
            for table, query in DEPENDENT_ROWS.items()
        }


async def test_deleting_a_workflow_cascades_versions_runs_and_steps(
    app_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    deleted, kept = two_tenants.first, two_tenants.second
    assert await dependent_row_counts(app_engine, deleted) == dict.fromkeys(DEPENDENT_ROWS, 1)

    async with workspace_transaction(app_engine, deleted.workspace_id) as connection:
        deletion = await connection.execute(
            text("DELETE FROM workflows WHERE id = :id"), {"id": deleted.workflow_id}
        )
        assert deletion.rowcount == 1

    assert await dependent_row_counts(app_engine, deleted) == dict.fromkeys(DEPENDENT_ROWS, 0)
    assert await dependent_row_counts(app_engine, kept) == dict.fromkeys(DEPENDENT_ROWS, 1)


@dataclass(frozen=True)
class Observation:
    workspace_id: uuid.UUID
    backend_pid: int
    setting: str
    visible_workspaces: list[uuid.UUID]


async def observe_workspace(engine: AsyncEngine, workspace_id: uuid.UUID) -> Observation:
    async with workspace_transaction(engine, workspace_id) as connection:
        await connection.execute(text("SELECT pg_sleep(:seconds)"), {"seconds": OVERLAP_SECONDS})
        state = (
            await connection.execute(
                text(
                    "SELECT pg_backend_pid() AS backend_pid,"
                    " current_setting('app.workspace_id') AS setting"
                )
            )
        ).one()
        visible = await connection.execute(text("SELECT DISTINCT workspace_id FROM workflows"))
        return Observation(
            workspace_id, state.backend_pid, state.setting, list(visible.scalars().all())
        )


async def test_concurrent_workspace_transactions_stay_isolated(
    app_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    workspace_ids = [two_tenants.first.workspace_id, two_tenants.second.workspace_id]
    observations: list[Observation] = []
    for _ in range(CONCURRENT_ROUNDS):
        observations += await asyncio.gather(
            *(
                observe_workspace(app_engine, workspace_id)
                for workspace_id in workspace_ids * TRANSACTIONS_PER_TENANT_PER_ROUND
            )
        )

    leaks = [
        observation
        for observation in observations
        if observation.setting != str(observation.workspace_id)
        or observation.visible_workspaces != [observation.workspace_id]
    ]
    assert leaks == []
    # The negative control: pooled connections really served both workspaces in turn.
    workspaces_by_connection: dict[int, set[uuid.UUID]] = {}
    for observation in observations:
        workspaces_by_connection.setdefault(observation.backend_pid, set()).add(
            observation.workspace_id
        )
    assert any(len(workspaces) == 2 for workspaces in workspaces_by_connection.values())
