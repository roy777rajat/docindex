"""manifest-service FastAPI app (called by Step Functions only)."""
from __future__ import annotations

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict

from cleandocs_shared import aws
from cleandocs_shared.audit import AuditWriter
from cleandocs_shared.auth import make_auth_dependency
from cleandocs_shared.config import Settings
from cleandocs_shared.models import ProcessRequest, ProcessResponse
from cleandocs_shared.registry import Registry
from cleandocs_shared.service import add_executions_endpoint, create_service_app, ttl_cached

from . import VERSION
from .logic import ManifestDeps, ingestion_status, list_files, set_state

SERVICE_NAME = "manifest-service"


class StateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    stage: str | None = None
    error_code: str | None = None


def create_app(settings: Settings | None = None, deps: ManifestDeps | None = None) -> FastAPI:
    settings = settings or Settings(service_name=SERVICE_NAME, service_version=VERSION)
    if deps is None:
        deps = ManifestDeps(settings=settings, registry=Registry(aws.registry_table(settings)),
                            audit=AuditWriter(aws.trail_table(settings), SERVICE_NAME, VERSION, settings.render_instance_id))
    d = deps
    app = create_service_app(settings, {"registry": ttl_cached(lambda: d.registry._t.table_status in ("ACTIVE", "UPDATING"))})  # noqa: SLF001
    auth = make_auth_dependency(lambda: settings.service_auth_secret.get_secret_value())
    add_executions_endpoint(app, lambda: d.audit._table, auth, SERVICE_NAME)  # noqa: SLF001

    @app.post("/api/v1/process", response_model=ProcessResponse, dependencies=[Depends(auth)])
    def process(req: ProcessRequest) -> ProcessResponse:
        return list_files(d, req.context)

    @app.post("/api/v1/ingestions/{ingestion_id}/documents/{document_id}/state", dependencies=[Depends(auth)])
    def state(ingestion_id: str, document_id: str, body: StateUpdate) -> dict:
        return set_state(d, ingestion_id, document_id, body.status, body.stage, body.error_code)

    @app.get("/api/v1/ingestions/{ingestion_id}", dependencies=[Depends(auth)])
    def status(ingestion_id: str) -> dict:
        return ingestion_status(d, ingestion_id)

    return app


app = create_app()
