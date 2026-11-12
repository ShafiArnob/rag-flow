"""The audit writer, against the real database as the application role."""

import ipaddress
import re
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.audit import AuditAction, record_audit_event
from app.database.tenancy import workspace_transaction
from support.tenants import TwoTenants

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("schema_at_head")]

ACTION_CHECK_DEFINITION = text(
    "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
    " WHERE conrelid = 'audit_log'::regclass AND conname = 'audit_log_action_check'"
)
# pg_get_constraintdef prints the IN list as ANY (ARRAY['login'::text, ...]).
QUOTED_TEXT_LITERAL = re.compile(r"'([^']*)'::text")
AUDIT_ENTRY = text(
    "SELECT workspace_id, actor_id, action, target, ip FROM audit_log WHERE target = :target"
)


async def test_audit_actions_are_exactly_the_values_the_schema_allows(
    owner_engine: AsyncEngine,
) -> None:
    async with owner_engine.connect() as connection:
        definition: str = (await connection.execute(ACTION_CHECK_DEFINITION)).scalar_one()

    allowed = QUOTED_TEXT_LITERAL.findall(definition)
    assert allowed, f"no values found in {definition!r}"
    assert sorted(allowed) == sorted(action.value for action in AuditAction)


async def test_record_audit_event_writes_every_field_it_is_given(
    app_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    tenant = two_tenants.first
    target = uuid.uuid4()
    ip = ipaddress.ip_address("2001:db8::7")

    async with workspace_transaction(app_engine, tenant.workspace_id) as connection:
        await record_audit_event(
            connection,
            workspace_id=tenant.workspace_id,
            actor_id=tenant.user_id,
            action=AuditAction.CONNECTOR_DELETE,
            target=target,
            ip=ip,
        )

    async with workspace_transaction(app_engine, tenant.workspace_id) as connection:
        entries = (await connection.execute(AUDIT_ENTRY, {"target": target})).all()
    assert [tuple(entry) for entry in entries] == [
        (tenant.workspace_id, tenant.user_id, "connector.delete", target, ip)
    ]
