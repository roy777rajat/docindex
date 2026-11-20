"""Layout-aware chunking. Each chunk keeps the locators needed for a citation (page range, sheet, section, block ids).

Rules: chunks never cross documents or spreadsheet sheets; headings start a new chunk once the current one has real
content; tables are split by rows with the header row repeated; oversized paragraphs are split on sentences, then
hard-split with a small overlap. Locators are copied from the source blocks, never guessed.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

_SENT = re.compile(r"(?<=[.!?])\s+")
MIN_BEFORE_HEADING_BREAK = 300


def hard_split(text: str, max_chars: int, overlap: int) -> list[str]:
    out, i = [], 0
    step = max(1, max_chars - overlap)
    while i < len(text):
        out.append(text[i:i + max_chars])
        if i + max_chars >= len(text):
            break
        i += step
    return out


def split_paragraph(text: str, max_chars: int, overlap: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    pieces, cur = [], ""
    for sent in _SENT.split(text):
        if len(sent) > max_chars:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.extend(hard_split(sent, max_chars, overlap))
        elif cur and len(cur) + 1 + len(sent) > max_chars:
            pieces.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        pieces.append(cur)
    return pieces


def split_table(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    lines = text.split("\n")
    header, rows = lines[0], lines[1:]
    pieces, cur = [], [header]
    size = len(header)
    for row in rows:
        row = row if len(row) + len(header) + 1 <= max_chars else row[: max_chars - len(header) - 1]
        if len(cur) > 1 and size + len(row) + 1 > max_chars:
            pieces.append("\n".join(cur))
            cur, size = [header], len(header)
        cur.append(row)
        size += len(row) + 1
    if len(cur) > 1:
        pieces.append("\n".join(cur))
    return pieces


def _pieces(block: dict[str, Any], max_chars: int, overlap: int) -> list[str]:
    if block["type"] == "table":
        return split_table(block["text"], max_chars)
    return split_paragraph(block["text"], max_chars, overlap)


def chunk_document(pages: list[dict[str, Any]], *, target: int, max_chars: int, overlap: int) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal cur
        if cur and cur["parts"]:
            text = "\n\n".join(cur["parts"])
            idx = len(chunks)
            chunks.append({
                "index": idx, "chunk_id": f"c{idx:04d}-{hashlib.sha256(text.encode()).hexdigest()[:8]}",
                "text": text, "page_start": cur["page_start"], "page_end": cur["page_end"], "sheet": cur["sheet"],
                "section": cur["section"], "block_ids": cur["block_ids"], "char_count": len(text),
            })
        cur = None

    for page in pages:
        for block in page["blocks"]:
            for n, piece in enumerate(_pieces(block, max_chars, overlap)):
                size = len(piece)
                if cur is not None:
                    cur_len = sum(len(p) + 2 for p in cur["parts"])
                    is_head = block["type"] == "heading" and n == 0
                    if cur["sheet"] != page.get("sheet") or cur_len + size > target or (is_head and cur_len >= MIN_BEFORE_HEADING_BREAK):
                        flush()
                if cur is None:
                    cur = {"parts": [], "page_start": page.get("page"), "page_end": page.get("page"),
                           "sheet": page.get("sheet"), "section": block.get("section"), "block_ids": []}
                cur["parts"].append(piece)
                cur["page_end"] = page.get("page")
                if block["id"] not in cur["block_ids"]:
                    cur["block_ids"].append(block["id"])
                if cur["section"] is None:
                    cur["section"] = block.get("section")
    flush()
    return chunks
