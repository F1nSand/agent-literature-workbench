"""Reading positions, annotations, metadata, and stable source locations."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261001_0008"
down_revision = "20261001_0007"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("authors", "tags"):
        op.add_column("documents", sa.Column(name, sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("documents", sa.Column("year", sa.Integer(), nullable=True))
    op.add_column("documents", sa.Column("doi", sa.String(250), nullable=True))
    op.add_column("documents", sa.Column("favorite", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("documents", sa.Column("reading_status", sa.String(16), nullable=False, server_default="unread"))
    op.add_column("document_pages", sa.Column("locator", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("document_pages", sa.Column("location_label", sa.String(150), nullable=False, server_default=""))
    op.execute("UPDATE document_pages SET locator = json_build_object('kind','page','page',page_number), location_label = '第 ' || page_number || ' 页'")
    op.create_table("reading_states",
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_versions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("locator", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("reading_annotations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("locator", sa.JSON(), nullable=False), sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False), sa.Column("color", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_reading_annotations_version", "reading_annotations", ["version_id", "created_at"])


def downgrade():
    op.drop_table("reading_annotations")
    op.drop_table("reading_states")
    op.drop_column("document_pages", "location_label")
    op.drop_column("document_pages", "locator")
    for name in ("reading_status", "favorite", "doi", "year", "tags", "authors"):
        op.drop_column("documents", name)
