"""ZIP safety rules (pure functions). Fatal problems reject the whole archive; per-entry problems reject
only that entry. Nothing here ever extracts to a filesystem path: entries are streamed to temp files and
S3, so path traversal can only ever be *detected*, never *exploited*."""
from __future__ import annotations

import re
import uuid
import zipfile

from cleandocs_shared.exceptions import ValidationFailure

IGNORED_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
IGNORED_PREFIXES = ("__macosx/",)
MIN_RATIO_CHECK_BYTES = 100 * 1024  # tiny files may compress extremely well legitimately
_NAMESPACE = uuid.UUID("6f1d8e0a-4c52-4c3b-9d0e-3a8f9b2c7e11")
_DRIVE = re.compile(r"^[A-Za-z]:")


class ArchiveFatal(ValidationFailure):
    """The entire archive must be rejected."""


def normalize_entry_path(name: str) -> str:
    """Return a safe relative path, or raise ArchiveFatal (ZIP Slip / absolute path / NUL / drive letter)."""
    if "\x00" in name:
        raise ArchiveFatal("archive entry name contains NUL", code="UNSAFE_ENTRY_PATH")
    p = name.replace("\\", "/")
    if p.startswith("/") or _DRIVE.match(p):
        raise ArchiveFatal("archive entry has an absolute path", code="UNSAFE_ENTRY_PATH")
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        raise ArchiveFatal("archive entry escapes its directory (path traversal)", code="UNSAFE_ENTRY_PATH")
    return "/".join(parts)


def is_ignored(path: str) -> bool:
    low = path.lower()
    return low.startswith(IGNORED_PREFIXES) or low.rsplit("/", 1)[-1] in IGNORED_NAMES


def is_symlink(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def child_document_id(parent_document_id: str, entry_path: str) -> str:
    """Deterministic: a retried extraction yields the same ids, so every write is idempotent."""
    return "doc_" + uuid.uuid5(_NAMESPACE, f"{parent_document_id}:{entry_path}").hex


def prescan(infos: list[zipfile.ZipInfo], *, max_files: int, max_expanded: int, max_file_size: int, max_ratio: int) -> list[zipfile.ZipInfo]:
    """Header-level checks over the central directory. Returns the file entries (directories dropped)."""
    files = [i for i in infos if not i.is_dir()]
    if len(files) > max_files:
        raise ArchiveFatal(f"archive has {len(files)} files, limit is {max_files}", code="ARCHIVE_LIMIT_EXCEEDED")
    declared = sum(i.file_size for i in files)
    if declared > max_expanded:
        raise ArchiveFatal("archive would expand beyond the allowed total size", code="ARCHIVE_LIMIT_EXCEEDED")
    for i in files:
        normalize_entry_path(i.filename)
        if is_symlink(i):
            raise ArchiveFatal("archive contains a symbolic link", code="UNSAFE_ENTRY_TYPE")
        if i.flag_bits & 0x1:
            raise ArchiveFatal("archive contains encrypted entries", code="ENCRYPTED_ARCHIVE")
        if max_ratio and MIN_RATIO_CHECK_BYTES < i.file_size <= max_file_size and i.file_size / max(i.compress_size, 1) > max_ratio:
            raise ArchiveFatal("suspicious compression ratio (possible decompression bomb)", code="BOMB_SUSPECTED")
    return files
