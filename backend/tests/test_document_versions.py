import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.documents.models import ContentUnit, Document, DocumentPage, DocumentVersion


def test_multiple_document_versions_keep_page_content_separate() -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL version test")

    engine = create_engine(database_url, pool_pre_ping=True)
    document_id = uuid4()
    version_ids = [uuid4(), uuid4()]
    try:
        with Session(engine) as session:
            document = Document(
                id=document_id,
                title="version isolation test",
                filename="version-test.pdf",
                storage_key=uuid4().hex,
                sha256=uuid4().hex + uuid4().hex,
                status="ready",
                page_count=1,
                is_deleted=False,
            )
            session.add(document)
            for index, version_id in enumerate(version_ids, start=1):
                session.add(DocumentVersion(
                    id=version_id,
                    document_id=document_id,
                    version_number=index,
                    filename=f"version-{index}.pdf",
                    storage_key=uuid4().hex,
                    sha256=uuid4().hex + uuid4().hex,
                    status="ready",
                    page_count=1,
                    processing_config_hash="test-config",
                ))
            session.flush()
            for index, version_id in enumerate(version_ids, start=1):
                session.add(DocumentPage(
                    document_id=document_id,
                    version_id=version_id,
                    page_number=1,
                    text=f"version {index}",
                    text_char_count=9,
                ))
            session.flush()
            for index, version_id in enumerate(version_ids, start=1):
                session.add(ContentUnit(
                    id=uuid4(),
                    document_id=document_id,
                    version_id=version_id,
                    page_number=1,
                    ordinal=0,
                    content_type="text",
                    text=f"version {index}",
                    text_sha256=uuid4().hex + uuid4().hex,
                    token_estimate=2,
                    extra={},
                ))
            session.flush()

            for version_id in version_ids:
                page = session.get(DocumentPage, (version_id, 1))
                units = session.scalars(select(ContentUnit).where(ContentUnit.version_id == version_id)).all()
                assert page is not None
                assert len(units) == 1
                assert page.text == units[0].text
            session.rollback()
    finally:
        engine.dispose()
