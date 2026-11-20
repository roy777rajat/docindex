"""archive-service FastAPI app (called by Step Functions only)."""
from __future__ import annotations

from fastapi import Depends, FastAPI

from cleandocs_shared import aws
from cleandocs_shared.audit import AuditWriter
from cleandocs_shared.auth import make_auth_dependency
from cleandocs_shared.config import Settings
from cleandocs_shared.models import ProcessRequest, ProcessResponse
from cleandocs_shared.registry import Registry
from cleandocs_shared.service import add_executions_endpoint, create_service_app, ttl_cached

from . import VERSION
from .logic import ArchiveDeps, run_extraction

SERVICE_NAME = "archive-service"


def create_app(settings: Settings | None = None, deps: ArchiveDeps | None = None) -> FastAPI:
    settings = settings or Settings(service_name=SERVICE_NAME, service_version=VERSION)
    if deps is None:
        deps = ArchiveDeps(
            settings=settings, s3=aws.s3_client(settings), registry=Registry(aws.registry_table(settings)),
            audit=AuditWriter(aws.trail_table(settings), SERVICE_NAME, VERSION, settings.render_instance_id),
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

    @app.post("/api/v1/process", response_model=ProcessResponse, dependencies=[Depends(auth)])
    def process(req: ProcessRequest) -> ProcessResponse:
        return run_extraction(d, req.context, req.input_s3_uri)

    return app


app = create_app()
