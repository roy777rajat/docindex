import io
import re

import pytest

from cleandocs_shared import utils


def test_sha256_stream_matches_bytes_and_counts_size():
    data = b"hello world" * 100_000
    digest, size = utils.sha256_stream(io.BytesIO(data))
    assert digest == utils.sha256_bytes(data)
    assert size == len(data)


def test_sha256_stream_accepts_chunk_iterables():
    digest, size = utils.sha256_stream(iter([b"ab", b"cd"]))
    assert digest == utils.sha256_bytes(b"abcd") and size == 4


def test_execution_id_format_includes_service_name():
    eid = utils.new_execution_id("validation-service")
    assert re.fullmatch(r"validation-service_\d{8}T\d{6}Z_[0-9a-f]{6}", eid)


def test_ids_are_unique_and_prefixed():
    assert utils.new_document_id().startswith("doc_")
    assert utils.new_ingestion_id().startswith("ing_")
    assert utils.new_document_id() != utils.new_document_id()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\scan 1.pdf", "scan_1.pdf"),
        ("report (final).PDF", "report_final_.PDF"),
        ("", "file"),
        ("...", "file"),
    ],
)
def test_safe_filename_strips_paths_and_unsafe_chars(raw, expected):
    assert utils.safe_filename(raw) == expected


def test_safe_filename_truncates_but_keeps_extension():
    name = utils.safe_filename("a" * 500 + ".pdf")
    assert len(name) <= 120 and name.endswith(".pdf")


def test_s3_uri_roundtrip_and_rejects_bad_uris():
    uri = utils.s3_uri("bkt", "raw/documents/doc_1/original/a.pdf")
    assert utils.parse_s3_uri(uri) == ("bkt", "raw/documents/doc_1/original/a.pdf")
    for bad in ("http://x/y", "s3://bucket-only", "s3:///key"):
        with pytest.raises(ValueError):
            utils.parse_s3_uri(bad)


def test_key_layout_matches_design():
    assert utils.raw_original_key("doc_1", "a b.pdf") == "raw/documents/doc_1/original/a_b.pdf"
    assert utils.kb_chunk_key("doc_1", "c1") == "kb-source/doc_1/c1.txt"
    assert utils.kb_chunk_metadata_key("doc_1", "c1") == "kb-source/doc_1/c1.txt.metadata.json"
    assert utils.processed_key("doc_1", "extraction/raw", "x.json") == "processed/documents/doc_1/extraction/raw/x.json"
