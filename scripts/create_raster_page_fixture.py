"""Create a one-page image-only OCR fixture from an identified sample-paper page."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import fitz
from pypdf import PdfReader


SOURCE_SHA256 = "519FED9EDE2F50E26D833312C4524537F5EE370A7420B8D3C7C824E228A04B70"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--page", type=int, default=5, help="1-based physical source page")
    parser.add_argument("--scale", type=float, default=2.4)
    args = parser.parse_args()

    source_hash = hashlib.sha256(args.source.read_bytes()).hexdigest().upper()
    if source_hash != SOURCE_SHA256:
        raise SystemExit("source PDF hash does not match the frozen sample manifest")

    with fitz.open(args.source) as source:
        if args.page < 1 or args.page > len(source):
            raise SystemExit("source page is out of range")
        source_page = source[args.page - 1]
        raster = source_page.get_pixmap(matrix=fitz.Matrix(args.scale, args.scale), alpha=False)
        image = raster.tobytes("png")
        image_pdf = fitz.open()
        page = image_pdf.new_page(width=source_page.rect.width, height=source_page.rect.height)
        page.insert_image(page.rect, stream=image)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        image_pdf.save(args.output, garbage=4, deflate=True)
        image_pdf.close()

    check = PdfReader(str(args.output), strict=False)
    if len(check.pages) != 1 or (check.pages[0].extract_text() or "").strip():
        raise SystemExit("output must contain exactly one page and no text layer")
    print(f"created {args.output} from source page {args.page}; source_sha256={source_hash}; extracted_text_chars=0")


if __name__ == "__main__":
    main()
