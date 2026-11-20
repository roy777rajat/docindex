"""Pure validation checks on a seekable binary file. Raises CleanDocsError subclasses on rejection.

Never trusts the extension: the detected content kind must agree with it, and a declared MIME type
must be consistent with both. Content is never logged or echoed in error messages.
"""
from __future__ import annotations

import json
import zipfile
from typing import Any, BinaryIO

from cleandocs_shared.config import Settings
from cleandocs_shared.exceptions import UnsupportedFormat, ValidationFailure
from cleandocs_shared.filetypes import (
    ALLOWED_EXTENSIONS, KIND_MIME, declared_mime_consistent, detect_kind, extension_matches_kind, extension_of,
)

_JSON_PARSE_LIMIT = 10 * 1024 * 1024  # larger JSON is only sniffed, not fully parsed (memory on small instances)


def check_pdf(f: BinaryIO, settings: Settings) -> dict[str, Any]:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    f.seek(0)
    try:
        reader = PdfReader(f, strict=False)
        if reader.is_encrypted:
            raise ValidationFailure("password-protected PDFs are not supported", code="ENCRYPTED_DOCUMENT")
        pages = len(reader.pages)
    except ValidationFailure:
        raise
    except (PyPdfError, ValueError, KeyError, OSError, RecursionError) as exc:
        raise ValidationFailure("PDF structure is corrupt", code="CORRUPT_FILE") from exc
    if pages == 0:
        raise ValidationFailure("PDF has no pages", code="CORRUPT_FILE")
    if pages > settings.max_pdf_pages:
        raise ValidationFailure(
            f"PDF has {pages} pages, limit is {settings.max_pdf_pages}", code="PAGE_LIMIT_EXCEEDED",
            details={"pages": pages, "limit": settings.max_pdf_pages},
        )
    return {"pages": pages}


def check_image(f: BinaryIO, kind: str, settings: Settings) -> dict[str, Any]:
    from PIL import Image, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = settings.max_image_pixels  # decompression-bomb guard
    expected = {"jpeg": "JPEG", "png": "PNG", "tiff": "TIFF"}[kind]
    f.seek(0)
    try:
        with Image.open(f) as img:
            fmt, (w, h) = img.format, img.size
            if fmt != expected:
                raise ValidationFailure("image format disagrees with its signature", code="SIGNATURE_MISMATCH")
            if w * h > settings.max_image_pixels:
                raise ValidationFailure("image dimensions too large", code="IMAGE_TOO_LARGE")
            frames = getattr(img, "n_frames", 1)
        f.seek(0)
        with Image.open(f) as img:
            img.verify()
    except ValidationFailure:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError) as exc:
        raise ValidationFailure("image is corrupt or unreadable", code="CORRUPT_FILE") from exc
    return {"width": w, "height": h, "frames": frames}


def check_ooxml(f: BinaryIO, settings: Settings) -> dict[str, Any]:
    """docx/xlsx are zip containers: verify integrity and guard against expansion bombs."""
    f.seek(0)
    try:
        with zipfile.ZipFile(f) as zf:
            infos = zf.infolist()
            if len(infos) > 5000:
                raise ValidationFailure("document container has too many parts", code="ARCHIVE_LIMIT_EXCEEDED")
            total = sum(i.file_size for i in infos)
            if total > settings.max_archive_expanded_bytes:
                raise ValidationFailure("document expands beyond the allowed size", code="ARCHIVE_LIMIT_EXCEEDED")
            if zf.testzip() is not None:
                raise ValidationFailure("document container failed its integrity check", code="CORRUPT_FILE")
    except zipfile.BadZipFile as exc:
        raise ValidationFailure("document container is corrupt", code="CORRUPT_FILE") from exc
    return {"parts": len(infos)}


def check_zip(f: BinaryIO, settings: Settings) -> dict[str, Any]:
    """Cheap early rejection only; archive-service enforces the full set of rules while extracting."""
    f.seek(0)
    try:
        with zipfile.ZipFile(f) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise ValidationFailure("archive is corrupt", code="CORRUPT_FILE") from exc
    files = [i for i in infos if not i.is_dir()]
    if any(i.flag_bits & 0x1 for i in files):
        raise ValidationFailure("encrypted archives are not supported", code="ENCRYPTED_ARCHIVE")
    if len(files) > settings.max_archive_file_count:
        raise ValidationFailure(
            f"archive has {len(files)} files, limit is {settings.max_archive_file_count}", code="ARCHIVE_LIMIT_EXCEEDED"
        )
    if sum(i.file_size for i in files) > settings.max_archive_expanded_bytes:
        raise ValidationFailure("archive expands beyond the allowed size", code="ARCHIVE_LIMIT_EXCEEDED")
    return {"entries": len(files)}


def check_json(f: BinaryIO, size: int) -> dict[str, Any]:
    f.seek(0)
    try:
        if size <= _JSON_PARSE_LIMIT:
            json.loads(f.read().decode("utf-8-sig"))
        else:
            first = f.read(1).decode("utf-8", "ignore").strip()
            if first not in ("{", "["):
                raise ValueError
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ValidationFailure("file is not valid JSON", code="INVALID_JSON") from exc
    return {}


def check_xml(f: BinaryIO) -> dict[str, Any]:
    from defusedxml import DefusedXmlException
    from defusedxml.ElementTree import ParseError, iterparse

    f.seek(0)
    try:
        for _ in iterparse(f, events=("end",), forbid_dtd=True):  # streams; rejects DTDs/entities (XXE, billion laughs)
            pass
    except DefusedXmlException as exc:
        raise ValidationFailure("XML contains forbidden constructs (DTD/entities)", code="UNSAFE_XML") from exc
    except (ParseError, ValueError) as exc:
        raise ValidationFailure("file is not well-formed XML", code="INVALID_XML") from exc
    return {}


def check_text(f: BinaryIO) -> dict[str, Any]:
    """csv/txt: must be text. UTF-8/UTF-16 are accepted; legacy single-byte encodings are tolerated."""
    f.seek(0)
    head = f.read(65536)
    if b"\x00" in head and not head.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValidationFailure("file contains binary data", code="NOT_TEXT")
    return {}


def validate_content(f: BinaryIO, *, filename: str, declared_mime: str | None, size: int, settings: Settings) -> dict[str, Any]:
    """Run all content checks. Returns a result dict (no content) or raises."""
    if size > settings.max_file_size_bytes:
        raise ValidationFailure(f"file exceeds the {settings.max_file_size_mb} MB limit", code="FILE_TOO_LARGE")
    ext = extension_of(filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise UnsupportedFormat(f"extension '.{ext}' is not supported" if ext else "file has no extension",
                                code="EXTENSION_NOT_ALLOWED")
    kind = detect_kind(f)
    if kind == "empty":
        raise ValidationFailure("file is empty", code="EMPTY_FILE")
    if kind == "corrupt_zip":
        raise ValidationFailure("container is corrupt", code="CORRUPT_FILE")
    if not extension_matches_kind(ext, kind):
        raise ValidationFailure(
            "file content does not match its extension", code="SIGNATURE_MISMATCH",
            details={"extension": ext, "detected_kind": kind},
        )
    if not declared_mime_consistent(ext, declared_mime):
        raise ValidationFailure(
            "declared content type is inconsistent with the file", code="MIME_MISMATCH",
            details={"extension": ext, "declared": (declared_mime or "")[:100]},
        )

    details: dict[str, Any]
    if kind == "pdf":
        details = check_pdf(f, settings)
    elif kind in ("jpeg", "png", "tiff"):
        details = check_image(f, kind, settings)
    elif kind in ("docx", "xlsx"):
        details = check_ooxml(f, settings)
    elif kind == "zip":
        details = check_zip(f, settings)
    elif kind == "ole2":  # legacy Office (.doc/.xls): signature is valid but no parser exists, so reject now rather than fail later
        raise UnsupportedFormat("legacy Office files (.doc/.xls) are not supported; save as .docx or .xlsx",
                                code="LEGACY_OFFICE_UNSUPPORTED")
    elif ext == "json":
        details = check_json(f, size)
    elif ext == "xml":
        details = check_xml(f)
    else:
        details = check_text(f)

    mime = {"json": "application/json", "xml": "application/xml", "csv": "text/csv"}.get(ext) if kind == "text" else None
    return {
        "file_kind": ext if kind == "text" else kind,
        "detected_mime": mime or KIND_MIME.get(kind, "application/octet-stream"),
        "extension": ext,
        "is_archive": kind == "zip",
        **details,
    }
