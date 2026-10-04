from uuid import uuid4

from app.documents.pipeline import SearchHit, build_citations, citation_quote, select_visual_page, split_content_units
from app.providers.siliconflow import estimate_tokens


def test_split_content_units_keeps_content_and_respects_limit() -> None:
    text = ("第一段内容。 " * 180) + "\n\n" + ("second paragraph with enough words. " * 120)
    units = split_content_units(text, max_chars=500)

    assert len(units) > 1
    assert all(0 < len(unit) <= 500 for unit in units)
    assert "第一段内容" in "".join(units)
    assert "second paragraph" in " ".join(units)


def test_token_estimate_counts_cjk_and_formula_conservatively() -> None:
    assert estimate_tokens("中文检索 pass@k: n-c") >= len("中文检索")


def test_citation_quote_is_short_and_stays_on_retrieved_page() -> None:
    source = "pass@k estimator. The unbiased Codex estimator characterizes what fraction of tasks any k randomly chosen candidates would solve."
    hit = SearchHit(id=uuid4(), page_number=6, text=source, score=0.9)

    quote = citation_quote(6, [hit])

    assert quote == "The unbiased Codex estimator"
    assert len(quote.split()) <= 20
    assert citation_quote(7, [hit]) == ""


def test_citation_quote_rejects_derived_transcription() -> None:
    transcription = SearchHit(
        id=uuid4(),
        page_number=3,
        text="Recognized words from a scanned page.",
        score=0.9,
        source="cloud_ocr",
        is_derived=True,
        quote_eligible=False,
    )

    assert citation_quote(3, [transcription]) == ""


def test_derived_ocr_can_locate_original_page_without_becoming_a_quote() -> None:
    document_id = uuid4()
    transcription = SearchHit(
        id=uuid4(),
        page_number=3,
        text="Recognized words from a scanned page.",
        score=0.9,
        source="cloud_ocr",
        is_derived=True,
        quote_eligible=False,
    )

    citations = build_citations(
        document_id,
        [transcription],
        [{"page_number": 3, "quote": transcription.text}],
        "扫描页提到了目标内容。",
    )

    assert citations == [{
        "document_id": str(document_id),
        "page_number": 3,
        "quote": "",
        "reason": "扫描页识别内容仅用于定位，请核对原始 PDF 页面。",
        "is_derived": True,
    }]
    assert transcription.text not in citations[0]["quote"]
    assert build_citations(document_id, [transcription], [{"page_number": 4, "quote": "x"}], "有依据的回答。") == [citations[0]]


def test_native_citation_keeps_only_a_verifiable_quote() -> None:
    document_id = uuid4()
    native = SearchHit(
        id=uuid4(),
        page_number=6,
        text="This exact sentence supports a claim.",
        score=0.9,
    )

    citations = build_citations(
        document_id,
        [native],
        [{"page_number": 6, "quote": "unverified model text"}],
        "The claim is supported.",
    )

    assert citations == [{
        "document_id": str(document_id),
        "page_number": 6,
        "quote": "This exact sentence supports a claim.",
        "reason": "检索到的相关原文页",
        "is_derived": False,
    }]


def test_visual_page_selection_prefers_formula_over_reference_mention() -> None:
    reference = SearchHit(
        id=uuid4(), page_number=21,
        text="Reference [9] discusses the pass@k estimator.", score=0.99,
    )
    formula = SearchHit(
        id=uuid4(), page_number=6,
        text="The unbiased Codex estimator is pass@k: dpass@k = 1 − n−c k / n k.", score=0.8,
    )

    assert select_visual_page("请找出 pass@k 公式", [reference, formula]) == 6
