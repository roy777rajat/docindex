"""Deterministic metadata rules: title, type, language hint, dates, keywords. No model calls."""
from __future__ import annotations

import re
from collections import Counter

DOC_TYPES: dict[str, tuple[str, ...]] = {
    "invoice": ("invoice", "bill to", "amount due", "invoice number", "gst", "subtotal", "due date"),
    "receipt": ("receipt", "paid", "payment received", "thank you for your purchase", "change due"),
    "medical_report": ("patient", "diagnosis", "clinical", "prescription", "hospital", "medical", "laboratory"),
    "contract": ("agreement", "party", "hereby", "terms and conditions", "governing law", "witness", "clause"),
    "resume": ("curriculum vitae", "work experience", "education", "skills", "references", "objective"),
    "bank_statement": ("statement of account", "opening balance", "closing balance", "debit", "credit", "account number"),
    "report": ("executive summary", "introduction", "conclusion", "findings", "methodology", "recommendations"),
}
STOP = set("""the and for that with this from have are was were will your you not but they their there which been has had
into than then them these those would could should about after before also such other over under more most some any all
can its our out who what when where how why one two been being does did done each very just may might must shall""".split())
_DATES = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2}\s(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s\d{4})\b",
    re.I)
_WORD = re.compile(r"[A-Za-z]{4,}")


def pick_title(pages: list[dict], filename: str) -> str:
    for p in pages:
        for b in p["blocks"]:
            if b["type"] == "heading":
                return b["text"][:200]
    for p in pages:
        for b in p["blocks"]:
            if b["type"] == "paragraph":
                first = b["text"].split("\n", 1)[0].strip()
                if 3 <= len(first) <= 120:
                    return first
    return filename.rsplit(".", 1)[0][:200]


def classify(text: str, kind: str) -> tuple[str, float, list[str]]:
    low = text.lower()
    best, hits = "general", []
    for name, kws in DOC_TYPES.items():
        found = [k for k in kws if k in low]
        if len(found) > len(hits):
            best, hits = name, found
    if len(hits) < 2:
        return ("spreadsheet", 0.6, []) if kind == "xlsx" else ("general", 0.2, hits)
    return best, round(min(1.0, len(hits) / 4), 2), hits


def language_hint(text: str) -> str:
    words = re.findall(r"[a-z]+", text.lower())
    if len(words) < 20:
        return "unknown"
    ratio = sum(1 for w in words if w in STOP or w in {"of", "to", "in", "is", "it", "on", "as", "at", "by", "be", "or"}) / len(words)
    return "en" if ratio >= 0.2 else "unknown"


def dates(text: str, limit: int = 10) -> list[str]:
    seen: list[str] = []
    for m in _DATES.findall(text):
        if m not in seen:
            seen.append(m)
        if len(seen) >= limit:
            break
    return seen


def keywords(text: str, limit: int = 10) -> list[str]:
    c = Counter(w.lower() for w in _WORD.findall(text) if w.lower() not in STOP)
    return [w for w, n in c.most_common(limit) if n >= 2]
