"""Add a per-tenant sequence number to detection runs

With a frozen evaluation clock every run has the same evaluated_at, so the
latest run is identified by run_number instead.

Revision ID: 0002
Revises: 0001
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("detection_run") as batch:
        batch.add_column(sa.Column("run_number", sa.Integer(), server_default="0", nullable=False))
    # Number any existing runs in insertion order of their primary key.
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, tenant_id FROM detection_run ORDER BY tenant_id, id")).fetchall()
    counters: dict[str, int] = {}
    for run_id, tenant_id in rows:
        counters[tenant_id] = counters.get(tenant_id, 0) + 1
        conn.execute(sa.text("UPDATE detection_run SET run_number = :n WHERE id = :id"),
                     {"n": counters[tenant_id], "id": run_id})
    with op.batch_alter_table("detection_run") as batch:
        batch.create_unique_constraint("uq_detection_run_tenant_id", ["tenant_id", "run_number"])


def downgrade() -> None:
    with op.batch_alter_table("detection_run") as batch:
        batch.drop_constraint("uq_detection_run_tenant_id", type_="unique")
        batch.drop_column("run_number")
