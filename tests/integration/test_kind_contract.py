"""validation-service records a file_kind; normalization-service must be able to parse every kind validation can accept.

Regression: validation recorded text files by extension ("txt") while normalization only knew "text", so every directly
uploaded .txt failed at normalization. Mocked chain tests registered documents by hand and never saw the mismatch.
"""
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "normalization-service"))
sys.path.insert(0, str(ROOT / "services" / "validation-service"))

from cleandocs_shared.config import Settings  # noqa: E402
from cleandocs_shared.exceptions import UnsupportedFormat  # noqa: E402
from cleandocs_shared.filetypes import ALLOWED_EXTENSIONS  # noqa: E402
from normalization_service.logic import SUPPORTED_KINDS  # noqa: E402
from validation_service import checks  # noqa: E402

SAMPLES = {
    "txt": b"plain text with some words in it\n", "csv": b"a,b\n1,2\n", "json": b'{"a": 1}', "xml": b"<a><b>x</b></a>",
    "pdf": None, "docx": None, "xlsx": None,
}


def _docx():
    from docx import Document

    b = io.BytesIO()
    d = Document()
    d.add_paragraph("hello")
    d.save(b)
    return b.getvalue()


def _xlsx():
    from openpyxl import Workbook

    b = io.BytesIO()
    wb = Workbook()
    wb.active["A1"] = "x"
    wb.save(b)
    return b.getvalue()


def _pdf():
    from reportlab.pdfgen import canvas

    b = io.BytesIO()
    c = canvas.Canvas(b)
    c.drawString(72, 750, "hello")
    c.save()
    return b.getvalue()


def _validate(ext, data):
    return checks.validate_content(io.BytesIO(data), filename=f"sample.{ext}", declared_mime=None, size=len(data),
                                   settings=Settings(service_name="validation-service", service_version="0"))


@pytest.mark.parametrize("ext", ["txt", "csv", "json", "xml", "pdf", "docx", "xlsx"])
def test_every_kind_validation_emits_for_a_supported_extension_is_parseable_by_normalization(ext):
    data = SAMPLES[ext] or {"pdf": _pdf, "docx": _docx, "xlsx": _xlsx}[ext]()
    res = _validate(ext, data)
    assert res["file_kind"] in SUPPORTED_KINDS, res["file_kind"]


def test_legacy_office_files_are_rejected_by_validation_not_later():
    ole2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600
    with pytest.raises(UnsupportedFormat) as e:
        _validate("doc", ole2)
    assert e.value.code == "LEGACY_OFFICE_UNSUPPORTED"


def test_extensions_that_validation_accepts_are_all_covered():
    kinds = {k for k in ALLOWED_EXTENSIONS.values()} - {"zip", "ole2"}
    assert kinds <= {"pdf", "jpeg", "png", "tiff", "docx", "xlsx", "text"}
