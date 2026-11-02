"""Create core case store tables.

Revision ID: 0001_core_case_store
Revises: None
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001_core_case_store"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cases",
        sa.Column("case_id", sa.String(length=160), nullable=False),
        sa.Column("lifecycle", sa.String(length=40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("case_id"),
    )
    op.create_index("ix_cases_lifecycle", "cases", ["lifecycle"])
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(length=160), nullable=False),
        sa.Column("case_id", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("lease_owner", sa.String(length=160), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dedupe_key", sa.String(length=300), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("job_id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index("ix_jobs_case_id", "jobs", ["case_id"])
    op.create_index("ix_jobs_state", "jobs", ["state"])
    op.create_index("ix_jobs_lease_expires_at", "jobs", ["lease_expires_at"])
    op.create_index("ix_jobs_case_state", "jobs", ["case_id", "state"])
    op.create_table(
        "case_events",
        sa.Column("event_id", sa.String(length=160), nullable=False),
        sa.Column("case_id", sa.String(length=160), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_case_events_case_id", "case_events", ["case_id"])
    op.create_index("ix_case_events_event_type", "case_events", ["event_type"])
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(length=300), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_index("ix_case_events_event_type", table_name="case_events")
    op.drop_index("ix_case_events_case_id", table_name="case_events")
    op.drop_table("case_events")
    op.drop_index("ix_jobs_case_state", table_name="jobs")
    op.drop_index("ix_jobs_lease_expires_at", table_name="jobs")
    op.drop_index("ix_jobs_state", table_name="jobs")
    op.drop_index("ix_jobs_case_id", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_cases_lifecycle", table_name="cases")
    op.drop_table("cases")
