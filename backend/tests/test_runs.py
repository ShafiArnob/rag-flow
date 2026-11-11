"""Runs and run steps: their check constraints and their monthly partitions."""

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app import run_partitions
from app.database import create_database_engine
from app.database_roles import DATABASE_APP_ROLE
from app.tenancy import workspace_transaction

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("schema_at_head")]

OPEN_RUN_STATUSES = ("running", "parked")
ENDED_RUN_STATUSES = ("completed", "failed", "cancelled", "timed_out")

INSERT_WORKSPACE = text("INSERT INTO workspaces (id, name) VALUES (:id, 'run tests')")
INSERT_WORKFLOW = text(
    "INSERT INTO workflows (id, workspace_id, type, name, draft_graph, schema_version)"
    " VALUES (:id, :workspace_id, 'chat', 'run tests', '{}', 1)"
)
INSERT_RUN = text(
    "INSERT INTO runs (workspace_id, workflow_id, status, started_at, ended_at)"
    " VALUES (:workspace_id, :workflow_id, :status, coalesce(:started_at, now()), :ended_at)"
    " RETURNING id, started_at"
)
INSERT_STEP = text(
    "INSERT INTO run_steps (workspace_id, run_id, run_started_at, node_id, node_type, status)"
    " VALUES (:workspace_id, :run_id, :run_started_at, 'node-1', 'llm', 'running')"
)

NEWEST_STEPS_PARTITION = text(
    "SELECT max(c.relname) FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
    " WHERE i.inhparent = 'run_steps'::regclass"
)
# Every privilege a table can carry. has_table_privilege is true if the role holds any of them.
PARTITION_STATE = text(
    "SELECT c.relname, i.inhparent::regclass::text AS parent, c.relrowsecurity,"
    " has_table_privilege(CAST(:app_role AS name), c.oid,"
    " 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER') AS app_has_privilege"
    " FROM pg_class c LEFT JOIN pg_inherits i ON i.inhrelid = c.oid"
    " WHERE c.relname IN (:runs_partition, :steps_partition)"
    " ORDER BY c.relname"
)


@dataclass(frozen=True)
class Workflow:
    workspace_id: uuid.UUID
    id: uuid.UUID


@pytest.fixture
async def workflow(app_engine: AsyncEngine) -> Workflow:
    """A workflow in a new workspace, created as the application role."""
    workflow = Workflow(workspace_id=uuid.uuid4(), id=uuid.uuid4())
    async with workspace_transaction(app_engine, workflow.workspace_id) as connection:
        await connection.execute(INSERT_WORKSPACE, {"id": workflow.workspace_id})
        await connection.execute(
            INSERT_WORKFLOW, {"id": workflow.id, "workspace_id": workflow.workspace_id}
        )
    return workflow


@pytest.fixture
async def maintenance_engine() -> AsyncIterator[AsyncEngine]:
    """The owner with row_security off, as Alembic and app.run_partitions connect."""
    engine = create_database_engine(
        SecretStr(os.environ["MIGRATION_DATABASE_URL"]), row_security=False
    )
    yield engine
    await engine.dispose()


async def insert_run(
    engine: AsyncEngine,
    workflow: Workflow,
    *,
    status: str,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> tuple[uuid.UUID, datetime]:
    async with workspace_transaction(engine, workflow.workspace_id) as connection:
        run = await connection.execute(
            INSERT_RUN,
            {
                "workspace_id": workflow.workspace_id,
                "workflow_id": workflow.id,
                "status": status,
                "started_at": started_at,
                "ended_at": ended_at,
            },
        )
        run_id, run_started_at = run.one()
    return run_id, run_started_at


async def insert_step(
    engine: AsyncEngine, workflow: Workflow, run_id: uuid.UUID, run_started_at: datetime
) -> None:
    async with workspace_transaction(engine, workflow.workspace_id) as connection:
        await connection.execute(
            INSERT_STEP,
            {
                "workspace_id": workflow.workspace_id,
                "run_id": run_id,
                "run_started_at": run_started_at,
            },
        )


async def partition_of(
    engine: AsyncEngine, workflow: Workflow, table: str, key_column: str, run_id: uuid.UUID
) -> str | None:
    async with workspace_transaction(engine, workflow.workspace_id) as connection:
        placement = await connection.execute(
            text(f"SELECT tableoid::regclass::text FROM {table} WHERE {key_column} = :run_id"),  # noqa: S608
            {"run_id": run_id},
        )
        return placement.scalar_one_or_none()


@pytest.mark.parametrize("status", OPEN_RUN_STATUSES + ENDED_RUN_STATUSES)
async def test_run_status_and_ended_at_must_agree(
    app_engine: AsyncEngine, workflow: Workflow, status: str
) -> None:
    now = datetime.now(UTC)
    is_open = status in OPEN_RUN_STATUSES
    await insert_run(app_engine, workflow, status=status, ended_at=None if is_open else now)

    with pytest.raises(IntegrityError, match="runs_ended_at_check"):
        await insert_run(app_engine, workflow, status=status, ended_at=now if is_open else None)


@pytest.mark.parametrize(
    "started_at",
    [
        pytest.param(datetime(2000, 1, 1, tzinfo=UTC), id="before-current-month"),
        # Partitions reach the current month plus 12, so two years ahead has none.
        pytest.param(
            datetime(datetime.now(UTC).year + 2, datetime.now(UTC).month, 1, tzinfo=UTC),
            id="after-horizon",
        ),
    ],
)
async def test_run_outside_created_partitions_is_rejected(
    app_engine: AsyncEngine, workflow: Workflow, started_at: datetime
) -> None:
    with pytest.raises(IntegrityError, match='no partition of relation "runs" found for row'):
        await insert_run(app_engine, workflow, status="running", started_at=started_at)


async def test_partition_is_recreated_under_force_with_rls_and_working_fks(
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    maintenance_engine: AsyncEngine,
    workflow: Workflow,
) -> None:
    async with maintenance_engine.begin() as connection:
        steps_partition = (await connection.execute(NEWEST_STEPS_PARTITION)).scalar_one()
        month_suffix = steps_partition.removeprefix("run_steps_")
        runs_partition = f"runs_{month_suffix}"
        # A runs partition cannot be dropped while run_steps' foreign key depends on it, so it is
        # detached first, after the steps partition that references it is gone. Detaching checks
        # that no step references the partition, a query that row-level security on run_steps
        # would filter, so with row_security off it fails unless FORCE is lifted around it.
        await connection.execute(text(f"DROP TABLE {steps_partition}"))
        await connection.execute(text("ALTER TABLE run_steps NO FORCE ROW LEVEL SECURITY"))
        await connection.execute(text(f"ALTER TABLE runs DETACH PARTITION {runs_partition}"))
        await connection.execute(text("ALTER TABLE run_steps FORCE ROW LEVEL SECURITY"))
        await connection.execute(text(f"DROP TABLE {runs_partition}"))

    partitions = {"runs_partition": runs_partition, "steps_partition": steps_partition}
    async with owner_engine.connect() as connection:
        state_after_drop = await connection.execute(
            PARTITION_STATE, {"app_role": DATABASE_APP_ROLE, **partitions}
        )
        assert state_after_drop.all() == []

    await run_partitions.main()

    async with owner_engine.connect() as connection:
        state = await connection.execute(
            PARTITION_STATE, {"app_role": DATABASE_APP_ROLE, **partitions}
        )
        assert [tuple(row) for row in state.all()] == [
            (steps_partition, "run_steps", True, False),
            (runs_partition, "runs", True, False),
        ]

    year, month = (int(part) for part in month_suffix.split("_"))
    run_id, run_started_at = await insert_run(
        app_engine, workflow, status="running", started_at=datetime(year, month, 15, tzinfo=UTC)
    )
    await insert_step(app_engine, workflow, run_id, run_started_at)
    assert await partition_of(app_engine, workflow, "runs", "id", run_id) == runs_partition
    assert await partition_of(app_engine, workflow, "run_steps", "run_id", run_id) == (
        steps_partition
    )

    # The foreign key cloned onto the new steps partition rejects a run that does not exist.
    with pytest.raises(IntegrityError, match="run_steps_workspace_id_run_id_run_started_at_fkey"):
        await insert_step(app_engine, workflow, uuid.uuid4(), run_started_at)

    # Deleting the workflow cascades into the new runs partition, and from there into its steps.
    async with workspace_transaction(app_engine, workflow.workspace_id) as connection:
        await connection.execute(text("DELETE FROM workflows WHERE id = :id"), {"id": workflow.id})
    assert await partition_of(app_engine, workflow, "runs", "id", run_id) is None
    assert await partition_of(app_engine, workflow, "run_steps", "run_id", run_id) is None
