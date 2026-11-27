"""Add durable authenticated reference-image uploads.

Revision ID: 0007_reference_image_uploads
Revises: 0006_routing_triggers
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_reference_image_uploads"
down_revision = "0006_routing_triggers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reference_image_uploads",
        sa.Column("upload_id", sa.String(length=160), primary_key=True),
        sa.Column("object_key", sa.String(length=500), nullable=False, unique=True),
        sa.Column("case_id", sa.String(length=160), nullable=False),
        sa.Column("operator_id", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_reference_image_uploads_case_id",
        "reference_image_uploads",
        ["case_id"],
    )
    op.create_index(
        "ix_reference_image_uploads_operator_id",
        "reference_image_uploads",
        ["operator_id"],
    )
    op.create_index(
        "ix_reference_image_uploads_status",
        "reference_image_uploads",
        ["status"],
    )
    op.create_index(
        "ix_reference_image_uploads_expires_at",
        "reference_image_uploads",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reference_image_uploads_expires_at",
        table_name="reference_image_uploads",
    )
    op.drop_index(
        "ix_reference_image_uploads_status",
        table_name="reference_image_uploads",
    )
    op.drop_index(
        "ix_reference_image_uploads_operator_id",
        table_name="reference_image_uploads",
    )
    op.drop_index(
        "ix_reference_image_uploads_case_id",
        table_name="reference_image_uploads",
    )
    op.drop_table("reference_image_uploads")
