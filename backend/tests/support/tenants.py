"""A workspace with one row in every tenant table, written as the application role, for tests that
check one workspace cannot reach another's rows."""

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.tenancy import workspace_transaction

SESSION_TOKEN_HASH_BYTES = 32
SECRET_NONCE_BYTES = 12

INSERT_WORKSPACE = text("INSERT INTO workspaces (id, name) VALUES (:workspace_id, 'tenant test')")
INSERT_USER = text("INSERT INTO users (id, email) VALUES (:user_id, :email)")
INSERT_MEMBER = text(
    "INSERT INTO workspace_members (workspace_id, user_id, role)"
    " VALUES (:workspace_id, :user_id, 'owner')"
)
INSERT_SESSION = text(
    "INSERT INTO sessions (id, workspace_id, user_id, token_hash, csrf_token, expires_at)"
    " VALUES (:session_id, :workspace_id, :user_id, :token_hash, :csrf_token,"
    " now() + interval '1 day')"
)
INSERT_WORKFLOW = text(
    "INSERT INTO workflows (id, workspace_id, type, name, draft_graph, schema_version)"
    " VALUES (:workflow_id, :workspace_id, 'chat', 'tenant test', '{}', 1)"
)
INSERT_WORKFLOW_VERSION = text(
    "INSERT INTO workflow_versions (id, workspace_id, workflow_id, version_no, graph, created_by)"
    " VALUES (:workflow_version_id, :workspace_id, :workflow_id, 1, '{}', :user_id)"
)
INSERT_SECRET = text(
    "INSERT INTO secrets (id, workspace_id, ciphertext, wrapped_dek, key_id, nonce, last4)"
    " VALUES (:secret_id, :workspace_id, :ciphertext, :wrapped_dek, 'test-key', :nonce, 'abcd')"
)
INSERT_CONNECTOR = text(
    "INSERT INTO connectors (id, workspace_id, type, name, config, secret_ref)"
    " VALUES (:connector_id, :workspace_id, 'http', 'tenant test', '{}', :secret_id)"
)
INSERT_AUDIT_ENTRY = text(
    "INSERT INTO audit_log (workspace_id, actor_id, action, target, ip)"
    " VALUES (:workspace_id, :user_id, 'connector.create', :connector_id, '192.0.2.1')"
    " RETURNING id"
)
INSERT_RUN = text(
    "INSERT INTO runs (id, workspace_id, workflow_id, workflow_version_id, status)"
    " VALUES (:run_id, :workspace_id, :workflow_id, :workflow_version_id, 'running')"
    " RETURNING started_at"
)
INSERT_RUN_STEP = text(
    "INSERT INTO run_steps (id, workspace_id, run_id, run_started_at, node_id, node_type, status)"
    " VALUES (:run_step_id, :workspace_id, :run_id, :run_started_at, 'node-1', 'llm', 'running')"
)


@dataclass(frozen=True)
class Tenant:
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    session_id: uuid.UUID
    workflow_id: uuid.UUID
    workflow_version_id: uuid.UUID
    secret_id: uuid.UUID
    connector_id: uuid.UUID
    audit_entry_id: uuid.UUID
    run_id: uuid.UUID
    run_started_at: datetime
    run_step_id: uuid.UUID


@dataclass(frozen=True)
class TwoTenants:
    first: Tenant
    second: Tenant


async def create_tenant(engine: AsyncEngine) -> Tenant:
    """Creates a workspace with random ids and one row in every tenant table, each referencing the
    workspace's own rows. The user is the workspace's only member."""
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    workflow_id = uuid.uuid4()
    workflow_version_id = uuid.uuid4()
    secret_id = uuid.uuid4()
    connector_id = uuid.uuid4()
    run_id = uuid.uuid4()
    run_step_id = uuid.uuid4()
    values = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "email": f"tenant-{workspace_id}@example.test",
        "session_id": session_id,
        "token_hash": secrets.token_bytes(SESSION_TOKEN_HASH_BYTES),
        "csrf_token": secrets.token_urlsafe(),
        "workflow_id": workflow_id,
        "workflow_version_id": workflow_version_id,
        "secret_id": secret_id,
        "ciphertext": secrets.token_bytes(),
        "wrapped_dek": secrets.token_bytes(),
        "nonce": secrets.token_bytes(SECRET_NONCE_BYTES),
        "connector_id": connector_id,
        "run_id": run_id,
        "run_step_id": run_step_id,
    }
    async with workspace_transaction(engine, workspace_id) as connection:
        for statement in (
            INSERT_WORKSPACE,
            INSERT_USER,
            INSERT_MEMBER,
            INSERT_SESSION,
            INSERT_WORKFLOW,
            INSERT_WORKFLOW_VERSION,
            INSERT_SECRET,
            INSERT_CONNECTOR,
        ):
            await connection.execute(statement, values)
        audit_entry_id = (await connection.execute(INSERT_AUDIT_ENTRY, values)).scalar_one()
        run_started_at = (await connection.execute(INSERT_RUN, values)).scalar_one()
        await connection.execute(INSERT_RUN_STEP, {**values, "run_started_at": run_started_at})
    return Tenant(
        workspace_id=workspace_id,
        user_id=user_id,
        session_id=session_id,
        workflow_id=workflow_id,
        workflow_version_id=workflow_version_id,
        secret_id=secret_id,
        connector_id=connector_id,
        audit_entry_id=audit_entry_id,
        run_id=run_id,
        run_started_at=run_started_at,
        run_step_id=run_step_id,
    )
