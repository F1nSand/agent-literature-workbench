import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID


MAX_JSON_CHARS = 400_000
MAX_NODES = 5_000
MAX_DEPTH = 12
MAX_TEXT_CHARS = 200_000
BLOCKS = {"doc", "paragraph", "heading", "bulletList", "orderedList", "listItem", "blockquote", "codeBlock", "horizontalRule"}
INLINE = {"text", "hardBreak"}
MARKS = {"bold", "italic", "strike", "code", "underline", "link", "textStyle"}
TEXT_STYLE_FAMILIES = {"Noto Sans SC", "Noto Serif SC", "Georgia", "Arial", "serif", "sans-serif"}
TEXT_STYLE_SIZES = {"12px", "14px", "16px", "18px", "22px"}
TEXT_STYLE_COLORS = {"#416c5a", "#826743", "#557585", "#b05d4b", "#26332f"}
DOCUMENT_PATH = re.compile(r"^/documents/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")
UUID_QUERY_VALUE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _safe_document_href(href: str) -> bool:
    try:
        parsed = urlsplit(href)
        match = DOCUMENT_PATH.fullmatch(parsed.path)
        if parsed.scheme or parsed.netloc or parsed.fragment or match is None:
            return False
        if str(UUID(match.group(1))) != match.group(1).lower():
            return False
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=5)
    except ValueError:
        return False
    values: dict[str, str] = {}
    for key, value in query:
        if key not in {"version", "page", "chapter", "cfi", "anchor"} or key in values:
            return False
        values[key] = value
    if "version" in values:
        if not UUID_QUERY_VALUE.fullmatch(values["version"]):
            return False
        if str(UUID(values["version"])) != values["version"].lower():
            return False
    locator_keys = set(values) - {"version"}
    if "version" not in values or not locator_keys:
        return False
    if "page" in locator_keys:
        return len(locator_keys) == 1 and re.fullmatch(r"[1-9][0-9]{0,8}", values["page"]) is not None
    if "anchor" in locator_keys:
        return len(locator_keys) == 1 and re.fullmatch(r"p-[1-9][0-9]{0,8}", values["anchor"]) is not None
    if locator_keys - {"chapter", "cfi"}:
        return False
    if "chapter" in values:
        chapter = values["chapter"]
        if len(chapter) > 2000 or ".." in chapter or "://" in chapter or any(ord(char) < 0x20 for char in chapter):
            return False
    if "cfi" in values and re.fullmatch(r"epubcfi\([^\s\x00-\x1f]{1,1990}\)", values["cfi"]) is None:
        return False
    return True


def validate_content(value: Any) -> dict:
    """Normalize and validate the deliberately small Tiptap JSON subset."""
    if not isinstance(value, dict) or value.get("type") != "doc":
        raise ValueError("正文必须是 Tiptap doc 对象")
    budget = {"nodes": 0, "text": 0}

    def walk(node: Any, depth: int, parent: str | None = None) -> dict:
        if depth > MAX_DEPTH or not isinstance(node, dict):
            raise ValueError("正文结构过深或节点格式错误")
        budget["nodes"] += 1
        if budget["nodes"] > MAX_NODES:
            raise ValueError("正文节点数量超出限制")
        kind = node.get("type")
        if not isinstance(kind, str) or kind not in BLOCKS | INLINE:
            raise ValueError("正文包含不支持的节点")
        out: dict[str, Any] = {"type": kind}
        allowed = {"type", "content", "attrs", "marks", "text"}
        if set(node) - allowed:
            raise ValueError("正文节点包含不支持的字段")
        if kind == "text":
            text = node.get("text")
            if not isinstance(text, str):
                raise ValueError("文本节点内容格式错误")
            budget["text"] += len(text)
            if budget["text"] > MAX_TEXT_CHARS:
                raise ValueError("正文文本长度超出限制")
            out["text"] = text
            marks = node.get("marks", [])
            if not isinstance(marks, list) or len(marks) > 8:
                raise ValueError("文本样式格式错误")
            if marks:
                out["marks"] = [clean_mark(mark) for mark in marks]
            if "content" in node or "attrs" in node:
                raise ValueError("文本节点不能含子节点或属性")
            return out
        attrs = node.get("attrs", {})
        if not isinstance(attrs, dict):
            raise ValueError("节点属性格式错误")
        if kind == "heading":
            if set(attrs) - {"level"} or attrs.get("level", 1) not in (1, 2, 3):
                raise ValueError("标题级别无效")
            out["attrs"] = {"level": attrs.get("level", 1)}
        elif kind == "orderedList":
            if set(attrs) - {"start"} or not isinstance(attrs.get("start", 1), int) or not 1 <= attrs.get("start", 1) <= 10000:
                raise ValueError("有序列表属性无效")
            out["attrs"] = {"start": attrs.get("start", 1)}
        elif kind == "codeBlock":
            if set(attrs) - {"language"}:
                raise ValueError("代码块属性无效")
            clean_attrs = {key: value for key, value in attrs.items() if value is not None}
            if "language" in clean_attrs and (not isinstance(clean_attrs["language"], str) or len(clean_attrs["language"]) > 40):
                raise ValueError("代码块属性无效")
            if clean_attrs:
                out["attrs"] = clean_attrs
        elif attrs:
            raise ValueError("此节点不接受属性")
        children = node.get("content", [])
        if not isinstance(children, list):
            raise ValueError("正文子节点格式错误")
        if kind == "horizontalRule":
            if children:
                raise ValueError("分隔线不能有子节点")
        elif kind == "hardBreak":
            if children:
                raise ValueError("换行节点不能有子节点")
        elif kind == "doc":
            if parent is not None or any(not isinstance(child, dict) or child.get("type") not in BLOCKS - {"doc"} for child in children):
                raise ValueError("文档只能包含块节点")
        elif kind in {"paragraph", "heading", "codeBlock"}:
            if any(not isinstance(child, dict) or child.get("type") not in INLINE for child in children):
                raise ValueError("文本块只能包含行内节点")
        elif kind in {"bulletList", "orderedList"}:
            if any(not isinstance(child, dict) or child.get("type") != "listItem" for child in children):
                raise ValueError("列表只能包含列表项")
        elif kind == "listItem":
            if any(not isinstance(child, dict) or child.get("type") not in {"paragraph", "bulletList", "orderedList", "blockquote", "codeBlock"} for child in children):
                raise ValueError("列表项内容无效")
        elif kind == "blockquote":
            if any(not isinstance(child, dict) or child.get("type") not in {"paragraph", "heading", "bulletList", "orderedList", "codeBlock"} for child in children):
                raise ValueError("引用块内容无效")
        if children:
            out["content"] = [walk(child, depth + 1, kind) for child in children]
        if "marks" in node:
            raise ValueError("样式只能附加于文本节点")
        return out

    normalized = walk(value, 0)
    import json
    if len(json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))) > MAX_JSON_CHARS:
        raise ValueError("正文数据超出限制")
    return normalized


def clean_mark(mark: Any) -> dict:
    if not isinstance(mark, dict) or set(mark) - {"type", "attrs"}:
        raise ValueError("文本样式格式错误")
    kind = mark.get("type")
    if kind not in MARKS:
        raise ValueError("正文包含不支持的文本样式")
    attrs = mark.get("attrs", {})
    if not isinstance(attrs, dict):
        raise ValueError("文本样式属性格式错误")
    if kind == "link":
        href = attrs.get("href")
        if set(attrs) - {"href", "target", "rel", "title"} or not isinstance(href, str) or len(href) > 4096:
            raise ValueError("链接地址无效")
        if "title" in attrs and attrs["title"] is not None:
            raise ValueError("链接标题不受支持")
        if any(char.isspace() or ord(char) < 0x20 for char in href):
            raise ValueError("链接地址无效")
        parsed = urlsplit(href)
        scheme = parsed.scheme.lower()
        if scheme in {"http", "https", "mailto"}:
            if (scheme in {"http", "https"} and not parsed.netloc) or (scheme == "mailto" and not parsed.path):
                raise ValueError("链接地址无效")
        elif not _safe_document_href(href):
            raise ValueError("链接地址不受支持")
        return {"type": "link", "attrs": {"href": href, "target": "_blank", "rel": "noopener noreferrer"}}
    if kind == "textStyle":
        allowed = {"fontFamily", "fontSize", "color"}
        if not attrs or set(attrs) - allowed:
            raise ValueError("文本样式属性不受支持")
        attrs = {key: value for key, value in attrs.items() if value is not None and value != ""}
        if not attrs:
            raise ValueError("文本样式属性不受支持")
        if "fontFamily" in attrs and attrs["fontFamily"] not in TEXT_STYLE_FAMILIES:
            raise ValueError("字体不受支持")
        if "fontSize" in attrs and attrs["fontSize"] not in TEXT_STYLE_SIZES:
            raise ValueError("字号不受支持")
        if "color" in attrs and attrs["color"] not in TEXT_STYLE_COLORS:
            raise ValueError("文字颜色不受支持")
        return {"type": "textStyle", "attrs": attrs.copy()}
    if attrs:
        raise ValueError("此文本样式不接受属性")
    return {"type": kind}


def plain_text(content: dict) -> str:
    out: list[str] = []
    def visit(node: dict) -> None:
        kind = node["type"]
        if kind == "text":
            out.append(node["text"])
        elif kind == "hardBreak":
            out.append("\n")
        else:
            for child in node.get("content", []):
                visit(child)
            if kind in {"paragraph", "heading", "listItem", "blockquote", "codeBlock"}:
                out.append("\n")
    visit(content)
    return "".join(out).strip()
