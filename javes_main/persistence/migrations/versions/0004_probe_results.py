"""Add idempotent accepted probe results.

Revision ID: 0004_probe_results
Revises: 0003_outbox
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_probe_results"
down_revision = "0003_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "probe_results",
        sa.Column("fingerprint", sa.String(length=64), primary_key=True),
        sa.Column("job_id", sa.String(length=160), nullable=False),
        sa.Column("case_id", sa.String(length=160), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index("ix_probe_results_job_id", "probe_results", ["job_id"])
    op.create_index("ix_probe_results_case_id", "probe_results", ["case_id"])


def downgrade() -> None:
    op.drop_index("ix_probe_results_case_id", table_name="probe_results")
    op.drop_index("ix_probe_results_job_id", table_name="probe_results")
    op.drop_table("probe_results")
