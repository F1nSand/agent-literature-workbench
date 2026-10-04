"""PostgreSQL integration tests; point WORKBENCH_TEST_DATABASE_URL at the isolated Docker test DB."""
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, update
from sqlalchemy.orm import Session

from app.documents.models import Document, DocumentPage, DocumentVersion, ReadingAnnotation, ReadingState
from app.documents import reader_api
from app.main import app


def test_reader_state_and_annotations_are_version_scoped_and_locators_are_validated(monkeypatch) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run reader PostgreSQL API tests")
    engine = create_engine(database_url, pool_pre_ping=True)
    monkeypatch.setattr(reader_api, "get_engine", lambda: engine)
    document_id, version_a_id, version_b_id = uuid4(), uuid4(), uuid4()
    annotation_id = uuid4()
    client = TestClient(app)
    try:
        with Session(engine) as session:
            doc = Document(
                id=document_id, title="Versioned reader paper", filename="paper.md", storage_key=uuid4().hex,
                sha256=uuid4().hex + uuid4().hex, status="ready", page_count=2, is_deleted=False,
            )
            session.add(doc)
            session.flush()
            versions = [
                DocumentVersion(id=version_a_id, document_id=document_id, version_number=1, filename="paper.md",
                    storage_key=uuid4().hex, sha256=uuid4().hex + uuid4().hex, status="ready", page_count=2,
                    processing_config_hash="a" * 64),
                DocumentVersion(id=version_b_id, document_id=document_id, version_number=2, filename="paper.md",
                    storage_key=uuid4().hex, sha256=uuid4().hex + uuid4().hex, status="ready", page_count=2,
                    processing_config_hash="b" * 64),
            ]
            session.add_all(versions)
            session.flush()
            doc.active_version_id = version_b_id
            for version_id, marker in ((version_a_id, "old"), (version_b_id, "new")):
                session.add_all([
                    DocumentPage(document_id=document_id, version_id=version_id, page_number=1,
                        text=f"{marker} paragraph one", text_char_count=19,
                        locator={"kind": "text", "anchor": "p-1", "section": 1}, location_label="Paragraph 1",
                        extraction_method="plain_text"),
                    DocumentPage(document_id=document_id, version_id=version_id, page_number=2,
                        text=f"{marker} paragraph two", text_char_count=19,
                        locator={"kind": "text", "anchor": "p-2", "section": 2}, location_label="Paragraph 2",
                        extraction_method="plain_text"),
                ])
            session.add(ReadingState(document_id=document_id, version_id=version_a_id,
                                     locator={"kind": "text", "anchor": "p-2", "section": 2}))
            session.add(ReadingAnnotation(id=annotation_id, document_id=document_id, version_id=version_a_id,
                locator={"kind": "text", "anchor": "p-1", "section": 1}, quote="old quote",
                comment="old note", color="yellow"))
            session.commit()

        manifest_b = client.get(f"/api/v1/documents/{document_id}/reader", params={"version_id": str(version_b_id)})
        assert manifest_b.status_code == 200, manifest_b.text
        assert manifest_b.json()["file_format"] == "md"
        assert manifest_b.json()["sections"][1]["locator"]["anchor"] == "p-2"
        assert manifest_b.json()["reading_state"] is None
        assert manifest_b.json()["annotations"] == []

        manifest_a = client.get(f"/api/v1/documents/{document_id}/reader", params={"version_id": str(version_a_id)})
        assert manifest_a.json()["reading_state"]["locator"]["anchor"] == "p-2"
        assert len(manifest_a.json()["annotations"]) == 1
        assert manifest_a.json()["annotations"][0]["quote"] == "old quote"

        invalid_position = client.put(f"/api/v1/documents/{document_id}/reading-state", json={
            "version_id": str(version_b_id), "locator": {"kind": "text", "anchor": "p-999", "section": 999},
        })
        assert invalid_position.status_code == 422
        mismatched_position = client.put(f"/api/v1/documents/{document_id}/reading-state", json={
            "version_id": str(version_b_id), "locator": {"kind": "text", "anchor": "p-2", "section": 999},
        })
        assert mismatched_position.status_code == 422
        unexpected_position = client.put(f"/api/v1/documents/{document_id}/reading-state", json={
            "version_id": str(version_b_id), "locator": {"kind": "text", "anchor": "p-2", "page": 2},
        })
        assert unexpected_position.status_code == 422
        wrong_format = client.post(f"/api/v1/documents/{document_id}/annotations", json={
            "version_id": str(version_b_id), "locator": {"kind": "epub", "chapter": "OPS/c.xhtml"},
            "quote": "quote", "comment": "bad locator",
        })
        assert wrong_format.status_code == 422

        saved_position = client.put(f"/api/v1/documents/{document_id}/reading-state", json={
            "version_id": str(version_b_id), "locator": {"kind": "text", "anchor": "p-1", "section": 1},
        })
        assert saved_position.status_code == 200
        saved_annotation = client.post(f"/api/v1/documents/{document_id}/annotations", json={
            "version_id": str(version_b_id), "locator": {"kind": "text", "anchor": "p-2", "section": 2},
            "quote": "new quote", "comment": "new annotation", "color": "blue",
        })
        assert saved_annotation.status_code == 201, saved_annotation.text
        refreshed_b = client.get(f"/api/v1/documents/{document_id}/reader", params={"version_id": str(version_b_id)}).json()
        assert refreshed_b["reading_state"]["locator"]["anchor"] == "p-1"
        assert [item["quote"] for item in refreshed_b["annotations"]] == ["new quote"]
        assert len(client.get(f"/api/v1/documents/{document_id}/reader", params={"version_id": str(version_a_id)}).json()["annotations"]) == 1

        invalid_metadata = client.patch(f"/api/v1/documents/{document_id}/metadata", json={"title": "   "})
        assert invalid_metadata.status_code == 422
        for required_field in ("title", "authors", "tags", "favorite", "reading_status"):
            response = client.patch(f"/api/v1/documents/{document_id}/metadata", json={required_field: None})
            assert response.status_code == 422, (required_field, response.text)
        optional_metadata = client.patch(f"/api/v1/documents/{document_id}/metadata", json={"year": None, "doi": None})
        assert optional_metadata.status_code == 200
        metadata = client.patch(f"/api/v1/documents/{document_id}/metadata", json={
            "title": "Curated title", "authors": [" Ada Lovelace ", "Ada Lovelace"], "year": 1843,
            "doi": "10.1000/example", "tags": ["methods", " methods "], "favorite": True,
            "reading_status": "read",
        })
        assert metadata.status_code == 200, metadata.text
        assert metadata.json()["title"] == "Curated title"
        assert metadata.json()["authors"] == ["Ada Lovelace"]
        assert metadata.json()["tags"] == ["methods"]
        assert metadata.json()["favorite"] is True and metadata.json()["reading_status"] == "read"
    finally:
        with Session(engine) as session, session.begin():
            session.execute(update(Document).where(Document.id == document_id).values(active_version_id=None))
            session.execute(delete(Document).where(Document.id == document_id))
        engine.dispose()
