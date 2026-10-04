import csv
import hashlib
import io
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.artifacts import api as artifact_routes
from app.artifacts.export import ExportSource, render_csv, render_markdown, safe_csv_cell
from app.artifacts.models import Artifact, ArtifactVersion
from app.documents.models import Document, DocumentVersion
from app.main import app
from app.topics.models import Topic
from app.artifacts.export import ExportSource, render_csv, render_markdown, safe_csv_cell


def test_csv_cells_escape_spreadsheet_formula_prefixes() -> None:
    for prefix in ("=", "+", "-", "@", "\t", "\r", "\n", "\u00a0=", "\v="):
        assert safe_csv_cell(f"{prefix}formula") == f"'{prefix}formula"
        assert safe_csv_cell(f"  {prefix}formula") == f"'  {prefix}formula"
    assert safe_csv_cell("  \tordinary") == "'  \tordinary"
    assert safe_csv_cell(6) == "6"
    assert safe_csv_cell("普通中文正文") == "普通中文正文"


def test_markdown_export_keeps_chinese_body_version_and_page_citations() -> None:
    source = ExportSource(
        document_title="中文论文",
        document_id="doc-1",
        version_id="version-1",
        version_number=2,
        page_number=6,
        content_unit_id="unit-1",
        content_sha256="a" * 64,
    )

    result = render_markdown(
        title="研究笔记",
        artifact_type="note",
        version_number=3,
        body_text="结论\n|指标|数值|",
        sources=[source],
    )

    assert "# 研究笔记" in result
    assert "版本：3" in result
    assert "结论\n|指标|数值|" in result
    assert "中文论文" in result
    assert "第 6 页" in result


def test_csv_round_trip_preserves_unicode_newlines_table_and_sources() -> None:
    source = ExportSource(
        document_title="中文论文",
        document_id="doc-1",
        version_id="version-1",
        version_number=2,
        page_number=6,
        content_unit_id="unit-1",
        content_sha256="a" * 64,
    )
    content = render_csv(
        title="研究笔记",
        artifact_type="comparison_table",
        version_number=3,
        body_text="|指标|数值|\n|---|---|\n|召回率|0.8|",
        sources=[source],
    )

    assert content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    assert rows[0][:4] == ["成果名称", "成果类型", "成果版本", "正文"]
    assert rows[1][0] == "研究笔记"
    assert rows[1][1] == "文献比较"
    assert rows[1][2] == "3"
    assert rows[1][3] == "|指标|数值|\n|---|---|\n|召回率|0.8|"
    assert rows[1][4] == "中文论文"
    assert rows[1][7] == "6"


def test_csv_without_sources_still_has_one_body_row() -> None:
    content = render_csv(
        title="无引用笔记",
        artifact_type="note",
        version_number=1,
        body_text="保留正文",
        sources=[],
    )
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    assert len(rows) == 2
    assert rows[1][:4] == ["无引用笔记", "阅读笔记", "1", "保留正文"]
    assert rows[1][4:] == [""] * (len(rows[0]) - 4)


def test_artifact_exports_current_version_and_rejects_archived_topic(monkeypatch) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL artifact export test")

    from sqlalchemy import create_engine

    engine = create_engine(database_url, pool_pre_ping=True)
    monkeypatch.setattr(artifact_routes, "get_engine", lambda: engine)
    topic_id, document_id, document_version_id = uuid4(), uuid4(), uuid4()
    artifact_id, artifact_version_id = uuid4(), uuid4()
    content_unit_id, evidence_id = uuid4(), uuid4()
    body = "|指标|数值|\n|---|---|\n|准确率|0.8|"
    client = TestClient(app)

    try:
        with Session(engine) as session:
            topic = Topic(id=topic_id, title="导出验证", goal="验证保存成果导出")
            document = Document(
                id=document_id,
                title="\t=IMPORTDATA(1)",
                filename="source.pdf",
                storage_key=uuid4().hex,
                sha256=uuid4().hex + uuid4().hex,
                status="ready",
                page_count=6,
                active_version_id=None,
                is_deleted=False,
            )
            document_version = DocumentVersion(
                id=document_version_id,
                document_id=document_id,
                version_number=2,
                filename="source.pdf",
                storage_key=uuid4().hex,
                sha256=uuid4().hex + uuid4().hex,
                status="ready",
                page_count=6,
                processing_config_hash="e" * 64,
            )
            session.add(topic)
            session.flush()
            session.add_all([document, document_version])
            session.flush()
            artifact = Artifact(
                id=artifact_id,
                topic_id=topic_id,
                title="=2+2",
                artifact_type="comparison_table",
                revision=1,
                current_version_id=None,
            )
            session.add(artifact)
            session.flush()
            document.active_version_id = document_version_id
            version = ArtifactVersion(
                id=artifact_version_id,
                artifact_id=artifact_id,
                version_number=3,
                parent_version_id=None,
                origin="user",
                status="current",
                body_text=body,
                structured_content=None,
                content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                source_run_id=None,
                evidence_snapshot=[{
                    "evidence_id": str(evidence_id),
                    "document_id": str(document_id),
                    "version_id": str(document_version_id),
                    "page_number": 6,
                    "content_unit_id": str(content_unit_id),
                    "content_sha256": "a" * 64,
                }],
            )
            session.add(version)
            session.flush()
            artifact.current_version_id = artifact_version_id
            session.commit()

        markdown = client.get(f"/api/v1/artifacts/{artifact_id}/export", params={"format": "markdown"})
        assert markdown.status_code == 200, markdown.text
        assert markdown.headers["content-type"].startswith("text/markdown")
        assert "attachment; filename=\"artifact-" in markdown.headers["content-disposition"]
        assert "# =2+2" in markdown.text
        assert "版本：3" in markdown.text
        assert body in markdown.text
        assert "第 6 页" in markdown.text

        exported = client.get(f"/api/v1/artifacts/{artifact_id}/export", params={"format": "csv"})
        assert exported.status_code == 200, exported.text
        assert exported.headers["content-type"].startswith("text/csv")
        rows = list(csv.reader(io.StringIO(exported.content.decode("utf-8-sig"))))
        assert rows[1][0] == "'=2+2"
        assert rows[1][1:4] == ["文献比较", "3", body]
        assert rows[1][4] == "'\t=IMPORTDATA(1)"
        assert rows[1][6:8] == ["2", "6"]

        with Session(engine) as session:
            topic = session.get(Topic, topic_id)
            assert topic is not None
            topic.archived_at = datetime.now(timezone.utc)
            session.commit()
        archived = client.get(f"/api/v1/artifacts/{artifact_id}/export", params={"format": "csv"})
        assert archived.status_code == 409
        missing = client.get(f"/api/v1/artifacts/{uuid4()}/export", params={"format": "csv"})
        assert missing.status_code == 404
    finally:
        with Session(engine) as session:
            artifact = session.get(Artifact, artifact_id)
            if artifact is not None:
                artifact.current_version_id = None
            document = session.get(Document, document_id)
            if document is not None:
                document.active_version_id = None
            session.flush()
            session.execute(delete(ArtifactVersion).where(ArtifactVersion.artifact_id == artifact_id))
            session.execute(delete(Artifact).where(Artifact.id == artifact_id))
            session.execute(delete(DocumentVersion).where(DocumentVersion.id == document_version_id))
            session.execute(delete(Document).where(Document.id == document_id))
            session.execute(delete(Topic).where(Topic.id == topic_id))
            session.commit()
        engine.dispose()
