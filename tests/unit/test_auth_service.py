import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from pydantic import SecretStr

from cleandocs_shared.auth import Unauthorized, make_auth_dependency, verify_bearer
from cleandocs_shared.config import Settings
from cleandocs_shared.exceptions import ServiceUnavailable, ValidationFailure
from cleandocs_shared.service import create_service_app


def test_verify_bearer_accepts_only_exact_secret():
    verify_bearer("Bearer s3cret", "s3cret")
    for bad in (None, "", "s3cret", "Bearer wrong", "Basic s3cret"):
        with pytest.raises(Unauthorized):
            verify_bearer(bad, "s3cret")


def test_missing_secret_config_fails_closed():
    with pytest.raises(ServiceUnavailable):
        verify_bearer("Bearer anything", "")


def _app(secret="s3cret", ready=True):
    settings = Settings(service_name="test-service", service_version="9.9.9", service_auth_secret=SecretStr(secret))
    app = create_service_app(settings, {"dep": lambda: ready})
    auth = make_auth_dependency(lambda: settings.service_auth_secret.get_secret_value())

    @app.post("/api/v1/process", dependencies=[Depends(auth)])
    def process() -> dict:
        return {"ok": True}

    @app.get("/boom")
    def boom():
        raise ValueError("patient John Doe")

    @app.get("/known")
    def known():
        raise ValidationFailure("bad input", code="BAD")

    return TestClient(app, raise_server_exceptions=False)


def test_health_endpoints_are_open_and_info_reports_versions():
    c = _app()
    assert c.get("/health").json() == {"status": "alive"}
    assert c.get("/health/live").status_code == 200
    info = c.get("/api/v1/service/info").json()
    assert info["service_name"] == "test-service" and info["service_version"] == "9.9.9" and info["region"] == "eu-west-1"


def test_readiness_returns_503_when_a_dependency_check_fails():
    assert _app(ready=True).get("/health/ready").status_code == 200
    r = _app(ready=False).get("/health/ready")
    assert r.status_code == 503 and r.json()["checks"] == {"dep": False}


def test_process_requires_bearer_token():
    c = _app()
    assert c.post("/api/v1/process").status_code == 401
    assert c.post("/api/v1/process", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.post("/api/v1/process", headers={"Authorization": "Bearer s3cret"}).json() == {"ok": True}


def test_process_is_closed_when_secret_unset():
    c = _app(secret="")
    assert c.post("/api/v1/process", headers={"Authorization": "Bearer "}).status_code == 503


def test_errors_are_structured_and_do_not_leak_messages():
    c = _app()
    r = c.get("/boom")
    assert r.status_code == 500 and "John" not in r.text and r.json()["retryable"] is False
    k = c.get("/known")
    assert k.status_code == 422 and k.json()["error_code"] == "BAD"


def test_correlation_headers_are_propagated_or_generated():
    c = _app()
    r = c.get("/health", headers={"X-Correlation-Id": "abc", "X-Trace-Id": "tr1"})
    assert r.headers["x-correlation-id"] == "abc" and r.headers["x-trace-id"] == "tr1"
    assert c.get("/health").headers["x-correlation-id"]


def test_request_validation_errors_do_not_echo_values():
    settings = Settings(service_name="t", service_auth_secret=SecretStr("x"))
    app = create_service_app(settings)

    @app.get("/n/{n}")
    def n(n: int):
        return {"n": n}

    r = TestClient(app).get("/n/SECRET-VALUE")
    assert r.status_code == 422 and "SECRET-VALUE" not in r.text
