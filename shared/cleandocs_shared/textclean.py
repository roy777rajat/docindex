"""Safe text cleaning shared by cleaning-service and OCR post-processing.

Only whitespace, control characters, unicode form and line-wrap artefacts are touched. Numeric, date and currency
tokens are frozen: `protected_tokens` lets callers prove that nothing numeric changed.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​‌‍⁠﻿]")
_SPACES = re.compile(r"[ \t  -   　]+")
_HYPHEN_WRAP = re.compile(r"([A-Za-z]{2})-\n([a-z]{2})")
_PROTECTED = re.compile(r"[$€£₹]?\d[\d,.\-/:]*\d%?|\d")


def clean_text(text: str, *, join_lines: bool = False) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    lines = [_SPACES.sub(" ", ln).strip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if join_lines:  # paragraphs only; tables keep their row structure
        text = _HYPHEN_WRAP.sub(r"\1\2", text)
        text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    return text


def protected_tokens(text: str) -> Counter:
    return Counter(_PROTECTED.findall(text))
