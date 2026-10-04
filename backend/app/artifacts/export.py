import csv
import io
from dataclasses import dataclass


@dataclass(frozen=True)
class ExportSource:
    document_title: str
    document_id: str
    version_id: str
    version_number: int | None
    page_number: int
    content_unit_id: str | None
    content_sha256: str | None


_ARTIFACT_TYPE_LABELS = {
    "note": "阅读笔记",
    "comparison_table": "文献比较",
    "draft": "研究草稿",
}
_CSV_HEADERS = [
    "成果名称",
    "成果类型",
    "成果版本",
    "正文",
    "来源文献",
    "文献 ID",
    "文献版本",
    "页码",
    "内容单元 ID",
    "来源内容哈希",
]
_FORMULA_PREFIXES = frozenset("=+-@")
_UNSAFE_LEADING_CONTROLS = frozenset("\t\r\n")


def safe_csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    first_visible = text.lstrip()
    leading_whitespace = text[: len(text) - len(first_visible)]
    if (
        first_visible and first_visible[0] in _FORMULA_PREFIXES
    ) or any(character in _UNSAFE_LEADING_CONTROLS for character in leading_whitespace):
        return "'" + text
    return text


def render_markdown(
    *,
    title: str,
    artifact_type: str,
    version_number: int,
    body_text: str,
    sources: list[ExportSource],
) -> str:
    label = _ARTIFACT_TYPE_LABELS.get(artifact_type, "学习成果")
    lines = [
        f"# {title}",
        "",
        f"类型：{label}",
        f"版本：{version_number}",
        "",
        "## 正文",
        body_text or "（暂无正文）",
        "",
        "## 文献来源",
    ]
    if sources:
        for source in sources:
            version = f"，文献版本 {source.version_number}" if source.version_number is not None else ""
            lines.append(f"- {source.document_title}{version}，第 {source.page_number} 页")
    else:
        lines.append("暂无保存的来源记录。")
    return "\n".join(lines) + "\n"


def render_csv(
    *,
    title: str,
    artifact_type: str,
    version_number: int,
    body_text: str,
    sources: list[ExportSource],
) -> bytes:
    label = _ARTIFACT_TYPE_LABELS.get(artifact_type, "学习成果")
    rows = sources or [None]
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(_CSV_HEADERS)
    for source in rows:
        writer.writerow([
            safe_csv_cell(title),
            safe_csv_cell(label),
            str(version_number),
            safe_csv_cell(body_text),
            safe_csv_cell(source.document_title if source else ""),
            safe_csv_cell(source.document_id if source else ""),
            str(source.version_number) if source and source.version_number is not None else "",
            str(source.page_number) if source else "",
            safe_csv_cell(source.content_unit_id if source else ""),
            safe_csv_cell(source.content_sha256 if source else ""),
        ])
    return output.getvalue().encode("utf-8-sig")
