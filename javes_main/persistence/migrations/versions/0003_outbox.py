"""Add durable integration outbox.

Revision ID: 0003_outbox
Revises: 0002_internal_iam
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_outbox"
down_revision = "0002_internal_iam"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("message_id", sa.String(length=160), primary_key=True),
        sa.Column("case_id", sa.String(length=160), nullable=False),
        sa.Column("topic", sa.String(length=80), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("dedupe_key", sa.String(length=300), nullable=False, unique=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=160), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_outbox_case_id", "outbox", ["case_id"])
    op.create_index("ix_outbox_topic", "outbox", ["topic"])
    op.create_index("ix_outbox_state", "outbox", ["state"])
    op.create_index("ix_outbox_lease_expires_at", "outbox", ["lease_expires_at"])
    op.create_index("ix_outbox_state_available", "outbox", ["state", "available_at"])


def downgrade() -> None:
    op.drop_index("ix_outbox_state_available", table_name="outbox")
    op.drop_index("ix_outbox_lease_expires_at", table_name="outbox")
    op.drop_index("ix_outbox_state", table_name="outbox")
    op.drop_index("ix_outbox_topic", table_name="outbox")
    op.drop_index("ix_outbox_case_id", table_name="outbox")
    op.drop_table("outbox")
