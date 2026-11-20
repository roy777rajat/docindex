from cleandocs_shared.rollup import rollup


def m(*statuses):
    return [{"document_id": f"d{i}", "role": "FILE", "status": s} for i, s in enumerate(statuses)]


def test_rollup_rules():
    assert rollup(m("COMPLETED", "DUPLICATE"))["status"] == "COMPLETED"
    assert rollup(m("COMPLETED", "FAILED"))["status"] == "PARTIAL"
    assert rollup(m("PARTIAL"))["status"] == "PARTIAL"            # partial content is still usable: not FAILED
    assert rollup(m("FAILED", "REJECTED"))["status"] == "FAILED"
    assert rollup(m("COMPLETED", "PROCESSING"))["status"] == "PROCESSING"
    assert rollup([])["status"] == "PROCESSING"
