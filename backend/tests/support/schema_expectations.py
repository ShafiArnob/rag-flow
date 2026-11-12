"""What the schema must grant and protect, declared independently of the migrations so the catalog
tests catch a migration that drifts from it. Change a declaration here only together with the
migration that changes the fact."""

from dataclasses import dataclass


@dataclass(frozen=True)
class GrantedPrivileges:
    on_table: frozenset[str]
    # (column, privilege) pairs granted on single columns rather than the whole table.
    on_columns: frozenset[tuple[str, str]] = frozenset()


AUDIT_LOG_INSERTABLE_COLUMNS = ("workspace_id", "actor_id", "action", "target", "ip")

# Every privilege the application role holds on a relation in schema public. Nothing else may be
# granted to any role but the owner. Never TRUNCATE, REFERENCES or TRIGGER, and nothing on a
# partition: partitions are reached only through their parents.
APP_PRIVILEGES = {
    "workspaces": GrantedPrivileges(frozenset({"SELECT", "INSERT"})),
    "users": GrantedPrivileges(frozenset({"SELECT", "INSERT", "UPDATE"})),
    "workspace_members": GrantedPrivileges(frozenset({"SELECT", "INSERT"})),
    "sessions": GrantedPrivileges(frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})),
    "workflows": GrantedPrivileges(frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})),
    # A published version never changes.
    "workflow_versions": GrantedPrivileges(frozenset({"SELECT", "INSERT"})),
    "secrets": GrantedPrivileges(frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})),
    "connectors": GrantedPrivileges(frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})),
    # Append-only, and id and at always come from their defaults.
    "audit_log": GrantedPrivileges(
        frozenset({"SELECT"}),
        frozenset((column, "INSERT") for column in AUDIT_LOG_INSERTABLE_COLUMNS),
    ),
    "runs": GrantedPrivileges(frozenset({"SELECT", "INSERT", "UPDATE"})),
    "run_steps": GrantedPrivileges(frozenset({"SELECT", "INSERT", "UPDATE"})),
}


@dataclass(frozen=True)
class Policy:
    """A permissive policy for every role, with its expressions as pg_policies prints them."""

    name: str
    command: str
    using: str | None
    with_check: str | None


# Functions in schema public, outside extensions, that the application role may execute.
APP_EXECUTABLE_FUNCTIONS = {
    "app_current_workspace_id": "every tenant policy calls it as the querying role",
    "app_current_user_id": "the user-scoped policies call it as the querying role",
    "app_login_email": "the users_select policy calls it as the querying role",
}

# Tables without row-level security, each with the reason it is safe.
RLS_EXEMPT_TABLES = {
    "alembic_version": (
        "holds only the schema revision, and the application role has no grant on it"
    ),
}

# Tables with row-level security but no <table>_tenant policy, each with the reason. Their policies
# are all declared in EXTRA_POLICIES.
USER_SCOPED_TABLES = {
    "users": (
        "has no workspace key; a user can belong to several workspaces. A row is visible only to"
        " its own user, or to a login that names its exact email"
    ),
}

_ACTS_FOR_ROW_USER = "(id = app_current_user_id())"

# Every policy beyond the single <table>_tenant policy, by table. Each needs its own isolation
# story, told by tests/test_user_row_level_security.py.
EXTRA_POLICIES = {
    "users": frozenset(
        {
            Policy(
                "users_select",
                "SELECT",
                using="((id = app_current_user_id()) OR (email = app_login_email()))",
                with_check=None,
            ),
            # Signup creates a user before anyone is logged in.
            Policy("users_insert", "INSERT", using=None, with_check="true"),
            Policy(
                "users_update_self",
                "UPDATE",
                using=_ACTS_FOR_ROW_USER,
                with_check=_ACTS_FOR_ROW_USER,
            ),
        }
    ),
    # Login finds the user's workspace before it can act in one.
    "workspace_members": frozenset(
        {
            Policy(
                "workspace_members_self",
                "SELECT",
                using="(user_id = app_current_user_id())",
                with_check=None,
            ),
        }
    ),
}

WORKSPACE_KEY_COLUMN = "workspace_id"
# A workspace is its own tenant, so its policy compares its id.
WORKSPACE_KEY_COLUMN_EXCEPTIONS = {"workspaces": "id"}


def workspace_key_column(table: str) -> str:
    """The column that table's tenant policy compares with the workspace setting."""
    return WORKSPACE_KEY_COLUMN_EXCEPTIONS.get(table, WORKSPACE_KEY_COLUMN)


def tenant_policy_expression(table: str) -> str:
    """The USING and WITH CHECK expression of the tenant policy, as pg_policies prints it."""
    return f"({workspace_key_column(table)} = app_current_workspace_id())"
