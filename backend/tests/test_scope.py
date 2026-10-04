import hashlib
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from app.documents.models import ContentUnit, Document, DocumentPage, DocumentVersion, IndexGeneration
from app.memory.models import MemoryItem
from app.runs.models import Run  # Register MemoryItem's optional source_run_id foreign-key target.
from app.topics.access import (
    list_scope_documents,
    list_scope_memories,
    read_scope_page,
    scoped_pdf_info,
    validate_scope_reference,
)
from app.topics.models import Topic, TopicDocument
from app.topics.scope import (
    ResearchScope,
    ScopeError,
    ScopeForbidden,
    ScopeStale,
    ScopedDocument,
    resolve_scope,
    resolve_assistant_scope,
    validate_scope_current,
)


def _add_ready_document(session: Session, document_id, title: str):
    version_id = uuid4()
    digest = uuid4().hex + uuid4().hex
    document = Document(
        id=document_id,
        title=title,
        filename=f"{title}.pdf",
        storage_key=uuid4().hex,
        sha256=digest,
        status="ready",
        page_count=1,
        active_version_id=None,
        is_deleted=False,
    )
    session.add(document)
    session.flush()
    version = DocumentVersion(
        id=version_id,
        document_id=document_id,
        version_number=1,
        filename=f"{title}.pdf",
        storage_key=uuid4().hex,
        sha256=uuid4().hex + uuid4().hex,
        status="ready",
        page_count=1,
        processing_config_hash="a" * 64,
    )
    session.add(version)
    session.flush()
    document.active_version_id = version.id
    session.flush()
    return document, version


def test_assistant_scope_uses_separate_v2_snapshot_and_preserves_v1_hash() -> None:
    from app.topics.scope import _hash_payload

    topic_id, document_id, version_id, generation_id = uuid4(), uuid4(), uuid4(), uuid4()
    item = ScopedDocument(document_id, version_id)
    legacy = ResearchScope.create("library", topic_id, [item], generation_id)
    expected_v1 = {
        "schema_version": 1, "mode": "library", "topic_id": str(topic_id),
        "documents": [{"document_id": str(document_id), "version_id": str(version_id)}],
        "index_generation_id": str(generation_id),
    }
    assert legacy.to_snapshot() == expected_v1
    assert legacy.scope_hash == _hash_payload(expected_v1)
    assert ResearchScope.from_snapshot(expected_v1, legacy.scope_hash).topic_id == topic_id

    assistant = ResearchScope.create_assistant("library", [item], generation_id)
    assert assistant.topic_id is None
    assert assistant.to_snapshot()["schema_version"] == 2
    assert "topic_id" not in assistant.to_snapshot()
    restored = ResearchScope.from_snapshot(assistant.to_snapshot(), assistant.scope_hash)
    assert restored.topic_id is None
    assert restored.scope_hash == assistant.scope_hash


def test_global_run_contract_accepts_library_context_without_topic() -> None:
    from app.topics.schemas import RunCreate

    payload = RunCreate.model_validate({
        "question": "总结当前页面",
        "idempotency_key": "client-window-1",
        "scope": {"mode": "library"},
        "page_context": {"page_type": "reader", "document_id": str(uuid4()),
                         "version_id": str(uuid4()), "locator": {"kind": "page", "page": 1},
                         "selection": "bounded selection"},
    })
    assert payload.scope is not None and payload.scope.mode == "library"
    assert payload.page_context is not None and payload.page_context.page_type == "reader"


def test_scope_modes_are_explicit_isolated_and_version_pinned() -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL scope test")

    engine = create_engine(database_url, pool_pre_ping=True)
    topic_a_id, topic_b_id = uuid4(), uuid4()
    doc_a_id, doc_b_id, shared_id, library_id = uuid4(), uuid4(), uuid4(), uuid4()
    initial_generation_id = None
    created_initial_generation = False
    replacement_generation_id = None
    version_a_id = None
    version_b_id = None
    version_a_storage_key = None

    try:
        with Session(engine) as session:
            initial_generation = session.scalar(
                select(IndexGeneration).where(IndexGeneration.status == "active").with_for_update()
            )
            if initial_generation is None:
                initial_generation_id = uuid4()
                created_initial_generation = True
                session.add(IndexGeneration(
                    id=initial_generation_id,
                    provider="test",
                    model="test-embedding",
                    dimensions=2560,
                    config_hash="b" * 64,
                    status="active",
                ))
            else:
                initial_generation_id = initial_generation.id

            topic_a = Topic(id=topic_a_id, title="Scope A")
            topic_b = Topic(id=topic_b_id, title="Scope B")
            session.add_all([topic_a, topic_b])
            _, version_a = _add_ready_document(session, doc_a_id, "paper-a")
            _, version_b = _add_ready_document(session, doc_b_id, "paper-b")
            version_a_id = version_a.id
            version_b_id = version_b.id
            version_a_storage_key = version_a.storage_key
            _add_ready_document(session, shared_id, "shared-paper")
            _add_ready_document(session, library_id, "library-only")
            page_text = "Original excerpt for scope verification. OCR note is not a quote."
            session.add(DocumentPage(
                document_id=doc_a_id,
                version_id=version_a.id,
                page_number=1,
                text=page_text,
                text_char_count=len(page_text),
                extraction_method="pdf_text",
            ))
            session.flush()
            original_unit_id, derived_unit_id = uuid4(), uuid4()
            original_text = "Original excerpt for scope verification."
            derived_text = "OCR note is not a quote."
            session.add_all([
                ContentUnit(
                    id=original_unit_id,
                    document_id=doc_a_id,
                    version_id=version_a.id,
                    page_number=1,
                    ordinal=0,
                    content_type="text",
                    text=original_text,
                    text_sha256=hashlib.sha256(original_text.encode()).hexdigest(),
                    search_text=original_text,
                    token_estimate=7,
                    extra={"source": "pdf_text", "quote_eligible": True},
                ),
                ContentUnit(
                    id=derived_unit_id,
                    document_id=doc_a_id,
                    version_id=version_a.id,
                    page_number=1,
                    ordinal=1,
                    content_type="text",
                    text=derived_text,
                    text_sha256=hashlib.sha256(derived_text.encode()).hexdigest(),
                    search_text=derived_text,
                    token_estimate=5,
                    extra={"source": "ocr", "is_derived": True, "quote_eligible": False},
                ),
                MemoryItem(
                    topic_id=topic_a_id,
                    memory_type="research_conclusion",
                    status="confirmed",
                    content="Confirmed topic conclusion.",
                    evidence_snapshot=[{
                        "document_id": str(doc_a_id),
                        "version_id": str(version_a_id),
                        "page_number": 1,
                    }],
                    confirmed_at=func.now(),
                ),
                MemoryItem(
                    topic_id=topic_a_id,
                    memory_type="research_conclusion",
                    status="pending",
                    content="Unconfirmed topic conclusion.",
                    evidence_snapshot=[{
                        "document_id": str(doc_a_id),
                        "version_id": str(version_a_id),
                        "page_number": 1,
                    }],
                ),
                MemoryItem(
                    topic_id=topic_a_id,
                    memory_type="process_summary",
                    status="active",
                    content="Topic-only process context.",
                    evidence_snapshot=[{
                        "document_id": str(doc_a_id),
                        "version_id": str(version_a_id),
                        "page_number": 1,
                    }],
                ),
                MemoryItem(
                    topic_id=topic_b_id,
                    memory_type="research_conclusion",
                    status="confirmed",
                    content="Conclusion owned by another topic.",
                    evidence_snapshot=[{
                        "document_id": str(doc_b_id),
                        "version_id": str(version_b_id),
                        "page_number": 1,
                    }],
                    confirmed_at=func.now(),
                ),
            ])
            session.add_all([
                TopicDocument(topic_id=topic_a_id, document_id=doc_a_id),
                TopicDocument(topic_id=topic_a_id, document_id=shared_id),
                TopicDocument(topic_id=topic_b_id, document_id=doc_b_id),
                TopicDocument(topic_id=topic_b_id, document_id=shared_id),
            ])
            session.commit()

        with Session(engine) as session:
            topic_scope = resolve_scope(session, topic_a_id, "topic")
            assert {item.document_id for item in topic_scope.documents} == {doc_a_id, shared_id}
            assert topic_scope.index_generation_id == initial_generation_id
            assert ResearchScope.from_snapshot(topic_scope.to_snapshot(), topic_scope.scope_hash) == topic_scope

            listed = list_scope_documents(session, topic_scope)
            assert {item.document_id for item in listed} == {doc_a_id, shared_id}
            pdf_info = scoped_pdf_info(session, topic_scope, doc_a_id)
            assert pdf_info.version_id == version_a_id
            assert pdf_info.storage_key == version_a_storage_key
            page = read_scope_page(session, topic_scope, doc_a_id, 1)
            assert page.text == page_text
            assert len(page.content_units) == 2
            assert page.content_units[0].quote_eligible is True
            assert page.content_units[1].is_derived is True
            with pytest.raises(ScopeError, match="scope_page_unavailable"):
                read_scope_page(session, topic_scope, doc_a_id, 2)
            with pytest.raises(ScopeForbidden, match="evidence_page_outside_scope"):
                validate_scope_reference(
                    session,
                    topic_scope,
                    document_id=doc_a_id,
                    version_id=version_a_id,
                    index_generation_id=topic_scope.index_generation_id,
                    page_number=2,
                )
            original_reference = validate_scope_reference(
                session,
                topic_scope,
                document_id=doc_a_id,
                version_id=version_a_id,
                index_generation_id=topic_scope.index_generation_id,
                page_number=1,
                content_unit_id=original_unit_id,
                excerpt="Original excerpt",
            )
            assert original_reference.quote_eligible is True
            assert original_reference.content_sha256 == hashlib.sha256(original_text.encode()).hexdigest()
            with pytest.raises(ScopeForbidden, match="evidence_excerpt_not_verifiable"):
                validate_scope_reference(
                    session,
                    topic_scope,
                    document_id=doc_a_id,
                    version_id=version_a_id,
                    index_generation_id=topic_scope.index_generation_id,
                    page_number=1,
                    content_unit_id=derived_unit_id,
                    excerpt="OCR note",
                )
            with pytest.raises(ScopeForbidden, match="evidence_document_outside_scope"):
                read_scope_page(session, topic_scope, doc_b_id, 1)

            memories = list_scope_memories(session, topic_scope)
            assert {item.content for item in memories} == {"Confirmed topic conclusion."}
            assert all(item.is_evidence is False for item in memories)
            expanded_context = list_scope_memories(session, topic_scope, include_process_summaries=True)
            assert {item.content for item in expanded_context} == {
                "Confirmed topic conclusion.", "Topic-only process context.",
            }
            selected_scope = resolve_scope(session, topic_a_id, "selected", [shared_id])
            assert [item.document_id for item in selected_scope.documents] == [shared_id]
            selected_memories = list_scope_memories(session, selected_scope, include_process_summaries=True)
            assert selected_memories == ()
            with pytest.raises(ScopeForbidden, match="selected_document_outside_topic"):
                resolve_scope(session, topic_a_id, "selected", [doc_b_id])
            with pytest.raises(ScopeError, match="scope_selection_required"):
                resolve_scope(session, topic_a_id, "selected", [])
            with pytest.raises(ScopeError, match="scope_selection_not_allowed"):
                resolve_scope(session, topic_a_id, "topic", [doc_a_id])

            library_scope = resolve_scope(session, topic_a_id, "library")
            assert {item.document_id for item in library_scope.documents} == {
                doc_a_id, doc_b_id, shared_id, library_id,
            }
            with pytest.raises(ScopeError, match="scope_selection_not_allowed"):
                resolve_scope(session, topic_a_id, "library", [doc_a_id])

            with pytest.raises(ScopeForbidden, match="evidence_document_outside_scope"):
                topic_scope.require_reference(doc_b_id, uuid4(), initial_generation_id)
            doc_a_scope = next(item for item in topic_scope.documents if item.document_id == doc_a_id)
            with pytest.raises(ScopeForbidden, match="evidence_generation_outside_scope"):
                topic_scope.require_reference(doc_a_id, doc_a_scope.version_id, uuid4())

            tampered = topic_scope.to_snapshot()
            tampered["documents"][0]["document_id"] = str(doc_b_id)
            with pytest.raises(ScopeError, match="scope_snapshot_hash_mismatch"):
                ResearchScope.from_snapshot(tampered, topic_scope.scope_hash)

            missing_generation_scope = ResearchScope.create(
                "topic",
                topic_a_id,
                [doc_a_scope],
                uuid4(),
            )
            with pytest.raises(ScopeStale, match="scope_index_unavailable"):
                validate_scope_current(session, missing_generation_scope)

        with Session(engine) as session:
            previous_generation = session.get(IndexGeneration, initial_generation_id)
            assert previous_generation is not None
            previous_generation.status = "retired"
            replacement_generation_id = uuid4()
            session.add(IndexGeneration(
                id=replacement_generation_id,
                provider=previous_generation.provider,
                model=previous_generation.model,
                dimensions=previous_generation.dimensions,
                config_hash=previous_generation.config_hash,
                status="active",
            ))
            session.commit()

        with Session(engine) as session:
            validate_scope_current(session, topic_scope)
            latest_scope = resolve_scope(session, topic_a_id, "topic")
            assert latest_scope.index_generation_id == replacement_generation_id

        with Session(engine) as session:
            topic = session.get(Topic, topic_a_id)
            assert topic is not None
            topic.archived_at = func.now()
            session.commit()
        with Session(engine) as session:
            with pytest.raises(ScopeStale, match="scope_topic_unavailable"):
                validate_scope_current(session, topic_scope)
            topic = session.get(Topic, topic_a_id)
            assert topic is not None
            topic.archived_at = None
            session.commit()

        replacement_version_id = uuid4()
        with Session(engine) as session:
            document = session.get(Document, doc_a_id)
            assert document is not None
            session.add(DocumentVersion(
                id=replacement_version_id,
                document_id=doc_a_id,
                version_number=2,
                filename="paper-a-v2.pdf",
                storage_key=uuid4().hex,
                sha256=uuid4().hex + uuid4().hex,
                status="ready",
                page_count=1,
                processing_config_hash="c" * 64,
            ))
            session.flush()
            document.active_version_id = replacement_version_id
            session.commit()

        with Session(engine) as session:
            with pytest.raises(ScopeStale, match="scope_document_changed"):
                validate_scope_current(session, latest_scope)
            fresh_scope = resolve_scope(session, topic_a_id, "topic")
            validate_scope_current(session, fresh_scope)
            fresh_memories = list_scope_memories(session, fresh_scope)
            assert all(item.memory_type != "research_conclusion" for item in fresh_memories)
            document = session.get(Document, shared_id)
            assert document is not None
            document.is_deleted = True
            session.commit()

        with Session(engine) as session:
            with pytest.raises(ScopeStale, match="scope_document_changed"):
                validate_scope_current(session, fresh_scope)
    finally:
        with Session(engine) as session:
            session.execute(delete(MemoryItem).where(MemoryItem.topic_id.in_([topic_a_id, topic_b_id])))
            session.execute(delete(TopicDocument).where(TopicDocument.topic_id.in_([topic_a_id, topic_b_id])))
            session.execute(delete(Topic).where(Topic.id.in_([topic_a_id, topic_b_id])))
            for document in session.scalars(
                select(Document).where(Document.id.in_([doc_a_id, doc_b_id, shared_id, library_id]))
            ).all():
                document.active_version_id = None
            session.flush()
            session.execute(delete(Document).where(Document.id.in_([doc_a_id, doc_b_id, shared_id, library_id])))
            if replacement_generation_id is not None:
                replacement = session.get(IndexGeneration, replacement_generation_id)
                if replacement is not None:
                    replacement.status = "retired"
            if initial_generation_id is not None:
                original = session.get(IndexGeneration, initial_generation_id)
                if original is not None:
                    if created_initial_generation:
                        session.delete(original)
                    else:
                        original.status = "active"
            session.flush()
            if replacement_generation_id is not None:
                replacement = session.get(IndexGeneration, replacement_generation_id)
                if replacement is not None:
                    session.delete(replacement)
            session.commit()
        engine.dispose()
