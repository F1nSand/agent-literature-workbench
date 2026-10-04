"""Add topic-independent conversations and pinned assistant context.

Revision ID: 20261001_0007
Revises: 20261001_0006
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261001_0007"
down_revision = "20261001_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("conversations", "topic_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.add_column("conversations", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_conversations_archived_updated", "conversations", ["archived_at", "updated_at"])
    op.add_column("runs", sa.Column("page_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_table(
        "run_ui_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_id", sa.String(length=32), nullable=False),
        sa.Column("action_type", sa.String(length=40), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("acknowledgement", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("error", sa.String(length=200), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'succeeded', 'failed')", name="ck_run_ui_actions_status"),
        sa.CheckConstraint("action_type IN ('open_document', 'show_document_choices', 'show_note_candidate')", name="ck_run_ui_actions_type"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "action_id", name="uq_run_ui_actions_identity"),
    )
    op.create_index("ix_run_ui_actions_run_created", "run_ui_actions", ["run_id", "created_at"])


def downgrade() -> None:
    op.drop_column("runs", "page_context")
    op.drop_index("ix_run_ui_actions_run_created", table_name="run_ui_actions")
    op.drop_table("run_ui_actions")
    op.drop_index("ix_conversations_archived_updated", table_name="conversations")
    op.drop_column("conversations", "archived_at")
    op.alter_column("conversations", "topic_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
