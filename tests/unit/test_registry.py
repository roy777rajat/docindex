from cleandocs_shared.registry import Registry


def _doc(reg, doc="doc_1", ing="ing_1", **kw):
    return reg.create_document(
        document_id=doc, ingestion_id=ing, original_filename="a.pdf", extension="pdf", size_bytes=10,
        checksum_sha256="a" * 64, s3_uri_original="s3://b/raw/x", **kw,
    )


def test_create_is_idempotent_and_get_returns_python_types(registry_table):
    reg = Registry(registry_table)
    assert _doc(reg) is True
    assert _doc(reg) is False
    d = reg.get_document("doc_1")
    assert d["status"] == "PROCESSING" and d["stage"] == "UPLOADED" and d["size_bytes"] == 10
    assert d["graph_status"] == "NOT_CONFIGURED" and reg.get_document("nope") is None


def test_update_document_sets_fields_and_refuses_phantoms(registry_table):
    reg = Registry(registry_table)
    _doc(reg)
    reg.update_document("doc_1", stage="VALIDATED", status="REJECTED", rejection_code="X")  # reserved words work
    d = reg.get_document("doc_1")
    assert d["stage"] == "VALIDATED" and d["status"] == "REJECTED" and d["rejection_code"] == "X"
    try:
        reg.update_document("ghost", stage="X")
        raise AssertionError("expected failure")
    except Exception as e:  # noqa: BLE001
        assert "ConditionalCheckFailed" in str(e)


def test_members_roundtrip_and_updates(registry_table):
    reg = Registry(registry_table)
    assert reg.create_ingestion(ingestion_id="ing_1", trace_id="t")
    assert reg.add_member(ingestion_id="ing_1", document_id="doc_a", role="ARCHIVE")
    assert reg.add_member(ingestion_id="ing_1", document_id="doc_b", parent_document_id="doc_a", entry_path="x/y.pdf")
    assert not reg.add_member(ingestion_id="ing_1", document_id="doc_b")
    reg.update_member("ing_1", "doc_b", status="COMPLETED")
    m = {x["document_id"]: x for x in reg.list_members("ing_1")}
    assert set(m) == {"doc_a", "doc_b"} and m["doc_a"]["role"] == "ARCHIVE"
    assert m["doc_b"]["status"] == "COMPLETED" and m["doc_b"]["entry_path"] == "x/y.pdf"


def test_idempotency_key_returns_first_response(registry_table):
    reg = Registry(registry_table)
    assert reg.get_idempotent_response("k1") is None
    assert reg.store_idempotent_response("k1", {"document_id": "doc_1", "size": 5})
    assert not reg.store_idempotent_response("k1", {"document_id": "doc_2"})
    assert reg.get_idempotent_response("k1") == {"document_id": "doc_1", "size": 5}
