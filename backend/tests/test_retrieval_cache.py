import hashlib
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.documents import pipeline
from app.documents.models import (
    ContentUnit,
    ContentUnitEmbedding,
    Document,
    DocumentPage,
    DocumentVersion,
    EmbeddingCache,
    IndexGeneration,
)
from app.providers.siliconflow import ProviderResult
from app.topics.models import Topic, TopicDocument
from app.topics.scope import resolve_scope


class CachedEmbeddingClient:
    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.embed_calls = 0

    def embed(self, inputs: list[str], model: str | None = None, dimensions: int | None = None):
        self.embed_calls += 1
        return [self.vector for _ in inputs], ProviderResult({}, 1, 0, 1, 0)

    def rerank(self, query: str, documents: list[str]):
        return ([{"index": index, "relevance_score": 1.0 - index / 100} for index in range(len(documents))], ProviderResult({}, 1, 0, 1, 0))


def test_hybrid_retrieval_reads_active_generation_and_reuses_query_embedding(monkeypatch) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL retrieval test")

    engine = create_engine(database_url, pool_pre_ping=True)
    document_id = uuid4()
    topic_id = uuid4()
    version_id = uuid4()
    unit_id = uuid4()
    document_sha = hashlib.sha256(str(document_id).encode()).hexdigest()
    version_sha = hashlib.sha256(str(version_id).encode()).hexdigest()
    text_value = "pass@k formula: 1 - binom(n-c, k) / binom(n, k)"
    text_sha = hashlib.sha256(text_value.encode()).hexdigest()
    vector = [1.0] + [0.0] * 2559
    query = "pass@k formula"
    query_key = pipeline._embedding_cache_key(query, "query", "Qwen/Qwen3-Embedding-4B")
    content_key = pipeline._embedding_cache_key(text_value, "content", "Qwen/Qwen3-Embedding-4B", text_sha)
    client = CachedEmbeddingClient(vector)
    monkeypatch.setattr(pipeline, "get_engine", lambda: engine)
    replacement_generation_id = uuid4()
    cached_embeddings = pipeline._cached_embeddings
    cache_calls = 0

    def cache_then_rotate_generation(*args, **kwargs):
        nonlocal cache_calls
        result = cached_embeddings(*args, **kwargs)
        cache_calls += 1
        if cache_calls == 2:
            with Session(engine) as session:
                old = session.get(IndexGeneration, generation_id)
                assert old is not None and old.status == "active"
                old.status = "retired"
                session.add(IndexGeneration(
                    id=replacement_generation_id,
                    provider=old.provider,
                    model=old.model,
                    dimensions=old.dimensions,
                    config_hash=old.config_hash,
                    status="active",
                ))
                session.commit()
        return result

    monkeypatch.setattr(pipeline, "_cached_embeddings", cache_then_rotate_generation)

    try:
        with Session(engine) as session:
            session.execute(delete(EmbeddingCache).where(EmbeddingCache.cache_key.in_([query_key, content_key])))
            session.commit()
            generation = session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
            assert generation is not None
            document = Document(
                id=document_id,
                title="retrieval test",
                filename="retrieval-test.pdf",
                storage_key=uuid4().hex,
                sha256=document_sha,
                status="ready",
                page_count=1,
                is_deleted=False,
            )
            version = DocumentVersion(
                id=version_id,
                document_id=document_id,
                version_number=1,
                filename="retrieval-test.pdf",
                storage_key=uuid4().hex,
                sha256=version_sha,
                status="ready",
                page_count=1,
                processing_config_hash="0" * 64,
            )
            session.add(document)
            session.flush()
            session.add(version)
            session.flush()
            document.active_version_id = version_id
            session.add_all([
                Topic(id=topic_id, title="Retrieval scope"),
                TopicDocument(topic_id=topic_id, document_id=document_id),
            ])
            session.flush()
            page = DocumentPage(
                document_id=document_id,
                version_id=version_id,
                page_number=1,
                text=text_value,
                text_char_count=len(text_value),
            )
            session.add(page)
            session.flush()
            unit = ContentUnit(
                id=unit_id,
                document_id=document_id,
                version_id=version_id,
                page_number=1,
                ordinal=0,
                content_type="formula",
                text=text_value,
                text_sha256=text_sha,
                embedding=vector,
                embedding_model=generation.model,
                search_text=pipeline._search_text(text_value),
                token_estimate=16,
                extra={
                    "source": "pdf_text",
                    "is_derived": False,
                    "quote_eligible": True,
                    "bbox": [10.0, 20.0, 120.0, 36.0],
                },
            )
            session.add(unit)
            session.add(EmbeddingCache(
                cache_key=content_key,
                provider="siliconflow",
                model=generation.model,
                dimensions=2560,
                config_hash=generation.config_hash,
                embedding=vector,
            ))
            session.flush()
            session.add(ContentUnitEmbedding(
                content_unit_id=unit_id,
                generation_id=generation.id,
                cache_key=content_key,
                embedding=vector,
            ))
            session.commit()
            generation_id = generation.id

        with Session(engine) as session:
            scope = resolve_scope(session, topic_id, "topic")

        with pytest.raises(pipeline.JobFailure, match="research_scope_invalid"):
            pipeline.retrieve_evidence(uuid4(), query, client, scope=scope)
        assert client.embed_calls == 0

        for _ in range(2):
            hits = pipeline.retrieve_evidence(document_id, query, client, version_id, scope=scope)
            assert hits
            assert hits[0].page_number == 1
            assert hits[0].text == text_value
            assert hits[0].content_type == "formula"
            assert hits[0].source == "pdf_text"
            assert hits[0].is_derived is False
            assert hits[0].quote_eligible is True
            assert hits[0].bbox == [10.0, 20.0, 120.0, 36.0]
        assert client.embed_calls == 1
        with Session(engine) as session:
            assert session.scalar(select(ContentUnitEmbedding.generation_id).where(ContentUnitEmbedding.content_unit_id == unit_id)) == generation_id
    finally:
        with Session(engine) as session:
            session.execute(delete(TopicDocument).where(TopicDocument.topic_id == topic_id))
            session.execute(delete(Topic).where(Topic.id == topic_id))
            replacement = session.get(IndexGeneration, replacement_generation_id)
            if replacement is not None:
                replacement.status = "retired"
            original_generation = session.get(IndexGeneration, generation_id)
            if original_generation is not None:
                original_generation.status = "active"
            session.flush()
            session.execute(delete(Document).where(Document.id == document_id))
            if replacement is not None:
                session.delete(replacement)
            session.execute(delete(EmbeddingCache).where(EmbeddingCache.cache_key.in_([query_key, content_key])))
            session.commit()
        engine.dispose()
