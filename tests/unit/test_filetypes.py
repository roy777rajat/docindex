import io
import zipfile

import pytest

from cleandocs_shared import filetypes as ft


def _zip(names: dict[str, bytes]) -> io.BytesIO:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in names.items():
            z.writestr(n, d)
    b.seek(0)
    return b


@pytest.mark.parametrize(
    "content,kind",
    [
        (b"%PDF-1.7\n...", "pdf"),
        (b"\x89PNG\r\n\x1a\n" + b"0" * 20, "png"),
        (b"\xff\xd8\xff\xe0" + b"0" * 20, "jpeg"),
        (b"II*\x00" + b"0" * 20, "tiff"),
        (b"MM\x00*" + b"0" * 20, "tiff"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 20, "ole2"),
        (b"just some plain text\nwith lines", "text"),
        (b'{"a": 1}', "text"),
        (b"", "empty"),
        (b"\x00\x01\x02\x03\x04binary", "unknown"),
    ],
)
def test_detect_by_magic_bytes(content, kind):
    assert ft.detect_kind(io.BytesIO(content)) == kind


def test_zip_family_is_told_apart_by_contents():
    assert ft.detect_kind(_zip({"a.txt": b"x"})) == "zip"
    assert ft.detect_kind(_zip({"[Content_Types].xml": b"<x/>", "word/document.xml": b"<x/>"})) == "docx"
    assert ft.detect_kind(_zip({"[Content_Types].xml": b"<x/>", "xl/workbook.xml": b"<x/>"})) == "xlsx"
    assert ft.detect_kind(_zip({"[Content_Types].xml": b"<x/>", "ppt/slides/s1.xml": b"<x/>"})) == "ooxml_other"
    assert ft.detect_kind(io.BytesIO(b"PK\x03\x04" + b"garbage" * 10)) == "corrupt_zip"


def test_extension_cannot_override_content():
    # an .exe renamed to .pdf is "unknown" content, so it can never match the pdf extension
    kind = ft.detect_kind(io.BytesIO(b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00" + b"\x00" * 50))
    assert not ft.extension_matches_kind("pdf", kind)
    assert ft.extension_matches_kind("pdf", "pdf")
    assert not ft.extension_matches_kind("docx", "zip")
    assert ft.extension_matches_kind("json", "text") and not ft.extension_matches_kind("exe", "unknown")


def test_extension_of():
    assert ft.extension_of("a/b\\Scan.PDF") == "pdf" and ft.extension_of("noext") == ""


@pytest.mark.parametrize(
    "ext,declared,ok",
    [
        ("pdf", "application/pdf", True),
        ("pdf", "application/octet-stream", True),
        ("pdf", None, True),
        ("pdf", "image/png", False),
        ("csv", "application/vnd.ms-excel", True),
        ("png", "image/png; charset=binary", True),
        ("docx", "application/pdf", False),
    ],
)
def test_declared_mime_consistency(ext, declared, ok):
    assert ft.declared_mime_consistent(ext, declared) is ok
