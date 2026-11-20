"""FastAPI application factory giving every service identical health, info and error behaviour."""
from __future__ import annotations

import uuid
from typing import Callable

from typing import Any

from boto3.dynamodb.conditions import Attr, Key
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__ as SHARED_VERSION
from .config import Settings
from .exceptions import CleanDocsError, classify_exception
from .logging_utils import bind_context, clear_context, configure_logging, get_logger
from .models import ErrorResponse, ServiceInfo

log = get_logger(__name__)

ReadinessCheck = Callable[[], bool]


def create_service_app(
    settings: Settings,
    readiness_checks: dict[str, ReadinessCheck] | None = None,
) -> FastAPI:
    configure_logging(settings.service_name, settings.service_version, settings.log_level)
    app = FastAPI(
        title=settings.service_name,
        version=settings.service_version,
        docs_url=None, redoc_url=None, openapi_url=None,  # private APIs: no public schema browser
    )
    checks = readiness_checks or {}

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        clear_context()
        correlation_id = request.headers.get("x-correlation-id") or uuid.uuid4().hex
        trace_id = request.headers.get("x-trace-id") or correlation_id
        bind_context(correlation_id=correlation_id, trace_id=trace_id, service_name=settings.service_name)
        response = await call_next(request)
        response.headers["x-correlation-id"] = correlation_id
        response.headers["x-trace-id"] = trace_id
        return response

    def _error(exc: CleanDocsError, request: Request) -> JSONResponse:
        body = ErrorResponse(
            error_code=exc.code, message=exc.message, failure_class=exc.failure_class.value,
            retryable=exc.retryable, correlation_id=request.headers.get("x-correlation-id"),
            details=exc.details,
        )
        return JSONResponse(status_code=exc.http_status, content=body.model_dump())

    @app.exception_handler(CleanDocsError)
    async def handle_known(request: Request, exc: CleanDocsError):
        return _error(exc, request)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(request: Request, exc: RequestValidationError):
        # Report field locations only; never echo submitted values (may be document content).
        fields = [".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()]
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error_code="REQUEST_INVALID", message="request validation failed",
                failure_class="VALIDATION", retryable=False,
                correlation_id=request.headers.get("x-correlation-id"), details={"fields": fields},
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def handle_unknown(request: Request, exc: Exception):
        err = classify_exception(exc)
        log.error("unhandled exception", exc_info=exc)
        return _error(err, request)

    @app.get("/health")
    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready() -> JSONResponse:
        results: dict[str, bool] = {}
        for name, check in checks.items():
            try:
                results[name] = bool(check())
            except Exception:  # noqa: BLE001
                results[name] = False
        ok = all(results.values())
        return JSONResponse(status_code=200 if ok else 503, content={"status": "ready" if ok else "not_ready", "checks": results})

    @app.get("/api/v1/service/info")
    def info() -> ServiceInfo:
        return ServiceInfo(
            service_name=settings.service_name, service_version=settings.service_version,
            shared_version=SHARED_VERSION, region=settings.aws_region,
            render_instance_id=settings.render_instance_id,
        )

    return app


def add_executions_endpoint(app: FastAPI, trail_table_getter: Callable[[], Any], auth_dependency: Callable[..., None], service_name: str) -> None:
    """GET /api/v1/executions/{execution_id}?document_id=... returns this service's audit rows for one execution.

    The trail is partitioned by document, so the document id is required (or ingestion_id for ingestion-level runs).
    """
    from .utils import doc_pk, from_dynamo, ingestion_pk
    from .exceptions import NotFound, ValidationFailure

    @app.get("/api/v1/executions/{execution_id}", dependencies=[Depends(auth_dependency)])
    def get_execution(execution_id: str, document_id: str | None = None, ingestion_id: str | None = None) -> dict:
        if not execution_id.startswith(f"{service_name}_"):
            raise ValidationFailure("execution id does not belong to this service", code="WRONG_SERVICE")
        if not document_id and not ingestion_id:
            raise ValidationFailure("document_id or ingestion_id is required", code="PARTITION_REQUIRED")
        pk = doc_pk(document_id) if document_id else ingestion_pk(ingestion_id or "")
        resp = trail_table_getter().query(
            KeyConditionExpression=Key("PK").eq(pk),
            FilterExpression=Attr("service_execution_id").eq(execution_id),
        )
        rows = [from_dynamo(i) for i in resp.get("Items", [])]
        if not rows:
            raise NotFound("execution not found", code="EXECUTION_NOT_FOUND")
        return {"service_execution_id": execution_id, "events": rows}


def ttl_cached(check: Callable[[], bool], ttl_seconds: float = 30.0) -> Callable[[], bool]:
    """Wrap a readiness check so repeated /health/ready polls do not each hit AWS."""
    import time

    state: dict[str, Any] = {"at": 0.0, "value": False}

    def wrapped() -> bool:
        now = time.monotonic()
        if now - state["at"] >= ttl_seconds or not state["value"]:
            state["value"] = bool(check())
            state["at"] = now
        return state["value"]

    return wrapped
