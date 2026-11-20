"""AWS client factories. No credentials in code: the default boto3 chain is used
(named profile locally, environment variables on Render)."""
from __future__ import annotations

from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

from .config import Settings, get_settings


def _config(settings: Settings, read_timeout: int = 30) -> Config:
    return Config(
        region_name=settings.aws_region,
        retries={"max_attempts": 5, "mode": "standard"},
        connect_timeout=5,
        read_timeout=read_timeout,
    )


@lru_cache(maxsize=1)
def _session() -> boto3.session.Session:
    return boto3.session.Session(region_name=get_settings().aws_region)


def s3_client(settings: Settings | None = None) -> Any:
    s = settings or get_settings()
    return _session().client("s3", config=_config(s, 60))


def sqs_client(settings: Settings | None = None) -> Any:
    s = settings or get_settings()
    return _session().client("sqs", config=_config(s))


def sfn_client(settings: Settings | None = None) -> Any:
    s = settings or get_settings()
    return _session().client("stepfunctions", config=_config(s))


def textract_client(settings: Settings | None = None) -> Any:
    s = settings or get_settings()
    return _session().client("textract", config=_config(s, 60))


def bedrock_runtime_client(settings: Settings | None = None) -> Any:
    s = settings or get_settings()
    return _session().client("bedrock-runtime", config=_config(s, 120))


def bedrock_agent_client(settings: Settings | None = None) -> Any:
    s = settings or get_settings()
    return _session().client("bedrock-agent", config=_config(s))


def dynamodb_table(table_name: str, settings: Settings | None = None) -> Any:
    s = settings or get_settings()
    return _session().resource("dynamodb", config=_config(s)).Table(table_name)


def trail_table(settings: Settings | None = None) -> Any:
    s = settings or get_settings()
    return dynamodb_table(s.dynamodb_ingest_trail_table, s)


def registry_table(settings: Settings | None = None) -> Any:
    s = settings or get_settings()
    return dynamodb_table(s.dynamodb_registry_table, s)
