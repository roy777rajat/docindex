"""Format-aware parsing into the internal schema: pages -> blocks, each carrying citation locators.

Page/sheet/section are recorded only when the format really provides them (no guessing).
"""
from __future__ import annotations

import csv
import io
from typing import Any, BinaryIO

MIN_TEXT_CHARS = 25  # a PDF page with less text than this is treated as scanned
MAX_SHEET_ROWS = 20_000


def _paras(text: str) -> list[str]:
    return [p.strip() for p in text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n") if p.strip()]


def _block(unit: str, n: int, btype: str, text: str, section: str | None, level: int | None = None) -> dict[str, Any]:
    b: dict[str, Any] = {"id": f"{unit}-b{n}", "type": btype, "text": text}
    if section:
        b["section"] = section
    if level:
        b["level"] = level
    return b


def decode_text(raw: bytes) -> tuple[str, str]:
    for enc in ("utf-8-sig", "utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            pass
    return raw.decode("cp1252", errors="replace"), "cp1252"


def parse_text(f: BinaryIO, extension: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    text, enc = decode_text(f.read())
    blocks: list[dict[str, Any]] = []
    if extension == "csv":
        rows = [" | ".join(c.strip() for c in r) for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
        if rows:
            blocks.append(_block("d", 1, "table", "\n".join(rows), None))
    else:
        blocks = [_block("d", i, "paragraph", p, None) for i, p in enumerate(_paras(text), 1)]
    return [{"page": None, "needs_ocr": False, "blocks": blocks}], {"encoding": enc}


def parse_pdf(f: BinaryIO, max_pages: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from pypdf import PdfReader

    reader = PdfReader(f)
    n = len(reader.pages)
    if n > max_pages:
        from cleandocs_shared.exceptions import ValidationFailure
        raise ValidationFailure(f"PDF has {n} pages, limit is {max_pages}", code="PDF_TOO_MANY_PAGES")
    pages = []
    for i, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        scanned = len(text) < MIN_TEXT_CHARS
        blocks = [] if scanned else [_block(f"p{i}", j, "paragraph", p, None) for j, p in enumerate(_paras(text) or [text], 1)]
        pages.append({"page": i, "needs_ocr": scanned, "method": "text_layer", "blocks": blocks})
    return pages, {}


def parse_docx(f: BinaryIO) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(f)
    blocks: list[dict[str, Any]] = []
    path: list[str] = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        n = len(blocks) + 1
        if tag == "p":
            p = Paragraph(child, doc)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name or "") if p.style is not None else ""
            if style.lower().startswith("heading") or style == "Title":
                digits = "".join(c for c in style if c.isdigit())
                level = int(digits) if digits else 1
                path = path[: level - 1] + [text]
                blocks.append(_block("d", n, "heading", text, " > ".join(path), level))
            else:
                blocks.append(_block("d", n, "paragraph", text, " > ".join(path) or None))
        elif tag == "tbl":
            t = Table(child, doc)
            rows = [" | ".join(c.text.strip().replace("\n", " ") for c in r.cells) for r in t.rows]
            rows = [r for r in rows if r.replace("|", "").strip()]
            if rows:
                blocks.append(_block("d", n, "table", "\n".join(rows), " > ".join(path) or None))
    return [{"page": None, "needs_ocr": False, "blocks": blocks}], {}


def parse_xlsx(f: BinaryIO) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from openpyxl import load_workbook

    wb = load_workbook(f, read_only=True, data_only=True)
    pages, truncated = [], []
    for si, ws in enumerate(wb.worksheets, 1):
        rows: list[str] = []
        for ri, row in enumerate(ws.iter_rows(values_only=True), 1):
            if ri > MAX_SHEET_ROWS:
                truncated.append(ws.title)
                break
            cells = ["" if v is None else str(v).strip() for v in row]
            if any(cells):
                rows.append(" | ".join(cells))
        blocks = [_block(f"s{si}", 1, "table", "\n".join(rows), ws.title)] if rows else []
        pages.append({"page": None, "sheet": ws.title, "needs_ocr": False, "blocks": blocks})
    return pages, {"truncated_sheets": truncated}


def parse_image(f: BinaryIO, kind: str, max_pages: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from PIL import Image

    img = Image.open(f)
    frames = min(getattr(img, "n_frames", 1), max_pages)
    return [{"page": i, "needs_ocr": True, "method": "pending_ocr", "blocks": []} for i in range(1, frames + 1)], {}
