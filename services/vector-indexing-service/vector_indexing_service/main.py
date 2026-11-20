"""vector-indexing-service FastAPI app (called by Step Functions only)."""
from __future__ import annotations

from cleandocs_shared.pipeline import build_stage_app

from . import VERSION
from .logic import add_routes, run

SERVICE_NAME = "vector-indexing-service"


def create_app(settings=None, deps=None):
    return build_stage_app(SERVICE_NAME, VERSION, run, settings, deps, with_bedrock_agent=True, extra_routes=add_routes)


app = create_app()
