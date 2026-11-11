"""Runs and run steps, partitioned by month. Part of P01_S03.

``runs`` is partitioned on ``started_at`` and ``run_steps`` on ``run_started_at``, a copy of its
run's ``started_at``. A partition key must be part of every unique key, so both primary keys carry
it, and a step references its run by ``(workspace_id, run_id, run_started_at)``. There is no
DEFAULT partition: a run in a month with no partition fails loudly instead of collecting in a table
that later has to be split.

Row-level security and grants live on the parents and are not inherited, yet each partition can be
queried directly. ``create_run_partitions()`` therefore enables row-level security on every
partition it creates, with no policy, and grants nothing on it, so direct access is denied. The
application role reaches the rows only through the parents.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

from app.database_roles import DATABASE_APP_ROLE

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

PARTITIONED_TENANT_TABLES = ("runs", "run_steps")

APP_PRIVILEGES = {
    "runs": "SELECT, INSERT, UPDATE",
    "run_steps": "SELECT, INSERT, UPDATE",
}


def upgrade() -> None:
    # The statuses are the run lifecycle of docs/runner-contract.md. workflow_version_id is NULL for
    # a test run of the draft.
    op.execute(
        """
        CREATE TABLE runs (
            id uuid NOT NULL DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces (id),
            workflow_id uuid NOT NULL,
            workflow_version_id uuid,
            status text NOT NULL CONSTRAINT runs_status_check CHECK (
                status IN ('running', 'parked', 'completed', 'failed', 'cancelled', 'timed_out')
            ),
            started_at timestamptz NOT NULL DEFAULT now(),
            ended_at timestamptz,
            error jsonb,
            PRIMARY KEY (workspace_id, id, started_at),
            FOREIGN KEY (workspace_id, workflow_id)
                REFERENCES workflows (workspace_id, id) ON DELETE CASCADE,
            FOREIGN KEY (workspace_id, workflow_id, workflow_version_id)
                REFERENCES workflow_versions (workspace_id, workflow_id, id),
            CONSTRAINT runs_ended_at_check
                CHECK ((ended_at IS NULL) = (status IN ('running', 'parked')))
        ) PARTITION BY RANGE (started_at)
        """
    )
    op.execute(
        "CREATE INDEX runs_workspace_id_workflow_id_started_at_idx"
        " ON runs (workspace_id, workflow_id, started_at DESC)"
    )
    # The statuses are the step statuses of docs/runner-contract.md. A step keeps one row per node,
    # updated in place on each attempt.
    op.execute(
        """
        CREATE TABLE run_steps (
            id uuid NOT NULL DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces (id),
            run_id uuid NOT NULL,
            run_started_at timestamptz NOT NULL,
            node_id text NOT NULL,
            node_type text NOT NULL,
            status text NOT NULL CONSTRAINT run_steps_status_check CHECK (
                status IN (
                    'running', 'retrying', 'parked', 'completed', 'failed', 'skipped', 'cancelled'
                )
            ),
            attempt integer NOT NULL DEFAULT 1
                CONSTRAINT run_steps_attempt_check CHECK (attempt >= 1),
            input jsonb,
            output jsonb,
            tokens integer CONSTRAINT run_steps_tokens_check CHECK (tokens >= 0),
            cost numeric CONSTRAINT run_steps_cost_check CHECK (cost >= 0),
            latency_ms integer CONSTRAINT run_steps_latency_ms_check CHECK (latency_ms >= 0),
            error jsonb,
            PRIMARY KEY (workspace_id, id, run_started_at),
            UNIQUE (workspace_id, run_id, run_started_at, node_id),
            FOREIGN KEY (workspace_id, run_id, run_started_at)
                REFERENCES runs (workspace_id, id, started_at) ON DELETE CASCADE
        ) PARTITION BY RANGE (run_started_at)
        """
    )
    # Covers the current UTC month and the 12 after it, and skips partitions that exist, so
    # app.run_partitions can call it on every deploy to move the horizon forward. The pinned
    # TimeZone makes the month bounds UTC whatever the caller's session uses; the pinned
    # search_path keeps the unqualified names resolving to these tables.
    op.execute(
        """
        CREATE FUNCTION create_run_partitions() RETURNS void
        LANGUAGE plpgsql
        SET search_path = public, pg_temp
        SET TimeZone = 'UTC'
        AS $$
        DECLARE
            current_month timestamptz := date_trunc('month', now(), 'UTC');
            month_start timestamptz;
            month_suffix text;
            parent text;
            partition_name text;
        BEGIN
            FOR month_offset IN 0..12 LOOP
                month_start := current_month + make_interval(months => month_offset);
                month_suffix := to_char(month_start AT TIME ZONE 'UTC', 'YYYY_MM');
                FOREACH parent IN ARRAY ARRAY['runs', 'run_steps'] LOOP
                    partition_name := parent || '_' || month_suffix;
                    CONTINUE WHEN to_regclass(partition_name) IS NOT NULL;
                    EXECUTE format(
                        'CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                        partition_name, parent, month_start, month_start + interval '1 month'
                    );
                    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', partition_name);
                    EXECUTE format('REVOKE ALL ON %I FROM PUBLIC', partition_name);
                END LOOP;
            END LOOP;
        END
        $$
        """
    )
    op.execute("REVOKE EXECUTE ON FUNCTION create_run_partitions() FROM PUBLIC")
    op.execute("SELECT create_run_partitions()")
    for table in PARTITIONED_TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON {table} FOR ALL"
            " USING (workspace_id = app_current_workspace_id())"
            " WITH CHECK (workspace_id = app_current_workspace_id())"
        )
    for table, privileges in APP_PRIVILEGES.items():
        op.execute(f"GRANT {privileges} ON {table} TO {DATABASE_APP_ROLE}")


def downgrade() -> None:
    # Steps first: their foreign key depends on every runs partition. Dropping a parent drops its
    # partitions, policies and grants with it.
    op.execute("DROP TABLE run_steps")
    op.execute("DROP TABLE runs")
    op.execute("DROP FUNCTION create_run_partitions()")
