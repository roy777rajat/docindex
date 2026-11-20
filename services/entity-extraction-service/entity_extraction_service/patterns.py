"""Deterministic entity-candidate patterns. Candidates are suggestions with evidence, not facts."""
from __future__ import annotations

import re

MONEY = re.compile(r"(?:₹|Rs\.?|INR|\$|USD|€|EUR|£|GBP)\s?\d[\d,]*(?:\.\d{1,2})?")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
DATE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2}\s(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s\d{4})\b",
    re.I)
IDENTIFIER = re.compile(
    r"\b(?:invoice|policy|account|reference|ref|order|claim|receipt)\s*(?:no\.?|number|#|id)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]{3,})",
    re.I)
ORGANIZATION = re.compile(
    r"\b([A-Z][A-Za-z&]+(?:\s[A-Z][A-Za-z&]+){0,3}\s(?:Pvt\.?\sLtd|Ltd|Limited|Inc|LLC|LLP|Corporation|Corp|Hospital|Clinic|Bank|University))\b")
PERSON = re.compile(r"\b(?:Dr|Mr|Mrs|Ms|Prof)\.?\s([A-Z][a-z]+(?:\s[A-Z][a-z]+){0,2})")

# (type, regex, capture group, confidence). Confidence reflects how unambiguous the pattern is.
RULES = [
    ("EMAIL", EMAIL, 0, 0.95),
    ("MONEY", MONEY, 0, 0.9),
    ("DATE", DATE, 0, 0.85),
    ("IDENTIFIER", IDENTIFIER, 1, 0.7),
    ("ORGANIZATION", ORGANIZATION, 1, 0.6),
    ("PERSON", PERSON, 1, 0.5),
]

# Capitalised words that start sentences and get absorbed by the organisation pattern ("See Acme Traders Ltd").
LEADING_NOISE = {"see", "the", "this", "that", "contact", "please", "from", "to", "by", "dear", "attn", "and", "for", "of",
                 "at", "in", "on", "with", "via", "paid", "issued", "billed", "invoice", "bill", "total", "payment"}


def trim_leading_noise(value: str) -> tuple[str, int]:
    """Returns (trimmed value, number of characters removed from the start)."""
    removed = 0
    while True:
        first, _, rest = value.partition(" ")
        if rest and first.lower() in LEADING_NOISE:
            removed += len(first) + 1
            value = rest
        else:
            return value, removed
