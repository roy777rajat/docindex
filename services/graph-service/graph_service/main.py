"""graph-service FastAPI app: contract skeleton. Every graph operation answers 501 NOT_IMPLEMENTED."""
from __future__ import annotations

from cleandocs_shared.pipeline import build_stage_app

from . import VERSION
from .logic import add_routes, run

SERVICE_NAME = "graph-service"


def create_app(settings=None, deps=None):
    return build_stage_app(SERVICE_NAME, VERSION, run, settings, deps, extra_routes=add_routes)


app = create_app()
