"""File type detection from content (magic bytes), never from the extension alone.

Pure utility shared by validation-service and archive-service. Works on any seekable binary file.
"""
from __future__ import annotations

import zipfile
from typing import BinaryIO

SAMPLE_BYTES = 8192

# extension -> the content kind that must be detected for the file to be accepted
ALLOWED_EXTENSIONS: dict[str, str] = {
    "pdf": "pdf",
    "jpg": "jpeg", "jpeg": "jpeg",
    "png": "png",
    "tif": "tiff", "tiff": "tiff",
    "docx": "docx", "doc": "ole2",
    "xlsx": "xlsx", "xls": "ole2",
    "csv": "text", "txt": "text", "json": "text", "xml": "text",
    "zip": "zip",
}

_JPEG = {"image/jpeg", "image/jpg", "image/pjpeg"}
_ZIP = {"application/zip", "application/x-zip-compressed", "application/x-zip", "multipart/x-zip"}
# MIME types a client may legitimately declare for each extension.
ACCEPTABLE_DECLARED_MIME: dict[str, set[str]] = {
    "pdf": {"application/pdf", "application/x-pdf"},
    "jpg": _JPEG, "jpeg": _JPEG,
    "png": {"image/png"},
    "tif": {"image/tiff", "image/x-tiff"}, "tiff": {"image/tiff", "image/x-tiff"},
    "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "doc": {"application/msword"},
    "xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    "xls": {"application/vnd.ms-excel"},
    # Windows commonly labels CSV as application/vnd.ms-excel
    "csv": {"text/csv", "application/csv", "text/plain", "application/vnd.ms-excel"},
    "txt": {"text/plain"},
    "json": {"application/json", "text/json", "text/plain"},
    "xml": {"application/xml", "text/xml"},
    "zip": _ZIP,
}
GENERIC_MIME = {"", "application/octet-stream", "binary/octet-stream"}

KIND_MIME: dict[str, str] = {
    "pdf": "application/pdf", "jpeg": "image/jpeg", "png": "image/png", "tiff": "image/tiff",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ole2": "application/x-ole-storage", "zip": "application/zip", "text": "text/plain",
}


def extension_of(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def looks_like_text(sample: bytes) -> bool:
    if not sample:
        return False
    if sample.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")):
        return True
    if b"\x00" in sample:
        return False
    control = sum(1 for b in sample if b < 0x20 and b not in (9, 10, 12, 13))
    return control / len(sample) < 0.01


def detect_kind(f: BinaryIO) -> str:
    """Return one of: pdf, jpeg, png, tiff, docx, xlsx, ooxml_other, zip, corrupt_zip, ole2, text, empty, unknown."""
    f.seek(0)
    head = f.read(SAMPLE_BYTES)
    if not head:
        return "empty"
    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        return "tiff"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole2"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        f.seek(0)
        try:
            with zipfile.ZipFile(f) as zf:
                names = zf.namelist()
        except zipfile.BadZipFile:
            return "corrupt_zip"
        if "[Content_Types].xml" in names:
            if any(n.startswith("word/") for n in names):
                return "docx"
            if any(n.startswith("xl/") for n in names):
                return "xlsx"
            return "ooxml_other"
        return "zip"
    if looks_like_text(head):
        return "text"
    return "unknown"


def extension_matches_kind(ext: str, kind: str) -> bool:
    return ALLOWED_EXTENSIONS.get(ext) == kind


def declared_mime_consistent(ext: str, declared: str | None) -> bool:
    """True when nothing was declared, a generic type was declared, or the declared type fits the extension."""
    d = (declared or "").split(";")[0].strip().lower()
    if d in GENERIC_MIME:
        return True
    return d in ACCEPTABLE_DECLARED_MIME.get(ext, set())
