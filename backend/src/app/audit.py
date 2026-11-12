"""The append-only audit log of security-relevant actions in a workspace."""

import ipaddress
import uuid
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class AuditAction(StrEnum):
    """The values the audit_log_action_check constraint allows; tests/test_audit.py keeps them
    equal."""

    LOGIN = "login"
    LOGOUT = "logout"
    CONNECTOR_CREATE = "connector.create"
    CONNECTOR_DELETE = "connector.delete"
    SECRET_ROTATE = "secret.rotate"  # noqa: S105 - an action name, not a secret
    WORKFLOW_DELETE = "workflow.delete"


# The application role may insert only these columns, so id and at always come from their
# defaults and an entry cannot be backdated.
_INSERT_AUDIT_EVENT = text(
    "INSERT INTO audit_log (workspace_id, actor_id, action, target, ip)"
    " VALUES (:workspace_id, :actor_id, :action, :target, :ip)"
)


async def record_audit_event(
    connection: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: AuditAction,
    target: uuid.UUID | None,
    ip: IPAddress | None,
) -> None:
    """Records that ``actor_id`` did ``action`` in ``workspace_id``, from ``ip``, to ``target``.

    It runs in the caller's transaction, so the entry is written exactly when the action is.
    """
    await connection.execute(
        _INSERT_AUDIT_EVENT,
        {
            "workspace_id": workspace_id,
            "actor_id": actor_id,
            "action": action.value,
            "target": target,
            "ip": ip,
        },
    )
