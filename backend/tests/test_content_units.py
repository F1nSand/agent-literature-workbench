import fitz

from app.documents.pipeline import parse_page_content


def test_native_equations_are_separate_quote_eligible_formula_units() -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "The pass rate is estimated as follows.")
    page.insert_text((72, 110), "pass@k = 1 - (1 - p)^k")
    page.insert_text((72, 145), "This estimate improves with more samples.")

    units = parse_page_content(
        page,
        "The pass rate is estimated as follows.\npass@k = 1 - (1 - p)^k\nThis estimate improves with more samples.",
        "pdf_text",
    )
    formula = next(unit for unit in units if unit.content_type == "formula")
    body = "\n".join(unit.text for unit in units if unit.content_type == "text")

    assert formula.text == "pass@k = 1 - (1 - p)^k"
    assert formula.source == "pdf_text"
    assert formula.is_derived is False
    assert formula.quote_eligible is True
    assert formula.bbox is not None
    assert "pass@k = 1 - (1 - p)^k" not in body
    assert "improves with more samples" in body
    document.close()


def test_code_and_parameter_lines_remain_native_text() -> None:
    document = fitz.open()
    page = document.new_page()
    lines = [
        "i=1 and the branch",
        "Pipeline (N=5, t=0.5, M=5)",
        "model_cfg = Config.from_file(cfg_file)",
        "The K=2 spawn fan-out.",
        "Baseline → CoT",
    ]
    for index, value in enumerate(lines):
        page.insert_text((72, 72 + index * 24), value)

    units = parse_page_content(page, "\n".join(lines), "pdf_text")

    assert not any(unit.content_type == "formula" for unit in units)
    body = "\n".join(unit.text for unit in units if unit.content_type == "text")
    for line in lines:
        assert line in body
    document.close()


def test_native_table_cells_are_structured_without_claiming_original_layout() -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 80), "Table 1: Results")
    x_edges = [72, 245, 418]
    y_edges = [100, 145, 190]
    for x in x_edges:
        page.draw_line((x, y_edges[0]), (x, y_edges[-1]), color=(0, 0, 0))
    for y in y_edges:
        page.draw_line((x_edges[0], y), (x_edges[-1], y), color=(0, 0, 0))
    values = [["Method", "Score"], ["Baseline", "0.72"]]
    for row_index, row in enumerate(values):
        for column_index, value in enumerate(row):
            page.insert_text((x_edges[column_index] + 10, y_edges[row_index] + 28), value)
    page_text = page.get_text("text", sort=True)

    units = parse_page_content(page, page_text, "pdf_text")
    table = next(unit for unit in units if unit.content_type == "table")

    assert "Method | Score" in table.text
    assert "Baseline | 0.72" in table.text
    assert table.source == "pdf_text_table"
    assert table.is_derived is True
    assert table.quote_eligible is False
    assert table.bbox is not None
    document.close()


def test_compact_borderless_table_is_detected_but_not_whole_page_columns() -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 80), "Table 2: Method comparison")
    rows = [
        ["Method", "Score", "Time"],
        ["Baseline", "0.72", "1.4s"],
        ["Proposed", "0.86", "1.8s"],
    ]
    x_positions = [72, 250, 420]
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            page.insert_text((x_positions[column_index], 120 + row_index * 28), value)
    page_text = page.get_text("text", sort=True)

    units = parse_page_content(page, page_text, "pdf_text")
    table = next(unit for unit in units if unit.content_type == "table")

    assert "Method | Score | Time" in table.text
    assert "Proposed | 0.86 | 1.8s" in table.text
    document.close()


def test_chart_caption_is_classified_only_when_near_graphics() -> None:
    document = fitz.open()
    page = document.new_page()
    page.draw_rect(fitz.Rect(100, 120, 420, 300), color=(0, 0, 0))
    page.insert_text((105, 330), "Figure 1: Retrieval accuracy by method")

    units = parse_page_content(page, page.get_text("text", sort=True), "pdf_text")
    chart = next(unit for unit in units if unit.content_type == "chart")

    assert chart.text == "Figure 1: Retrieval accuracy by method"
    assert chart.source == "pdf_text"
    assert chart.is_derived is False
    assert chart.quote_eligible is True
    assert chart.bbox is not None

    no_graphics = document.new_page()
    no_graphics.insert_text((72, 72), "Figure 2: Mention without a chart")
    text_only_units = parse_page_content(no_graphics, no_graphics.get_text("text"), "pdf_text")
    assert not any(unit.content_type == "chart" for unit in text_only_units)
    document.close()


def test_ocr_transcription_is_marked_as_derived_and_not_verbatim_citable() -> None:
    document = fitz.open()
    page = document.new_page()

    units = parse_page_content(page, "Recognized words from a scanned page.", "cloud_ocr")

    assert len(units) == 1
    assert units[0].content_type == "text"
    assert units[0].source == "cloud_ocr"
    assert units[0].is_derived is True
    assert units[0].quote_eligible is False
    document.close()
