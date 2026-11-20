import io
import json
import logging

from cleandocs_shared import logging_utils as lu


def _capture():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(lu.JsonFormatter("svc", "1.2.3"))
    logger = logging.getLogger("test.capture")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return lu.get_logger("test.capture"), stream


def test_log_lines_are_json_with_bound_identifiers():
    lu.clear_context()
    lu.bind_context(document_id="doc_1", trace_id="t1", ingestion_id=None)
    log, stream = _capture()
    log.info("hello", fields={"duration_ms": 5})
    line = json.loads(stream.getvalue())
    assert line["message"] == "hello" and line["document_id"] == "doc_1" and line["trace_id"] == "t1"
    assert line["service_name"] == "svc" and line["duration_ms"] == 5
    assert "ingestion_id" not in line  # None values are not bound
    lu.clear_context()


def test_document_content_and_secrets_are_redacted():
    log, stream = _capture()
    log.info("x", fields={"text": "Patient John Doe HbA1c 7.2", "nested": {"authorization": "Bearer abc"}, "size": 10})
    out = stream.getvalue()
    assert "John" not in out and "Bearer abc" not in out
    line = json.loads(out)
    assert line["text"] == "[REDACTED]" and line["nested"]["authorization"] == "[REDACTED]" and line["size"] == 10


def test_exception_message_is_not_logged():
    log, stream = _capture()
    try:
        raise ValueError("account number 12345678")
    except ValueError:
        log.error("failed", exc_info=True)
    out = stream.getvalue()
    assert json.loads(out)["exception_type"] == "ValueError"
    assert "12345678" not in json.loads(out).get("message", "")
