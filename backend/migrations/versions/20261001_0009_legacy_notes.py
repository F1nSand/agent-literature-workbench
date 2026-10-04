"""Project legacy topic artifacts into independent notes without removing them.

Revision ID: 20261001_0009
Revises: 20261001_0008
"""
from __future__ import annotations

from uuid import UUID, uuid5

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261001_0009"
down_revision = "20261001_0008"
branch_labels = None
depends_on = None

EMPTY_NOTE_NAMESPACE = UUID("d1db6e70-d4e5-4f38-86bf-0033d7ea614c")


def _columns(bind, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _constraint_names(bind, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    return {
        constraint.get("name")
        for constraint in (
            *inspector.get_unique_constraints(table_name),
            *inspector.get_foreign_keys(table_name),
        )
    }


def _add_column_if_missing(bind, table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(bind, table_name):
        op.add_column(table_name, column)


def _paragraph_document(text: str) -> dict:
    """Keep the exact old text in one editor text node, including its newlines."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    nodes = [{"type": "text", "text": normalized}] if normalized else []
    return {"type": "doc", "content": [{"type": "paragraph", "content": nodes}]}


def _plain_text(text: str) -> str:
    # Match the normal note plain-text projection while keeping original line breaks.
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _import_legacy_artifacts(bind) -> None:
    artifacts = sa.table(
        "artifacts",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("title", sa.String()),
        sa.column("artifact_type", sa.String()),
        sa.column("current_version_id", postgresql.UUID(as_uuid=True)),
        sa.column("revision", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    artifact_versions = sa.table(
        "artifact_versions",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("artifact_id", postgresql.UUID(as_uuid=True)),
        sa.column("version_number", sa.Integer()),
        sa.column("parent_version_id", postgresql.UUID(as_uuid=True)),
        sa.column("origin", sa.String()),
        sa.column("status", sa.String()),
        sa.column("body_text", sa.Text()),
        sa.column("structured_content", postgresql.JSONB()),
        sa.column("content_sha256", sa.String()),
        sa.column("source_run_id", postgresql.UUID(as_uuid=True)),
        sa.column("evidence_snapshot", postgresql.JSONB()),
        sa.column("adopted_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    notes = sa.table(
        "notes",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("title", sa.String()),
        sa.column("revision", sa.Integer()),
        sa.column("current_version_id", postgresql.UUID(as_uuid=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("legacy_artifact_id", postgresql.UUID(as_uuid=True)),
        sa.column("legacy_artifact_type", sa.String()),
    )
    note_versions = sa.table(
        "note_versions",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("note_id", postgresql.UUID(as_uuid=True)),
        sa.column("version_number", sa.Integer()),
        sa.column("base_revision", sa.Integer()),
        sa.column("parent_version_id", postgresql.UUID(as_uuid=True)),
        sa.column("origin", sa.String()),
        sa.column("status", sa.String()),
        sa.column("content", postgresql.JSONB()),
        sa.column("plain_text", sa.Text()),
        sa.column("source_run_id", postgresql.UUID(as_uuid=True)),
        sa.column("adopted_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("legacy_artifact_version_id", postgresql.UUID(as_uuid=True)),
        sa.column("legacy_content_sha256", sa.String()),
        sa.column("legacy_evidence_snapshot", postgresql.JSONB()),
        sa.column("legacy_structured_content", postgresql.JSONB()),
    )

    artifacts_data = bind.execute(sa.select(artifacts).order_by(artifacts.c.id)).mappings().all()
    if not artifacts_data:
        return

    insert_note = postgresql.insert(notes)
    for artifact in artifacts_data:
        bind.execute(
            insert_note.values(
                id=artifact["id"], title=artifact["title"], revision=max(artifact["revision"] or 1, 1),
                current_version_id=None, created_at=artifact["created_at"], updated_at=artifact["updated_at"],
                legacy_artifact_id=artifact["id"], legacy_artifact_type=artifact["artifact_type"],
            ).on_conflict_do_nothing(index_elements=[notes.c.legacy_artifact_id])
        )

    versions_data = bind.execute(
        sa.select(artifact_versions).order_by(artifact_versions.c.artifact_id, artifact_versions.c.version_number)
    ).mappings().all()
    insert_version = postgresql.insert(note_versions)
    for version in versions_data:
        text = version["body_text"] or ""
        bind.execute(
            insert_version.values(
                id=version["id"], note_id=version["artifact_id"], version_number=version["version_number"],
                base_revision=None, parent_version_id=None, origin=version["origin"], status=version["status"],
                content=_paragraph_document(text), plain_text=_plain_text(text), source_run_id=version["source_run_id"],
                adopted_at=version["adopted_at"], created_at=version["created_at"],
                legacy_artifact_version_id=version["id"], legacy_content_sha256=version["content_sha256"],
                legacy_evidence_snapshot=version["evidence_snapshot"],
                legacy_structured_content=version["structured_content"],
            ).on_conflict_do_nothing(index_elements=[note_versions.c.legacy_artifact_version_id])
        )

    # Add parent pointers in a second pass so source ordering cannot violate the self-FK.
    for version in versions_data:
        parent_id = version["parent_version_id"]
        if parent_id is not None:
            bind.execute(
                sa.update(note_versions).where(
                    note_versions.c.legacy_artifact_version_id == version["id"],
                    note_versions.c.parent_version_id.is_(None),
                ).values(parent_version_id=parent_id)
            )

    for artifact in artifacts_data:
        artifact_id = artifact["id"]
        target_version_id = artifact["current_version_id"]
        if target_version_id is None:
            target_version_id = bind.execute(
                sa.select(artifact_versions.c.id).where(
                    artifact_versions.c.artifact_id == artifact_id,
                    artifact_versions.c.status == "current",
                ).order_by(artifact_versions.c.version_number.desc()).limit(1)
            ).scalar_one_or_none()
        if target_version_id is None:
            note_id = bind.execute(sa.select(notes.c.id).where(notes.c.legacy_artifact_id == artifact_id)).scalar_one()
            has_version = bind.execute(sa.select(note_versions.c.id).where(note_versions.c.note_id == note_id).limit(1)).scalar_one_or_none()
            if has_version is None:
                number = (bind.execute(sa.select(sa.func.max(note_versions.c.version_number)).where(note_versions.c.note_id == note_id)).scalar_one() or 0) + 1
                fallback_id = uuid5(EMPTY_NOTE_NAMESPACE, str(artifact_id))
                bind.execute(insert_version.values(
                    id=fallback_id, note_id=note_id, version_number=number, base_revision=None,
                    parent_version_id=None, origin="user", status="current", content=_paragraph_document(""),
                    plain_text="", source_run_id=None, adopted_at=artifact["updated_at"],
                    created_at=artifact["updated_at"], legacy_artifact_version_id=None,
                    legacy_content_sha256=None, legacy_evidence_snapshot=None, legacy_structured_content=None,
                ).on_conflict_do_nothing())
                target_version_id = fallback_id
        if target_version_id is not None:
            bind.execute(
                sa.update(notes).where(
                    notes.c.legacy_artifact_id == artifact_id,
                    notes.c.current_version_id.is_(None),
                ).values(current_version_id=target_version_id)
            )


def upgrade() -> None:
    bind = op.get_bind()
    _add_column_if_missing(bind, "notes", sa.Column("legacy_artifact_id", postgresql.UUID(as_uuid=True), nullable=True))
    _add_column_if_missing(bind, "notes", sa.Column("legacy_artifact_type", sa.String(length=32), nullable=True))
    _add_column_if_missing(bind, "note_versions", sa.Column("legacy_artifact_version_id", postgresql.UUID(as_uuid=True), nullable=True))
    _add_column_if_missing(bind, "note_versions", sa.Column("legacy_content_sha256", sa.String(length=64), nullable=True))
    _add_column_if_missing(bind, "note_versions", sa.Column("legacy_evidence_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    _add_column_if_missing(bind, "note_versions", sa.Column("legacy_structured_content", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    names = _constraint_names(bind, "notes")
    if "uq_notes_legacy_artifact" not in names:
        op.create_unique_constraint("uq_notes_legacy_artifact", "notes", ["legacy_artifact_id"])
    if "fk_notes_legacy_artifact" not in names:
        op.create_foreign_key("fk_notes_legacy_artifact", "notes", "artifacts", ["legacy_artifact_id"], ["id"], ondelete="RESTRICT")
    names = _constraint_names(bind, "note_versions")
    if "uq_note_versions_legacy_artifact_version" not in names:
        op.create_unique_constraint("uq_note_versions_legacy_artifact_version", "note_versions", ["legacy_artifact_version_id"])
    if "fk_note_versions_legacy_artifact_version" not in names:
        op.create_foreign_key("fk_note_versions_legacy_artifact_version", "note_versions", "artifact_versions", ["legacy_artifact_version_id"], ["id"], ondelete="RESTRICT")
    _import_legacy_artifacts(bind)


def downgrade() -> None:
    """Keep legacy sources and their projected notes intact on rollback.

    This revision only adds data and metadata. A rollback changes Alembic's revision
    marker without dropping either legacy artifact tables or the migrated notes.
    Keeping the additions also makes a later re-upgrade idempotent.
    """
    return None
