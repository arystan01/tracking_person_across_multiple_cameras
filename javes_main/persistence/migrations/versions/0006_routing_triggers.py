"""Add durable Routing triggers.

Revision ID: 0006_routing_triggers
Revises: 0005_case_data
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_routing_triggers"
down_revision = "0005_case_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "routing_triggers",
        sa.Column("trigger_id", sa.String(length=160), primary_key=True),
        sa.Column("case_id", sa.String(length=160), nullable=False),
        sa.Column("trigger_type", sa.String(length=60), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("dedupe_key", sa.String(length=300), nullable=False, unique=True),
        sa.Column("lease_owner", sa.String(length=160), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_routing_triggers_case_id", "routing_triggers", ["case_id"])
    op.create_index("ix_routing_triggers_state", "routing_triggers", ["state"])
    op.create_index("ix_routing_triggers_lease_expires_at", "routing_triggers", ["lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_routing_triggers_lease_expires_at", table_name="routing_triggers")
    op.drop_index("ix_routing_triggers_state", table_name="routing_triggers")
    op.drop_index("ix_routing_triggers_case_id", table_name="routing_triggers")
    op.drop_table("routing_triggers")
