"""extraction-service FastAPI app (called by Step Functions only)."""
from __future__ import annotations

from cleandocs_shared.pipeline import build_stage_app

from . import VERSION
from .logic import run

SERVICE_NAME = "extraction-service"


def create_app(settings=None, deps=None):
    return build_stage_app(SERVICE_NAME, VERSION, run, settings, deps, with_textract=True)


app = create_app()
