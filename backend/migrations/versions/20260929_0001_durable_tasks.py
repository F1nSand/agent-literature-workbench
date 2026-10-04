"""Add durable background tasks with claim leases and fencing tokens.

Revision ID: 20260929_0001
Revises:
Create Date: 2026-09-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260929_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "background_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_type", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("fencing_token", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_background_tasks_status"),
        sa.CheckConstraint("attempt_count >= 0 AND max_attempts > 0", name="ck_background_tasks_attempts"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_background_tasks_claim", "background_tasks", ["status", "created_at"])
    op.create_index("ix_background_tasks_expired_lease", "background_tasks", ["lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_background_tasks_expired_lease", table_name="background_tasks")
    op.drop_index("ix_background_tasks_claim", table_name="background_tasks")
    op.drop_table("background_tasks")
    # Keep the pgvector extension: other schemas may depend on it.
