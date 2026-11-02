"""Add immutable versioned case data journal.

Revision ID: 0005_case_data
Revises: 0004_probe_results
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_case_data"
down_revision = "0004_probe_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "case_data",
        sa.Column("case_id", sa.String(length=160), primary_key=True),
        sa.Column("kind", sa.String(length=60), primary_key=True),
        sa.Column("record_id", sa.String(length=240), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index("ix_case_data_case_id", "case_data", ["case_id"])
    op.create_index("ix_case_data_case_kind", "case_data", ["case_id", "kind"])


def downgrade() -> None:
    op.drop_index("ix_case_data_case_kind", table_name="case_data")
    op.drop_index("ix_case_data_case_id", table_name="case_data")
    op.drop_table("case_data")
