"""upload-service FastAPI app. The only service a human client calls directly."""
from __future__ import annotations

from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile
from pydantic import BaseModel

from cleandocs_shared import aws
from cleandocs_shared.audit import AuditWriter
from cleandocs_shared.auth import make_auth_dependency
from cleandocs_shared.config import Settings
from cleandocs_shared.registry import Registry
from cleandocs_shared.service import add_executions_endpoint, create_service_app, ttl_cached
from cleandocs_shared.utils import new_trace_id

from . import VERSION
from .logic import UploadDeps, handle_upload

SERVICE_NAME = "upload-service"


class UploadResponse(BaseModel):
    ingestion_id: str
    document_id: str
    filename: str
    size_bytes: int
    checksum_sha256: str
    s3_uri: str
    workflow_started: bool
    workflow_execution_arn: str | None = None
    trace_id: str
    idempotent_replay: bool = False


def create_app(settings: Settings | None = None, deps: UploadDeps | None = None) -> FastAPI:
    settings = settings or Settings(service_name=SERVICE_NAME, service_version=VERSION)
    if deps is None:
        deps = UploadDeps(
            settings=settings,
            s3=aws.s3_client(settings),
            registry=Registry(aws.registry_table(settings)),
            audit=AuditWriter(aws.trail_table(settings), SERVICE_NAME, VERSION, settings.render_instance_id),
            sfn=aws.sfn_client(settings),
        )
    d = deps

    def s3_ok() -> bool:
        d.s3.head_bucket(Bucket=settings.s3_bucket)
        return True

    def registry_ok() -> bool:
        return d.registry._t.table_status in ("ACTIVE", "UPDATING")  # noqa: SLF001

    app = create_service_app(settings, {"s3": ttl_cached(s3_ok), "registry": ttl_cached(registry_ok)})
    auth = make_auth_dependency(lambda: settings.service_auth_secret.get_secret_value())
    add_executions_endpoint(app, lambda: d.audit._table, auth, SERVICE_NAME)  # noqa: SLF001

    @app.post("/api/v1/uploads", status_code=202, response_model=UploadResponse, dependencies=[Depends(auth)])
    def upload(
        request: Request,
        file: UploadFile = File(...),
        label: str | None = Form(default=None, max_length=200),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        trace_id = request.headers.get("x-trace-id") or new_trace_id()
        return handle_upload(
            d, fileobj=file.file, filename=file.filename or "", content_type=file.content_type,
            idempotency_key=idempotency_key, label=label, trace_id=trace_id,
        )

    return app


app = create_app()  # uvicorn entrypoint; AWS clients are created lazily, readiness reports missing config
