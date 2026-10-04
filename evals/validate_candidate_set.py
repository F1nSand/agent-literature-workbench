#!/usr/bin/env python3
"""Validate the supplemental candidate annotations without provider access."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from pypdf import PdfReader


ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = Path(__file__).resolve().parent / "datasets"
CANDIDATE_PATH = DATASET_DIR / "supplemental-candidate-set.json"
MANIFEST_PATH = DATASET_DIR / "sample-corpus-manifest.json"
EXPECTED_COUNTS = {"answerable": 8, "insufficient_evidence": 4, "scope_security": 4}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _normalize_extracted_text(value: str) -> str:
    value = re.sub(r"-\s+(?=[A-Za-z])", "", value)
    value = re.sub(r"\s+", "", value)
    return value.translate(str.maketrans({"−": "-", "–": "-", "’": "'"})).casefold()


def validate() -> tuple[dict[str, Any], dict[str, Any]]:
    dataset = _load(CANDIDATE_PATH)
    manifest = _load(MANIFEST_PATH)
    errors: list[str] = []

    source = dataset.get("source", {})
    if _sha256(MANIFEST_PATH) != source.get("corpus_manifest_sha256"):
        errors.append("corpus manifest hash mismatch")
    question_path = ROOT / "示例论文文档与问题集" / "问题集.md"
    if _sha256(question_path) != source.get("original_questions_sha256"):
        errors.append("original question hash mismatch")
    if dataset.get("status") != "candidate" or dataset.get("official_evaluation_allowed") is not False:
        errors.append("candidate set must remain non-official")

    documents = {
        item["filename"]: item
        for item in manifest.get("pdf_inventory", [])
        if isinstance(item, dict) and isinstance(item.get("filename"), str)
    }
    pdf_root = ROOT / "示例论文文档与问题集"
    pdf_readers: dict[str, PdfReader] = {}
    for filename, metadata in documents.items():
        pdf_path = pdf_root / filename
        if not pdf_path.is_file() or _sha256(pdf_path) != metadata.get("sha256"):
            errors.append(f"source PDF hash mismatch for {filename}")
        else:
            pdf_readers[filename] = PdfReader(str(pdf_path))
    questions = dataset.get("questions")
    if not isinstance(questions, list):
        raise ValueError("questions must be an array")
    if len(questions) != sum(EXPECTED_COUNTS.values()):
        errors.append("expected exactly 16 supplemental questions")

    ids: list[str] = []
    categories: Counter[str] = Counter()
    holdout: Counter[str] = Counter()
    for question in questions:
        if not isinstance(question, dict):
            errors.append("each question must be an object")
            continue
        question_id = question.get("id")
        category = question.get("category")
        ids.append(question_id)
        categories[category] += 1
        if question.get("review", {}).get("status") != "pending":
            errors.append(f"{question_id}: every annotation must remain pending")
        if question.get("proposed_split") == "proposed_holdout":
            holdout[category] += 1
        for field in ("query", "scope", "candidate_answerability", "content_type"):
            if not question.get(field):
                errors.append(f"{question_id}: missing {field}")

        evidence_groups = question.get("candidate_evidence_groups", [])
        if not isinstance(evidence_groups, list):
            errors.append(f"{question_id}: candidate_evidence_groups must be an array")
            continue
        for group in evidence_groups:
            if not isinstance(group, list) or not group:
                errors.append(f"{question_id}: evidence groups must be non-empty arrays")
                continue
            for evidence in group:
                if not isinstance(evidence, dict):
                    errors.append(f"{question_id}: evidence item must be an object")
                    continue
                filename = evidence.get("document")
                source_doc = documents.get(filename)
                if source_doc is None:
                    errors.append(f"{question_id}: unknown source document {filename}")
                    continue
                if evidence.get("pdf_sha256") != source_doc.get("sha256"):
                    errors.append(f"{question_id}: PDF hash mismatch for {filename}")
                page = evidence.get("physical_page")
                if not isinstance(page, int) or not 1 <= page <= source_doc["pages"]:
                    errors.append(f"{question_id}: invalid physical page for {filename}")
                if not isinstance(evidence.get("quote"), str) or not evidence["quote"].strip():
                    errors.append(f"{question_id}: evidence quote is required")
                elif filename in pdf_readers and isinstance(page, int) and 1 <= page <= source_doc["pages"]:
                    page_text = pdf_readers[filename].pages[page - 1].extract_text() or ""
                    if _normalize_extracted_text(evidence["quote"]) not in _normalize_extracted_text(page_text):
                        errors.append(f"{question_id}: quoted excerpt not found on physical page {page} of {filename}")

        if category == "answerable" and not question.get("candidate_answer_points"):
            errors.append(f"{question_id}: answerable question requires candidate answer points")
        if category == "scope_security":
            if not question.get("expected_behavior"):
                errors.append(f"{question_id}: security case requires expected behavior")
            if "expected_side_effects" not in question:
                errors.append(f"{question_id}: security case requires side-effect expectations")

    if len(set(ids)) != len(ids) or set(ids) != {f"Q{i}" for i in range(9, 25)}:
        errors.append("question IDs must be unique and cover Q9-Q24")
    if dict(categories) != EXPECTED_COUNTS:
        errors.append(f"category counts must be {EXPECTED_COUNTS}, got {dict(categories)}")
    if dict(holdout) != {"answerable": 4, "insufficient_evidence": 2, "scope_security": 2}:
        errors.append("proposed holdout must contain 4 answerable, 2 insufficient, and 2 security cases")

    if errors:
        raise ValueError("; ".join(errors))
    return dataset, manifest


def retrieval_qrels(dataset: dict[str, Any]) -> dict[str, Any]:
    questions = []
    for item in dataset["questions"]:
        if item["category"] != "answerable":
            continue
        evidence = [
            {
                "document": source["document"],
                "page_number": source["physical_page"],
                "pdf_sha256": source["pdf_sha256"],
                "quote": source["quote"],
            }
            for group in item["candidate_evidence_groups"]
            for source in group
        ]
        questions.append({"id": item["id"], "query": item["query"], "candidate_evidence": evidence})
    return {
        "dataset_id": dataset["dataset_id"],
        "annotation_status": "candidate",
        "corpus_manifest_sha256": dataset["source"]["corpus_manifest_sha256"],
        "source_review": CANDIDATE_PATH.name,
        "proposed_holdout_status": dataset["proposed_holdout"]["status"],
        "note": "Machine-assisted candidate evidence only. Do not present retrieval scores as official until all required labels are reviewed and confirmed.",
        "questions": questions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate pending supplemental evaluation candidates.")
    parser.add_argument("--emit-retrieval-qrels", type=Path, help="write the 8 answerable candidates in the evaluator input format")
    args = parser.parse_args()
    try:
        dataset, _manifest = validate()
        if args.emit_retrieval_qrels:
            output = args.emit_retrieval_qrels
            if not output.is_absolute():
                output = ROOT / output
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(retrieval_qrels(dataset), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({
        "status": "candidate-valid",
        "official_evaluation_allowed": False,
        "question_count": len(dataset["questions"]),
        "category_counts": dict(Counter(item["category"] for item in dataset["questions"])),
        "proposed_holdout": dataset["proposed_holdout"]["allocation"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
