"""Add topic-scoped runs, evidence, memory, and immutable artifact versions.

Revision ID: 20260929_0005
Revises: 20260929_0004
Create Date: 2026-09-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260929_0005"
down_revision = "20260929_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "topics",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("goal", sa.Text(), server_default="", nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_topics_archived_updated", "topics", ["archived_at", "updated_at"])

    op.create_table(
        "topic_documents",
        sa.Column("topic_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("topic_id", "document_id"),
    )
    op.create_index("ix_topic_documents_document", "topic_documents", ["document_id"])
    op.create_index("ix_topic_documents_topic_created", "topic_documents", ["topic_id", "created_at"])

    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=500), server_default="新会话", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversations_topic_updated", "conversations", ["topic_id", "updated_at"])

    op.create_table(
        "runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="queued", nullable=False),
        sa.Column("scope_mode", sa.String(length=16), nullable=False),
        sa.Column("scope_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("budget_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tool_calls_used", sa.Integer(), server_default="0", nullable=False),
        sa.Column("model_steps_used", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted', 'awaiting_user')", name="ck_runs_status"),
        sa.CheckConstraint("scope_mode IN ('topic', 'selected', 'library')", name="ck_runs_scope_mode"),
        sa.CheckConstraint("attempt_count >= 0 AND tool_calls_used >= 0 AND model_steps_used >= 0", name="ck_runs_counters_nonnegative"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "idempotency_key", name="uq_runs_conversation_idempotency"),
    )
    op.create_index("ix_runs_conversation_created", "runs", ["conversation_id", "created_at"])
    op.create_index("ix_runs_status_updated", "runs", ["status", "updated_at"])

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("sequence_number > 0", name="ck_messages_sequence_positive"),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence_number", name="uq_messages_run_sequence"),
    )
    op.create_index("ix_messages_run_created", "messages", ["run_id", "created_at"])

    op.create_table(
        "evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_unit_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=24), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("is_derived", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("quote_eligible", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidation_reason", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("page_number > 0", name="ck_evidence_page_positive"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["version_id"], ["document_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["content_unit_id"], ["content_units.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evidence_run_message", "evidence", ["run_id", "message_id"])
    op.create_index("ix_evidence_document_version_page", "evidence", ["document_id", "version_id", "page_number"])

    op.create_table(
        "tool_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("sequence_number > 0", name="ck_tool_calls_sequence_positive"),
        sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name="ck_tool_calls_status"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence_number", name="uq_tool_calls_run_sequence"),
        sa.UniqueConstraint("run_id", "idempotency_key", name="uq_tool_calls_run_idempotency"),
    )
    op.create_index("ix_tool_calls_run_status", "tool_calls", ["run_id", "status"])

    op.create_table(
        "run_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("sequence_number > 0", name="ck_run_events_sequence_positive"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence_number", name="uq_run_events_run_sequence"),
    )
    op.create_index("ix_run_events_run_created", "run_events", ["run_id", "created_at"])

    op.create_table(
        "memory_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("memory_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("evidence_snapshot", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("memory_type IN ('preference', 'research_conclusion', 'process_summary')", name="ck_memory_items_type"),
        sa.CheckConstraint("status IN ('active', 'pending', 'confirmed', 'rejected', 'invalidated', 'archived')", name="ck_memory_items_status"),
        sa.CheckConstraint("revision > 0", name="ck_memory_items_revision_positive"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_items_topic_status_type", "memory_items", ["topic_id", "status", "memory_type"])

    op.create_table(
        "artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("artifact_type", sa.String(length=32), nullable=False),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("artifact_type IN ('note', 'comparison_table', 'draft')", name="ck_artifacts_type"),
        sa.CheckConstraint("revision > 0", name="ck_artifacts_revision_positive"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifacts_topic_updated", "artifacts", ["topic_id", "updated_at"])

    op.create_table(
        "artifact_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("parent_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="candidate", nullable=False),
        sa.Column("body_text", sa.Text(), server_default="", nullable=False),
        sa.Column("structured_content", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("evidence_snapshot", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("adopted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version_number > 0", name="ck_artifact_versions_number_positive"),
        sa.CheckConstraint("origin IN ('user', 'generated')", name="ck_artifact_versions_origin"),
        sa.CheckConstraint("status IN ('current', 'candidate', 'superseded')", name="ck_artifact_versions_status"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["artifact_id", "parent_version_id"], ["artifact_versions.artifact_id", "artifact_versions.id"], name="fk_artifact_versions_parent_owner"),
        sa.ForeignKeyConstraint(["source_run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artifact_id", "version_number", name="uq_artifact_versions_number"),
        sa.UniqueConstraint("artifact_id", "id", name="uq_artifact_versions_owner_id"),
    )
    op.create_index("ix_artifact_versions_artifact_created", "artifact_versions", ["artifact_id", "created_at"])
    op.create_foreign_key(
        "fk_artifacts_current_version_owner",
        "artifacts",
        "artifact_versions",
        ["id", "current_version_id"],
        ["artifact_id", "id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    nonempty = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM topics) OR EXISTS (SELECT 1 FROM runs) "
            "OR EXISTS (SELECT 1 FROM memory_items) OR EXISTS (SELECT 1 FROM artifacts)"
        )
    ).scalar_one()
    if nonempty:
        raise RuntimeError("cannot downgrade M4 schema while topic, run, memory, or artifact data exists")

    op.drop_constraint("fk_artifacts_current_version_owner", "artifacts", type_="foreignkey")
    op.drop_index("ix_artifact_versions_artifact_created", table_name="artifact_versions")
    op.drop_table("artifact_versions")
    op.drop_index("ix_artifacts_topic_updated", table_name="artifacts")
    op.drop_table("artifacts")
    op.drop_index("ix_memory_items_topic_status_type", table_name="memory_items")
    op.drop_table("memory_items")
    op.drop_index("ix_run_events_run_created", table_name="run_events")
    op.drop_table("run_events")
    op.drop_index("ix_tool_calls_run_status", table_name="tool_calls")
    op.drop_table("tool_calls")
    op.drop_index("ix_evidence_document_version_page", table_name="evidence")
    op.drop_index("ix_evidence_run_message", table_name="evidence")
    op.drop_table("evidence")
    op.drop_index("ix_messages_run_created", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_runs_status_updated", table_name="runs")
    op.drop_index("ix_runs_conversation_created", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_conversations_topic_updated", table_name="conversations")
    op.drop_table("conversations")
    op.drop_index("ix_topic_documents_topic_created", table_name="topic_documents")
    op.drop_index("ix_topic_documents_document", table_name="topic_documents")
    op.drop_table("topic_documents")
    op.drop_index("ix_topics_archived_updated", table_name="topics")
    op.drop_table("topics")
