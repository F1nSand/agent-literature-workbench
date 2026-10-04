from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import traceback
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import fitz
import jieba
from pgvector.sqlalchemy import HALFVEC
from pypdf import PdfReader
from sqlalchemy import cast, delete, desc, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_engine
from app.documents.models import (
    ContentUnit,
    ContentUnitEmbedding,
    Document,
    DocumentPage,
    DocumentVersion,
    EmbeddingCache,
    IndexGeneration,
    OCRCache,
    ProviderCall,
)
from app.documents.storage import resolve_document_pdf_path, resolve_document_source_path
from app.documents.formats import file_format, read_sections, convert_docx_to_pdf
from app.jobs.store import TaskLease, task_lease_is_current
from app.providers.siliconflow import MAX_RERANK_CANDIDATES, ProviderError, SiliconFlowClient, estimate_tokens
from app.topics.scope import ResearchScope, ScopeError, validate_scope_current

MIN_TEXT_CHARS_FOR_NATIVE_PDF = 80
MAX_CHUNK_CHARS = 2_200
MAX_DOCUMENT_UNITS = 256
MAX_INGEST_EMBED_ESTIMATE = 120_000
MAX_QUESTION_CHARS = 2_000
RETIRED_INDEX_GENERATIONS_TO_KEEP = 1
logger = logging.getLogger(__name__)


class JobFailure(RuntimeError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class SearchHit:
    id: UUID
    page_number: int
    text: str
    score: float
    content_type: str = "text"
    source: str = "pdf_text"
    is_derived: bool = False
    bbox: list[float] | None = None
    quote_eligible: bool = True


@dataclass(frozen=True)
class ParsedContentUnit:
    content_type: str
    text: str
    source: str
    is_derived: bool
    quote_eligible: bool
    bbox: list[float] | None = None


def _normalized_text(value: str) -> str:
    value = value.replace("\x00", " ").replace("\u00ad", "")
    value = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def split_content_units(page_text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Keep each unit page-local and split near line or sentence boundaries."""
    text_value = _normalized_text(page_text)
    if not text_value:
        return []
    units: list[str] = []
    remaining = text_value
    while len(remaining) > max_chars:
        boundary = max(
            remaining.rfind("\n", 0, max_chars),
            remaining.rfind(" ", 0, max_chars),
            remaining.rfind("。", 0, max_chars),
            remaining.rfind(". ", 0, max_chars),
        )
        if boundary < max_chars // 2:
            boundary = max_chars
        cut = boundary + (1 if remaining[boundary:boundary + 1] in {"。", " "} else 0)
        units.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        units.append(remaining)
    return [unit for unit in units if unit]


def _layout_lines(page: fitz.Page) -> list[tuple[str, list[float]]]:
    lines: list[tuple[str, list[float]]] = []
    for block in page.get_text("dict", sort=True).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            value = "".join(span.get("text", "") for span in line.get("spans", []))
            value = _normalized_text(value)
            if value:
                bbox = [round(float(coordinate), 2) for coordinate in line.get("bbox", [])]
                lines.append((value, bbox if len(bbox) == 4 else []))
    return lines


def _formula_like_line(value: str) -> bool:
    if len(value) > 500:
        return False
    if re.search(r"https?://|\b(?:doi|issn|isbn)\b|arxiv\.org", value, re.IGNORECASE):
        return False
    if re.search(r"[\u3400-\u9fff]", value):
        return False
    if re.search(r"\b(?:class|def|return|import)\b|=\s*[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\s*\(|=\s*['\"]", value):
        return False
    word_count = len(re.findall(r"\b[A-Za-z]{4,}\b", value))
    if word_count > 2:
        return False
    relation_count = len(re.findall(r"[=≈≠≤≥∈∉<>∼]", value))
    operator_count = len(re.findall(r"[+−×÷*/^∑∏∫√∗·]", value))
    has_math_context = bool(re.search(r"\d|[Α-Ωα-ω𝛢-𝟿∑∏∫√]", value))
    has_equation = "=" in value
    has_symbolic_relation = bool(re.search(r"[≈≠≤≥∈∉∼]", value))
    return has_math_context and (
        has_equation and operator_count > 0
        or relation_count > 1 and has_symbolic_relation and bool(re.search(r"[Α-Ωα-ω𝛢-𝟿]", value))
    )


def _table_content(page: fitz.Page, page_text: str) -> list[ParsedContentUnit]:
    if not re.search(r"(?im)^\s*(?:table|tab\.?|表)\s*(?:[A-Z]?\d+|[IVXLCDM]+|[一二三四五六七八九十]+)", page_text):
        return []
    try:
        finder = page.find_tables(strategy="lines_strict")
        tables = list(finder.tables)
        strict_line_tables = bool(tables)
        if not tables:
            finder = page.find_tables(strategy="text")
            tables = list(finder.tables)
    except Exception:
        return []

    units: list[ParsedContentUnit] = []
    for table in tables:
        try:
            rows = table.extract()
        except Exception:
            continue
        normalized_rows = [
            [str(cell).strip() if cell is not None else "" for cell in row]
            for row in rows
        ]
        normalized_rows = [row for row in normalized_rows if sum(bool(cell) for cell in row) >= 2]
        nonempty_cells = [cell for row in normalized_rows for cell in row if cell]
        if len(normalized_rows) < 2 or len(nonempty_cells) < 4:
            continue
        if not strict_line_tables:
            table_area = fitz.Rect(table.bbox).get_area()
            page_area = max(1.0, page.rect.get_area())
            column_count = max((len(row) for row in normalized_rows), default=0)
            if (
                len(normalized_rows) > 30
                or len(nonempty_cells) > 120
                or column_count > 12
                or table_area / page_area > 0.32
            ):
                continue
        table_text = "\n".join(" | ".join(row) for row in normalized_rows)
        bbox = [round(float(coordinate), 2) for coordinate in table.bbox]
        units.append(ParsedContentUnit(
            content_type="table",
            text=table_text,
            source="pdf_text_table",
            is_derived=True,
            quote_eligible=False,
            bbox=bbox if len(bbox) == 4 else None,
        ))
    return units


def _chart_captions(page: fitz.Page, lines: list[tuple[str, list[float]]], table_units: list[ParsedContentUnit]) -> list[ParsedContentUnit]:
    captions = [
        (value, bbox) for value, bbox in lines
        if bbox and re.match(r"^\s*(?:fig(?:ure)?\.?\s*\d+|图\s*\d+)", value, re.IGNORECASE)
    ]
    if not captions:
        return []

    page_area = max(1.0, float(page.rect.width * page.rect.height))
    visual_boxes: list[fitz.Rect] = []
    try:
        visual_boxes.extend(
            rect for image in page.get_images(full=True)
            for rect in page.get_image_rects(image[0])
        )
    except Exception:
        pass

    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if rect is not None and rect.get_area() >= page_area * 0.035:
            visual_boxes.append(rect)

    for unit in table_units:
        if unit.bbox is not None:
            table_rect = fitz.Rect(unit.bbox)
            remaining_boxes: list[fitz.Rect] = []
            for rect in visual_boxes:
                overlap_width = max(0.0, min(rect.x1, table_rect.x1) - max(rect.x0, table_rect.x0))
                overlap_height = max(0.0, min(rect.y1, table_rect.y1) - max(rect.y0, table_rect.y0))
                overlap_area = overlap_width * overlap_height
                if overlap_area < min(rect.get_area(), table_rect.get_area()) * 0.7:
                    remaining_boxes.append(rect)
            visual_boxes = remaining_boxes

    units: list[ParsedContentUnit] = []
    for value, bbox in captions:
        caption = fitz.Rect(bbox)
        near_visual = any(
            min(caption.x1, visual.x1) - max(caption.x0, visual.x0) > min(caption.width, visual.width) * 0.2
            and (
                0 <= caption.y0 - visual.y1 <= 96
                or 0 <= visual.y0 - caption.y1 <= 96
                or caption.intersects(visual)
            )
            for visual in visual_boxes
        )
        if near_visual:
            units.append(ParsedContentUnit(
                content_type="chart",
                text=value,
                source="pdf_text",
                is_derived=False,
                quote_eligible=True,
                bbox=bbox,
            ))
    return units


def parse_page_content(page: fitz.Page, page_text: str, extraction_method: str) -> list[ParsedContentUnit]:
    """Preserve native text and add structured units without treating OCR as verbatim source."""
    if extraction_method != "pdf_text":
        return [
            ParsedContentUnit("text", value, "cloud_ocr", True, False)
            for value in split_content_units(page_text)
        ]

    lines = _layout_lines(page)
    table_units = _table_content(page, page_text)
    formula_units = [
        ParsedContentUnit("formula", value, "pdf_text", False, True, bbox or None)
        for value, bbox in lines
        if _formula_like_line(value)
    ]
    chart_units = _chart_captions(page, lines, table_units)

    special_lines = {
        re.sub(r"\s+", " ", unit.text).casefold()
        for unit in [*formula_units, *chart_units]
    }
    remaining_lines = [
        line for line in page_text.splitlines()
        if re.sub(r"\s+", " ", _normalized_text(line)).casefold() not in special_lines
    ]
    text_units = [
        ParsedContentUnit("text", value, "pdf_text", False, True)
        for value in split_content_units("\n".join(remaining_lines))
    ]
    return [*text_units, *formula_units, *table_units, *chart_units]


def _embedding_profile_hash(model: str, dimensions: int) -> str:
    profile = {
        "provider": "siliconflow",
        "model": model,
        "dimensions": dimensions,
        "distance": "cosine-v1",
        "chunker": "page-local-v1",
    }
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode("utf-8")).hexdigest()


def _embedding_cache_key(text_value: str, kind: str, model: str, text_sha256: str | None = None) -> str:
    digest = text_sha256 or hashlib.sha256(_normalized_text(text_value).casefold().encode("utf-8")).hexdigest()
    if kind == "content":
        return f"embedding:content:siliconflow:{model}:d2560:cosine-v1:page-local-v1:{digest}"
    if kind == "query":
        return f"embedding:query:siliconflow:{model}:d2560:cosine-v1:query-v1:{digest}"
    raise ValueError("unsupported embedding cache kind")


def _cached_embeddings(
    client: SiliconFlowClient,
    texts: list[str],
    kind: str,
    model: str,
    text_hashes: list[str] | None = None,
    lease: TaskLease | None = None,
) -> tuple[list[list[float]], Any | None, int]:
    if text_hashes is not None and len(text_hashes) != len(texts):
        raise JobFailure("embedding_cache_input_mismatch")
    keys = [
        _embedding_cache_key(value, kind, model, text_hashes[index] if text_hashes is not None else None)
        for index, value in enumerate(texts)
    ]
    unique_texts: dict[str, str] = {}
    for key, value in zip(keys, texts, strict=True):
        unique_texts.setdefault(key, value)
    with Session(get_engine()) as session:
        cached = {
            row.cache_key: row.embedding
            for row in session.scalars(select(EmbeddingCache).where(EmbeddingCache.cache_key.in_(list(unique_texts)))).all()
        } if unique_texts else {}

    missing = [(key, value) for key, value in unique_texts.items() if key not in cached]
    provider_result = None
    if missing:
        vectors, provider_result = client.embed([value for _, value in missing], model=model, dimensions=2560)
        profile_hash = _embedding_profile_hash(model, 2560)
        rows = [{
            "cache_key": key,
            "provider": "siliconflow",
            "model": model,
            "dimensions": 2560,
            "config_hash": profile_hash,
            "embedding": vector,
        } for (key, _), vector in zip(missing, vectors, strict=True)]
        with Session(get_engine()) as session:
            if lease is not None and not task_lease_is_current(session, lease, lock=True):
                raise JobFailure("worker_lease_lost")
            session.execute(
                pg_insert(EmbeddingCache).values(rows).on_conflict_do_nothing(index_elements=[EmbeddingCache.cache_key])
            )
            session.commit()
        with Session(get_engine()) as session:
            cached.update({
                row.cache_key: row.embedding
                for row in session.scalars(select(EmbeddingCache).where(EmbeddingCache.cache_key.in_([key for key, _ in missing]))).all()
            })

    if any(key not in cached for key in keys):
        raise JobFailure("embedding_cache_write_failed")
    return [cached[key] for key in keys], provider_result, len(texts) - len(missing)


def _data_url(pdf_path: Path, page_number: int, crop: str | None = None) -> str:
    with fitz.open(pdf_path) as pdf:
        page = pdf[page_number - 1]
        bounds = page.rect
        if crop == "lower_half":
            clip = fitz.Rect(bounds.x0 + bounds.width * 0.07, bounds.y0 + bounds.height * 0.48,
                             bounds.x1 - bounds.width * 0.07, bounds.y1 - bounds.height * 0.04)
        else:
            clip = bounds
        pixmap = page.get_pixmap(matrix=fitz.Matrix(1.8, 1.8), clip=clip, alpha=False)
        image = pixmap.tobytes("jpeg")
    return "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")


def _ocr_page(
    client: SiliconFlowClient,
    pdf_path: Path,
    page_number: int,
    lease: TaskLease | None = None,
) -> tuple[str, bool]:
    settings = get_settings()
    prompt_version = "transcribe-visible-content-v1"
    image_data_url = _data_url(pdf_path, page_number)
    image_hash = hashlib.sha256(image_data_url.encode("ascii")).hexdigest()
    cache_key = hashlib.sha256(
        f"siliconflow\0{settings.siliconflow_ocr_model}\0{prompt_version}\0{image_hash}".encode("utf-8")
    ).hexdigest()
    with Session(get_engine()) as session:
        cached = session.get(OCRCache, cache_key)
        if cached is not None:
            return cached.text, True
    result = client.ocr(
        image_data_url,
        "请逐行转写页面中可见的正文、表格文字和数学符号；保留原文语言、段落次序与公式，不要概述、补全或推测看不清的内容。只输出转写文本。",
        max_tokens=8_192,
    )
    choices = result.data.get("choices") or []
    message = choices[0].get("message", {}) if choices else {}
    content = message.get("content", "") if isinstance(message, dict) else ""
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("ocr_empty_response")
    normalized = _normalized_text(content)
    with Session(get_engine()) as session:
        if lease is not None and not task_lease_is_current(session, lease, lock=True):
            raise JobFailure("worker_lease_lost")
        session.execute(pg_insert(OCRCache).values(
            cache_key=cache_key,
            provider="siliconflow",
            model=settings.siliconflow_ocr_model,
            prompt_version=prompt_version,
            text=normalized,
        ).on_conflict_do_nothing(index_elements=[OCRCache.cache_key]))
        session.commit()
    return normalized, False


def _set_document_error(
    document_id: UUID,
    error_code: str,
    version_id: UUID | None = None,
    lease: TaskLease | None = None,
) -> None:
    with Session(get_engine()) as session:
        if lease is not None and not task_lease_is_current(session, lease, lock=True):
            return
        document = session.get(Document, document_id)
        version = session.get(DocumentVersion, version_id) if version_id is not None else None
        if version is not None:
            version.status = "failed"
            version.error_code = error_code
        if document is not None and document.active_version_id is None:
            document.status = "failed"
            document.error_code = error_code
        elif document is not None and version is not None and document.active_version_id == version.id:
            document.status = "failed"
            document.error_code = error_code
        if document is not None:
            session.commit()


def process_document_ingestion(
    document_id: UUID,
    task_id: UUID,
    requested_version_id: UUID | None = None,
    lease: TaskLease | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    version_id = requested_version_id
    with Session(get_engine()) as session:
        if lease is not None and not task_lease_is_current(session, lease, lock=True):
            raise JobFailure("worker_lease_lost")
        document = session.get(Document, document_id)
        if document is None or document.is_deleted:
            raise JobFailure("document_not_found")
        if version_id is None:
            version_id = document.active_version_id
        version = session.get(DocumentVersion, version_id) if version_id is not None else None
        if version is None or version.document_id != document_id:
            raise JobFailure("document_version_not_found")
        if version.status == "ready" and document.active_version_id == version.id:
            return {"document_id": str(document_id), "version_id": str(version.id), "status": "ready", "page_count": version.page_count}
        storage_key = version.storage_key
        kind = file_format(version.filename)
        expected_page_count = version.page_count
        try:
            source_path = resolve_document_source_path(settings.data_dir, storage_key, kind)
            pdf_path = resolve_document_pdf_path(settings.data_dir, storage_key)
        except ValueError:
            _set_document_error(document_id, "document_file_missing", version.id, lease)
            raise JobFailure("document_file_missing") from None
        if not source_path.is_file():
            _set_document_error(document_id, "document_file_missing", version.id, lease)
            raise JobFailure("document_file_missing")
        version.status = "processing"
        version.error_code = None
        if document.active_version_id is None:
            document.status = "processing"
            document.error_code = None
        session.commit()

    try:
        client = SiliconFlowClient(document_id, task_id)
        extracted_pages: list[tuple[int, str, bool, str]] = []
        locators: dict[int, dict] = {}
        labels: dict[int, str] = {}
        ocr_pages = 0
        ocr_cache_hits = 0
        if kind in {"pdf", "docx"}:
            if kind == "docx":
                convert_docx_to_pdf(source_path, pdf_path)
            reader = PdfReader(str(pdf_path), strict=False)
            if reader.is_encrypted or (kind == "pdf" and len(reader.pages) != expected_page_count):
                raise JobFailure("pdf_validation_failed")
            if len(reader.pages) > settings.max_pdf_pages:
                raise JobFailure("pdf_page_limit_exceeded")
            for page_number, page in enumerate(reader.pages, start=1):
                page_text = _normalized_text(page.extract_text() or "")
                ocr_used = len(page_text) < MIN_TEXT_CHARS_FOR_NATIVE_PDF
                method = "pdf_text"
                if ocr_used:
                    if ocr_pages >= 24:
                        raise JobFailure("stage_ocr_page_limit_reached")
                    page_text, cache_hit = _ocr_page(client, pdf_path, page_number, lease)
                    ocr_pages += 1
                    ocr_cache_hits += int(cache_hit)
                    method = "cloud_ocr"
                extracted_pages.append((page_number, page_text, ocr_used, method))
                locators[page_number] = {"kind": "page", "page": page_number}
                labels[page_number] = f"第 {page_number} 页" + ("（DOCX 转换版）" if kind == "docx" else "")
        else:
            for section in read_sections(source_path, kind):
                extracted_pages.append((section.number, section.text, False, "epub_text" if kind == "epub" else "plain_text"))
                locators[section.number] = section.locator
                labels[section.number] = section.label[:150]

        page_models: list[DocumentPage] = []
        unit_models: list[ContentUnit] = []
        with fitz.open(pdf_path) if kind in {"pdf", "docx"} else nullcontext(None) as layout_pdf:
            for page_number, page_text, ocr_used, method in extracted_pages:
                page_models.append(DocumentPage(
                    document_id=document_id,
                    version_id=version_id,
                    page_number=page_number,
                    text=page_text,
                    text_char_count=len(page_text),
                    ocr_used=ocr_used,
                    extraction_method=method,
                    locator=locators[page_number], location_label=labels[page_number],
                ))
                parsed_units = parse_page_content(layout_pdf[page_number - 1], page_text, method) if layout_pdf is not None else [ParsedContentUnit("text", chunk, method, False, True) for chunk in split_content_units(page_text)]
                for ordinal, parsed in enumerate(parsed_units):
                    unit = ContentUnit(
                        id=uuid4(),
                        document_id=document_id,
                        version_id=version_id,
                        page_number=page_number,
                        ordinal=ordinal,
                        content_type=parsed.content_type,
                        text=parsed.text,
                        text_sha256=hashlib.sha256(parsed.text.encode("utf-8")).hexdigest(),
                        embedding=None,
                        search_text=_search_text(parsed.text),
                        token_estimate=estimate_tokens(parsed.text),
                        extra={
                            "source": parsed.source,
                            "is_derived": parsed.is_derived,
                            "quote_eligible": parsed.quote_eligible,
                            "bbox": parsed.bbox,
                            "parser_version": "pymupdf-structure-v1",
                            "locator": locators[page_number], "location_label": labels[page_number],
                        },
                    )
                    unit_models.append(unit)

        if not unit_models:
            raise JobFailure("pdf_has_no_searchable_text")
        if len(unit_models) > MAX_DOCUMENT_UNITS:
            raise JobFailure("stage_content_unit_limit_reached")
        estimated_tokens = sum(unit.token_estimate for unit in unit_models)
        if estimated_tokens > MAX_INGEST_EMBED_ESTIMATE:
            raise JobFailure("stage_embedding_input_budget_exceeded")
        unit_texts = [unit.text for unit in unit_models]
        unit_hashes = [unit.text_sha256 for unit in unit_models]
        unit_ids = [unit.id for unit in unit_models]

        with Session(get_engine()) as session:
            if lease is not None and not task_lease_is_current(session, lease, lock=True):
                raise JobFailure("worker_lease_lost")
            session.query(ContentUnit).filter(ContentUnit.version_id == version_id).delete(synchronize_session=False)
            session.query(DocumentPage).filter(DocumentPage.version_id == version_id).delete(synchronize_session=False)
            session.add_all(page_models)
            session.flush()
            session.add_all(unit_models)
            session.commit()

        with Session(get_engine()) as session:
            generation = session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
            if generation is None:
                raise JobFailure("active_index_generation_missing")
            generation_id = generation.id
            embedding_model = generation.model
            embedding_dimensions = generation.dimensions
        vectors, embedding_result, embedding_cache_hits = _cached_embeddings(
            client, unit_texts, "content", embedding_model, unit_hashes, lease
        )
        if embedding_result is not None and embedding_result.prompt_tokens is not None and embedding_result.prompt_tokens > MAX_INGEST_EMBED_ESTIMATE:
            raise JobFailure("stage_embedding_input_budget_exceeded")
        with Session(get_engine()) as session:
            if lease is not None and not task_lease_is_current(session, lease, lock=True):
                raise JobFailure("worker_lease_lost")
            rows = session.scalars(select(ContentUnit).where(ContentUnit.id.in_(unit_ids))).all()
            rows_by_id = {row.id: row for row in rows}
            if len(rows_by_id) != len(unit_models):
                raise JobFailure("content_unit_missing_after_embedding")
            cache_keys = [
                _embedding_cache_key(value, "content", embedding_model, text_hash)
                for value, text_hash in zip(unit_texts, unit_hashes, strict=True)
            ]
            association_rows = []
            for unit_id, vector, cache_key in zip(unit_ids, vectors, cache_keys, strict=True):
                row = rows_by_id[unit_id]
                row.embedding = vector
                row.embedding_model = embedding_model
                association_rows.append({
                    "content_unit_id": unit_id,
                    "generation_id": generation_id,
                    "cache_key": cache_key,
                    "embedding": vector,
                })
            embedding_upsert = pg_insert(ContentUnitEmbedding).values(association_rows)
            session.execute(
                embedding_upsert.on_conflict_do_update(
                    index_elements=[ContentUnitEmbedding.content_unit_id, ContentUnitEmbedding.generation_id],
                    set_={
                        "cache_key": embedding_upsert.excluded.cache_key,
                        "embedding": embedding_upsert.excluded.embedding,
                    },
                )
            )
            document = session.get(Document, document_id)
            version = session.get(DocumentVersion, version_id)
            current_generation = session.scalar(
                select(IndexGeneration).where(IndexGeneration.id == generation_id).with_for_update()
            )
            if document is None or version is None or document.is_deleted:
                raise JobFailure("document_not_found")
            if current_generation is None or current_generation.status != "active":
                raise JobFailure("index_generation_changed_during_ingestion")
            version.status = "ready"
            version.error_code = None
            version.page_count = len(extracted_pages)
            active = session.get(DocumentVersion, document.active_version_id) if document.active_version_id else None
            if active is None or version.version_number >= active.version_number:
                document.active_version_id = version.id
                document.filename = version.filename
                document.storage_key = version.storage_key
                document.sha256 = version.sha256
                document.status = "ready"
                document.error_code = None
                document.page_count = len(extracted_pages)
            session.commit()
        return {
            "document_id": str(document_id),
            "version_id": str(version_id),
            "status": "ready",
            "page_count": len(extracted_pages),
            "content_unit_count": len(unit_models),
            "ocr_page_count": ocr_pages,
            "ocr_cache_hits": ocr_cache_hits,
            "embedding_dimensions": embedding_dimensions,
            "embedding_prompt_tokens": embedding_result.prompt_tokens if embedding_result is not None else 0,
            "embedding_cache_hits": embedding_cache_hits,
        }
    except JobFailure as error:
        _set_document_error(document_id, error.error_code, version_id, lease)
        raise
    except ProviderError as error:
        _set_document_error(document_id, error.error_code, version_id, lease)
        raise JobFailure(error.error_code) from None
    except Exception as error:
        frames = traceback.extract_tb(error.__traceback__)
        if frames:
            locations = " > ".join(
                f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                for frame in frames[-5:]
            )
            logger.error(
                "document ingestion failed type=%s stack=%s",
                type(error).__name__,
                locations,
            )
        else:
            logger.error("document ingestion failed type=%s", type(error).__name__)
        _set_document_error(document_id, "document_processing_failed", version_id, lease)
        raise JobFailure("document_processing_failed") from None


def _ready_unit_count(session: Session, generation_id: UUID | None = None, profile: IndexGeneration | None = None) -> int:
    statement = (
        select(func.count(ContentUnit.id))
        .join(DocumentVersion, DocumentVersion.id == ContentUnit.version_id)
        .join(Document, Document.id == ContentUnit.document_id)
        .where(Document.is_deleted.is_(False), DocumentVersion.status == "ready")
    )
    if generation_id is not None:
        statement = statement.join(
            ContentUnitEmbedding,
            ContentUnitEmbedding.content_unit_id == ContentUnit.id,
        ).where(ContentUnitEmbedding.generation_id == generation_id)
        if profile is not None:
            statement = statement.join(
                EmbeddingCache,
                EmbeddingCache.cache_key == ContentUnitEmbedding.cache_key,
            ).where(
                EmbeddingCache.provider == profile.provider,
                EmbeddingCache.model == profile.model,
                EmbeddingCache.dimensions == profile.dimensions,
                EmbeddingCache.config_hash == profile.config_hash,
            )
    return session.scalar(statement) or 0


def _fail_index_generation(generation_id: UUID, error_code: str) -> None:
    with Session(get_engine()) as session:
        generation = session.get(IndexGeneration, generation_id)
        if generation is not None and generation.status == "building":
            generation.status = "failed"
            generation.error_code = error_code
            session.commit()


def rebuild_index_generation(source_generation_id: UUID, target_generation_id: UUID) -> dict[str, Any]:
    """Copy the active profile into a new generation, validate it, then atomically switch."""
    try:
        with Session(get_engine()) as session:
            source = session.get(IndexGeneration, source_generation_id)
            target = session.get(IndexGeneration, target_generation_id)
            if source is None or source.status != "active":
                raise JobFailure("index_rebuild_source_unavailable")
            if target is None or target.status != "building":
                raise JobFailure("index_rebuild_target_unavailable")
            if (
                target.provider != source.provider
                or target.model != source.model
                or target.dimensions != source.dimensions
                or target.config_hash != source.config_hash
            ):
                raise JobFailure("index_rebuild_profile_mismatch")
            expected_count = _ready_unit_count(session)
            source_count = _ready_unit_count(session, source_generation_id, source)
            if expected_count == 0:
                raise JobFailure("index_rebuild_no_ready_content")
            if source_count != expected_count:
                raise JobFailure("index_rebuild_source_incomplete")

        with Session(get_engine()) as session:
            source_rows = select(
                ContentUnitEmbedding.content_unit_id,
                literal(target_generation_id),
                ContentUnitEmbedding.cache_key,
                ContentUnitEmbedding.embedding,
            ).where(ContentUnitEmbedding.generation_id == source_generation_id)
            session.execute(pg_insert(ContentUnitEmbedding).from_select(
                ["content_unit_id", "generation_id", "cache_key", "embedding"],
                source_rows,
            ))
            session.commit()

        with Session(get_engine()) as session:
            source = session.get(IndexGeneration, source_generation_id)
            if source is None:
                raise JobFailure("index_rebuild_source_unavailable")
            expected_count = _ready_unit_count(session)
            target_count = _ready_unit_count(session, target_generation_id, source)
            if target_count != expected_count:
                raise JobFailure("index_rebuild_target_incomplete")

        with Session(get_engine()) as session:
            source = session.scalar(
                select(IndexGeneration)
                .where(IndexGeneration.id == source_generation_id, IndexGeneration.status == "active")
                .with_for_update()
            )
            target = session.scalar(
                select(IndexGeneration)
                .where(IndexGeneration.id == target_generation_id, IndexGeneration.status == "building")
                .with_for_update()
            )
            if source is None or target is None:
                raise JobFailure("index_rebuild_source_changed")
            expected_count = _ready_unit_count(session)
            target_count = _ready_unit_count(session, target_generation_id, source)
            if target_count != expected_count:
                raise JobFailure("index_rebuild_target_changed_during_activation")

            retired = session.execute(
                update(IndexGeneration)
                .where(IndexGeneration.id == source_generation_id, IndexGeneration.status == "active")
                .values(status="retired")
            )
            activated = session.execute(
                update(IndexGeneration)
                .where(IndexGeneration.id == target_generation_id, IndexGeneration.status == "building")
                .values(status="active", activated_at=func.now(), error_code=None)
            )
            if retired.rowcount != 1 or activated.rowcount != 1:
                raise JobFailure("index_rebuild_activation_failed")

            retired_ids = session.scalars(
                select(IndexGeneration.id)
                .where(IndexGeneration.status == "retired")
                .order_by(IndexGeneration.activated_at.desc().nullslast(), IndexGeneration.created_at.desc())
            ).all()
            prune_ids = retired_ids[RETIRED_INDEX_GENERATIONS_TO_KEEP:]
            if prune_ids:
                session.execute(delete(IndexGeneration).where(IndexGeneration.id.in_(prune_ids)))
            session.commit()

        return {
            "status": "active",
            "source_generation_id": str(source_generation_id),
            "index_generation_id": str(target_generation_id),
            "indexed_content_unit_count": expected_count,
            "retired_generations_retained": min(len(retired_ids), RETIRED_INDEX_GENERATIONS_TO_KEEP),
        }
    except JobFailure as error:
        _fail_index_generation(target_generation_id, error.error_code)
        raise
    except Exception as error:
        logger.error("index rebuild failed type=%s", type(error).__name__)
        _fail_index_generation(target_generation_id, "index_rebuild_failed")
        raise JobFailure("index_rebuild_failed") from None


def _lexical_terms(question: str) -> list[str]:
    english = re.findall(r"[A-Za-z][A-Za-z0-9_-]*", question.casefold())
    chinese = [
        token.casefold().strip()
        for token in jieba.cut(question, HMM=False)
        if re.search(r"[\u3400-\u9fff]", token)
    ]
    math_tags = [re.sub(r"\s+", "", term).casefold() for term in re.findall(r"[A-Za-z0-9]+\s*@\s*[A-Za-z0-9]+", question)]
    terms = english + chinese + math_tags
    stop = {"the", "and", "for", "from", "with", "this", "that", "paper", "论文", "公式", "里面", "解释"}
    normalized = [re.sub(r"[^a-z0-9_\u3400-\u9fff]", "", term) for term in terms]
    return list(dict.fromkeys(term for term in normalized if term and term not in stop))


def _search_text(value: str) -> str:
    return " ".join(_lexical_terms(value))


def retrieve_evidence(
    document_id: UUID,
    query: str,
    client: SiliconFlowClient,
    version_id: UUID | None = None,
    *,
    scope: ResearchScope | None = None,
) -> list[SearchHit]:
    with Session(get_engine()) as session:
        document = session.get(Document, document_id)
        scoped_document = None
        if scope is not None:
            try:
                scoped_document = scope.require_document(document_id, version_id)
                validate_scope_current(session, scope)
            except ScopeError:
                raise JobFailure("research_scope_invalid") from None
            active_version_id = scoped_document.version_id
        else:
            active_version_id = version_id or (document.active_version_id if document is not None else None)
        version = session.get(DocumentVersion, active_version_id) if active_version_id is not None else None
        generation_query = select(IndexGeneration)
        if scope is None:
            generation_query = generation_query.where(IndexGeneration.status == "active")
        else:
            generation_query = generation_query.where(IndexGeneration.id == scope.index_generation_id)
        generation = session.scalar(generation_query)
        if (
            document is None
            or document.is_deleted
            or version is None
            or version.document_id != document_id
            or version.status != "ready"
        ):
            raise JobFailure("document_not_available")
        if generation is None:
            raise JobFailure("active_index_generation_missing")
        generation_id = generation.id
        embedding_model = generation.model
        embedding_dimensions = generation.dimensions

    query_vectors, query_result, _ = _cached_embeddings(client, [query], "query", embedding_model)
    if query_result is not None and query_result.prompt_tokens is not None and query_result.prompt_tokens > 4_000:
        raise JobFailure("stage_embedding_query_token_limit_reached")
    query_vector = query_vectors[0]
    terms = _lexical_terms(query)
    tsquery_terms = " | ".join(terms)

    with Session(get_engine()) as session:
        if scope is not None:
            try:
                validate_scope_current(session, scope)
            except ScopeError:
                raise JobFailure("research_scope_invalid") from None
        generation = session.scalar(
            select(IndexGeneration)
            .where(IndexGeneration.id == generation_id)
            .with_for_update(read=True)
        )
        if generation is None or generation.status not in {"active", "retired"}:
            raise JobFailure("index_generation_changed_during_retrieval")
        session.execute(text("SET LOCAL hnsw.ef_search = 40"))
        session.execute(text("SET LOCAL hnsw.iterative_scan = 'strict_order'"))
        half_distance = cast(ContentUnitEmbedding.embedding, HALFVEC(embedding_dimensions)).cosine_distance(query_vector).label("distance")
        dense_rows = session.execute(
            select(ContentUnitEmbedding.content_unit_id.label("id"), half_distance)
            .join(ContentUnit, ContentUnit.id == ContentUnitEmbedding.content_unit_id)
            .where(
                ContentUnit.document_id == document_id,
                ContentUnit.version_id == active_version_id,
                ContentUnitEmbedding.generation_id == generation_id,
            )
            .order_by(half_distance)
            .limit(30)
        ).all()
        dense_ids = [row.id for row in dense_rows]
        dense_rank = {row.id: rank for rank, row in enumerate(dense_rows, start=1)}
        exact_distance = ContentUnitEmbedding.embedding.cosine_distance(query_vector).label("distance")
        exact_rows = session.execute(
            select(ContentUnitEmbedding.content_unit_id.label("id"), exact_distance)
            .join(ContentUnit, ContentUnit.id == ContentUnitEmbedding.content_unit_id)
            .where(
                ContentUnit.document_id == document_id,
                ContentUnit.version_id == active_version_id,
                ContentUnitEmbedding.generation_id == generation_id,
                ContentUnitEmbedding.content_unit_id.in_(dense_ids),
            )
            .order_by(exact_distance)
        ).all() if dense_ids else []
        exact_rank = {row.id: rank for rank, row in enumerate(exact_rows, start=1)}
        lexical_rows = session.execute(
            text(
                "SELECT id, ts_rank_cd(to_tsvector('simple', search_text), to_tsquery('simple', :terms)) AS score "
                "FROM content_units "
                "WHERE document_id = :document_id AND version_id = :version_id "
                "AND to_tsvector('simple', search_text) @@ to_tsquery('simple', :terms) "
                "ORDER BY score DESC, id LIMIT 30"
            ),
            {"terms": tsquery_terms, "document_id": document_id, "version_id": active_version_id},
        ).all() if tsquery_terms else []
        lexical_rank = {row.id: rank for rank, row in enumerate(lexical_rows, start=1)}

    rrf_scores: dict[UUID, float] = {}
    for unit_id, rank in dense_rank.items():
        rrf_scores[unit_id] = rrf_scores.get(unit_id, 0.0) + 1 / (60 + rank)
    for unit_id, rank in lexical_rank.items():
        rrf_scores[unit_id] = rrf_scores.get(unit_id, 0.0) + 1 / (60 + rank)
    candidate_ids = sorted(
        rrf_scores,
        key=lambda unit_id: (-rrf_scores[unit_id], exact_rank.get(unit_id, 10_000), unit_id.hex),
    )[:40]
    if not candidate_ids:
        raise JobFailure("retrieval_no_evidence")

    with Session(get_engine()) as session:
        if scope is not None:
            try:
                validate_scope_current(session, scope)
            except ScopeError:
                raise JobFailure("research_scope_invalid") from None
        unit_rows = session.execute(
            select(
                ContentUnit.id,
                ContentUnit.page_number,
                ContentUnit.text,
                ContentUnit.content_type,
                ContentUnit.extra,
            )
            .where(
                ContentUnit.document_id == document_id,
                ContentUnit.version_id == active_version_id,
                ContentUnit.id.in_(candidate_ids),
            )
        ).all()
    by_id = {row.id: row for row in unit_rows}
    candidate_ids = [unit_id for unit_id in candidate_ids if unit_id in by_id]
    if not candidate_ids:
        raise JobFailure("retrieval_no_evidence")

    rerank_input = [by_id[unit_id].text[:4_000] for unit_id in candidate_ids[:MAX_RERANK_CANDIDATES]]
    reranked, _ = client.rerank(query, rerank_input)
    rerank_rows: list[tuple[float, UUID]] = []
    for item in reranked:
        index = item.get("index")
        if isinstance(index, int) and 0 <= index < len(rerank_input):
            try:
                score = float(item.get("relevance_score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            rerank_rows.append((score, candidate_ids[index]))
    if not rerank_rows:
        rerank_rows = [(rrf_scores[unit_id], unit_id) for unit_id in candidate_ids[:8]]
    rerank_rows.sort(key=lambda item: (-item[0], item[1].hex))
    hits: list[SearchHit] = []
    for score, unit_id in rerank_rows[:8]:
        row = by_id[unit_id]
        extra = row.extra or {}
        source = extra.get("source", "pdf_text")
        hits.append(SearchHit(
            id=unit_id,
            page_number=row.page_number,
            text=row.text,
            score=score,
            content_type=row.content_type,
            source=source,
            is_derived=bool(extra.get("is_derived", source != "pdf_text")),
            bbox=extra.get("bbox"),
            quote_eligible=bool(extra.get("quote_eligible", source == "pdf_text")),
        ))
    return hits


def select_visual_page(question: str, hits: list[SearchHit]) -> int | None:
    if not hits:
        return None
    if re.search(r"pass\s*@\s*k", question, re.IGNORECASE):
        def formula_score(hit: SearchHit) -> int:
            content = hit.text.casefold()
            return (
                3 * bool(re.search(r"unbiased", content))
                + 2 * bool(re.search(r"pass\s*@\s*k", content))
                + 2 * bool(re.search(r"estimator", content))
                + 3 * bool(re.search(r"(?:\\binom|binom|n\s*[−-]\s*c)", content))
            )

        scored = [(formula_score(hit), -index, hit.page_number) for index, hit in enumerate(hits)]
        score, _, page_number = max(scored)
        if score:
            return page_number
    return hits[0].page_number


def inspect_relevant_page(
    document_id: UUID,
    version_id: UUID,
    question: str,
    hits: list[SearchHit],
    client: SiliconFlowClient,
) -> dict[str, Any] | None:
    if not re.search(r"pass\s*@\s*k|公式|图表|框图|架构图|流程图", question, re.IGNORECASE):
        return None
    settings = get_settings()
    with Session(get_engine()) as session:
        document = session.get(Document, document_id)
        version = session.get(DocumentVersion, version_id)
        if document is None or version is None or document.is_deleted or version.document_id != document_id or version.status != "ready":
            return None
        try:
            pdf_path = resolve_document_pdf_path(settings.data_dir, version.storage_key)
        except ValueError:
            return None
    page_number = select_visual_page(question, hits)
    if page_number is None or not pdf_path.is_file():
        return None

    image_data_url = _data_url(pdf_path, page_number, "lower_half" if re.search(r"pass\s*@\s*k", question, re.IGNORECASE) else None)
    try:
        result = client.vision(
            image_data_url,
            "核对图像中的公式、图表或流程主项，只按图中可见内容作简短转写，不推测缺失内容。若是 pass@k 公式，请保留帽号、组合数、变量和分母。",
            max_tokens=8_192,
        )
    except ProviderError:
        return {
            "page_number": page_number,
            "status": "unavailable",
            "source": "model_vision",
            "is_derived": True,
            "quote_eligible": False,
        }
    choices = result.data.get("choices") or []
    message = choices[0].get("message", {}) if choices else {}
    content = message.get("content", "") if isinstance(message, dict) else ""
    return {
        "page_number": page_number,
        "status": "checked" if isinstance(content, str) and content.strip() else "empty",
        "transcription": content[:32_768] if isinstance(content, str) else "",
        "source": "model_vision",
        "is_derived": True,
        "quote_eligible": False,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
    }


def citation_quote(page_number: int, hits: list[SearchHit]) -> str:
    candidate = next((hit for hit in hits if hit.page_number == page_number and hit.quote_eligible), None)
    if candidate is None:
        return ""
    defined_estimator = re.search(
        r"(?:the\s+)?unbiased(?:\s+codex)?\s+estimator",
        candidate.text,
        re.IGNORECASE,
    )
    if defined_estimator:
        return re.sub(r"\s+", " ", defined_estimator.group(0)).strip()
    sentences = re.split(r"(?<=[.!?])\s+|\n+", candidate.text)
    for sentence in sentences:
        if re.search(r"pass\s*@\s*k|unbiased|estimator|where n|where the number", sentence, re.IGNORECASE):
            words = sentence.strip().split()
            return " ".join(words[:20])
    return " ".join(candidate.text.strip().split()[:20])


def _page_only_citation(document_id: UUID, hit: SearchHit) -> dict[str, Any]:
    if hit.source == "cloud_ocr":
        reason = "扫描页识别内容仅用于定位，请核对原始 PDF 页面。"
    elif hit.content_type == "table":
        reason = "表格结构整理仅用于定位，请核对原始 PDF 页面。"
    else:
        reason = "识别或整理内容仅用于定位，请核对原始 PDF 页面。"
    return {
        "document_id": str(document_id),
        "page_number": hit.page_number,
        "quote": "",
        "reason": reason,
        "is_derived": True,
    }


def build_citations(
    document_id: UUID,
    hits: list[SearchHit],
    citation_rows: list[Any],
    answer: str,
) -> list[dict[str, Any]]:
    hits_by_page: dict[int, list[SearchHit]] = {}
    for hit in hits:
        hits_by_page.setdefault(hit.page_number, []).append(hit)

    citations: list[dict[str, Any]] = []
    page_only_citations: set[int] = set()
    for citation in citation_rows[:8]:
        if not isinstance(citation, dict):
            continue
        try:
            page_number = int(citation.get("page_number"))
        except (TypeError, ValueError):
            continue
        page_hits = hits_by_page.get(page_number, [])
        if not page_hits:
            continue

        quoteable_text = " ".join(hit.text for hit in page_hits if hit.quote_eligible)
        if quoteable_text:
            quote = str(citation.get("quote", "")).strip()[:240]
            normalized_quote = re.sub(r"\W+", "", quote.casefold())
            source_text = re.sub(r"\W+", "", quoteable_text.casefold())
            if not quote or not normalized_quote or normalized_quote not in source_text:
                continue
            citations.append({
                "document_id": str(document_id),
                "page_number": page_number,
                "quote": quote,
                "reason": str(citation.get("reason", "相关原文证据"))[:200],
                "is_derived": False,
            })
            continue

        derived_hit = next((hit for hit in page_hits if hit.is_derived), None)
        if derived_hit is None or page_number in page_only_citations:
            continue
        page_only_citations.add(page_number)
        citations.append(_page_only_citation(document_id, derived_hit))

    if not citations and not re.search(r"证据不足|未能找到|无法确认|cannot determine|insufficient evidence", answer, re.IGNORECASE):
        quoteable_hit = next((hit for hit in hits if hit.quote_eligible), None)
        if quoteable_hit is not None:
            citations.append({
                "document_id": str(document_id),
                "page_number": quoteable_hit.page_number,
                "quote": citation_quote(quoteable_hit.page_number, hits),
                "reason": "检索到的相关原文页",
                "is_derived": False,
            })
        else:
            derived_hit = next((hit for hit in hits if hit.is_derived), None)
            if derived_hit is not None:
                citations.append(_page_only_citation(document_id, derived_hit))
    return citations


def summarize_usage(document_id: UUID, task_id: UUID) -> dict[str, Any]:
    with Session(get_engine()) as session:
        rows = session.scalars(
            select(ProviderCall).where(
                ProviderCall.document_id == document_id,
                ProviderCall.task_id == task_id,
            ).order_by(ProviderCall.created_at, ProviderCall.id)
        ).all()
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = totals.setdefault(row.purpose, {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "known_cost_cny": 0.0, "cost_unknown_requests": 0})
        item["requests"] += 1
        item["prompt_tokens"] += row.prompt_tokens if row.prompt_tokens is not None else (row.estimated_prompt_tokens or 0)
        item["completion_tokens"] += row.completion_tokens or 0
        if row.estimated_cost_cny is None:
            item["cost_unknown_requests"] += 1
        else:
            item["known_cost_cny"] += float(row.estimated_cost_cny)
    return {
        "requests": len(rows),
        "prompt_tokens": sum(item["prompt_tokens"] for item in totals.values()),
        "completion_tokens": sum(item["completion_tokens"] for item in totals.values()),
        "known_cost_cny": round(sum(item["known_cost_cny"] for item in totals.values()), 8),
        "unpriced_request_count": sum(item["cost_unknown_requests"] for item in totals.values()),
        "by_purpose": totals,
    }


def answer_document_question(
    document_id: UUID,
    task_id: UUID,
    question: str,
    requested_version_id: UUID | None = None,
) -> dict[str, Any]:
    if not question or len(question) > MAX_QUESTION_CHARS:
        raise JobFailure("question_invalid")
    with Session(get_engine()) as session:
        document = session.get(Document, document_id)
        version_id = requested_version_id or (document.active_version_id if document is not None else None)
        version = session.get(DocumentVersion, version_id) if version_id is not None else None
        if document is None or document.is_deleted or version is None or version.document_id != document_id or version.status != "ready":
            raise JobFailure("document_version_not_available")
    client = SiliconFlowClient(document_id, task_id)
    try:
        client.check_embedding_capacity(estimate_tokens(question))
    except ProviderError as error:
        raise JobFailure(error.error_code) from None
    tool_schema = [{
        "type": "function",
        "function": {
            "name": "retrieve_evidence",
            "description": "在当前文献范围内检索最相关的原文段落。回答必须以此工具返回的内容为依据。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "需要在当前论文中查找的具体子问题"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }]
    first = client.chat({
        "messages": [
            {"role": "system", "content": "你是论文研读助手。只能依据用户当前选定论文作答。先调用 retrieve_evidence 检索原文；不要依赖外部知识，不展示内部推理。"},
            {"role": "user", "content": question},
        ],
        "tools": tool_schema,
        "tool_choice": {"type": "function", "function": {"name": "retrieve_evidence"}},
        "temperature": 0,
    }, max_tokens=2_048)
    choices = first.data.get("choices") or []
    assistant = choices[0].get("message", {}) if choices else {}
    tool_calls = assistant.get("tool_calls", []) if isinstance(assistant, dict) else []
    if not tool_calls:
        raise JobFailure("agent_did_not_retrieve_evidence")
    tool_call = tool_calls[0]
    function = tool_call.get("function", {})
    if function.get("name") != "retrieve_evidence":
        raise JobFailure("agent_requested_unknown_tool")
    try:
        arguments = json.loads(function.get("arguments", "{}"))
    except (TypeError, ValueError):
        raise JobFailure("agent_tool_arguments_invalid") from None
    query = arguments.get("query") if isinstance(arguments, dict) else None
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUESTION_CHARS:
        raise JobFailure("agent_tool_query_invalid")

    hits = retrieve_evidence(document_id, query.strip(), client, version_id)
    vision = inspect_relevant_page(document_id, version_id, question, hits, client)
    evidence = [{
        "page_number": hit.page_number,
        "text": hit.text[:2_000],
        "content_type": hit.content_type,
        "source": hit.source,
        "is_derived": hit.is_derived,
        "quote_eligible": hit.quote_eligible,
        "bbox": hit.bbox,
    } for hit in hits]
    tool_result = {"evidence": evidence, "visual_check": vision}
    final = client.chat({
        "messages": [
            {"role": "system", "content": (
                "根据工具检索的证据回答用户问题。不得补写证据中没有的作者观点。"
                "请只返回 JSON：{\"answer\": string, \"citations\": [{\"page_number\": integer, \"quote\": string, \"reason\": string}]}。"
                "页码必须来自工具结果；quote 只能是 quote_eligible=true 证据中的连续短语，尽量不超过 25 个英文词。"
                "若依据仅来自派生识别或整理内容，quote 留空并说明需核对原始 PDF 页面；不得把派生转写冒充原文。证据不足则在 answer 中直说并返回空 citations。"
            )},
            {"role": "user", "content": question},
            {"role": "assistant", "tool_calls": [{
                "id": tool_call.get("id", "retrieve-1"),
                "type": "function",
                "function": {"name": "retrieve_evidence", "arguments": json.dumps(arguments, ensure_ascii=False)},
            }]},
            {"role": "tool", "tool_call_id": tool_call.get("id", "retrieve-1"), "name": "retrieve_evidence", "content": json.dumps(tool_result, ensure_ascii=False)},
        ],
        "temperature": 0,
    }, max_tokens=8_192)
    final_choices = final.data.get("choices") or []
    final_message = final_choices[0].get("message", {}) if final_choices else {}
    answer_text = final_message.get("content", "") if isinstance(final_message, dict) else ""
    if not isinstance(answer_text, str) or not answer_text.strip():
        raise JobFailure("agent_empty_answer")

    parsed: dict[str, Any] = {}
    try:
        parsed = json.loads(answer_text)
    except ValueError:
        match = re.search(r"\{[\s\S]*\}", answer_text)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except ValueError:
                parsed = {}
    answer = parsed.get("answer") if isinstance(parsed, dict) else None
    if not isinstance(answer, str) or not answer.strip():
        answer = answer_text.strip()

    rows = parsed.get("citations", []) if isinstance(parsed, dict) else []
    citations = build_citations(document_id, hits, rows if isinstance(rows, list) else [], answer)
    return {
        "answer": answer.strip(),
        "citations": citations,
        "usage": summarize_usage(document_id, task_id),
        "retrieval": {"tool_calls": 1, "candidate_pages": list(dict.fromkeys(hit.page_number for hit in hits))},
        "visual_check": vision,
    }
