"""Externalized configuration. Everything environment-specific comes from env vars."""
from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProcessingMode(str, Enum):
    SEQUENTIAL = "SEQUENTIAL"  # one file workflow at a time, globally
    PER_INGESTION = "PER_INGESTION"  # one at a time per ingestion
    CONCURRENT = "CONCURRENT"  # up to MAX_CONCURRENT_FILES


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # identity of the running service (set per service)
    service_name: str = "unknown-service"
    service_version: str = "0.0.0"
    render_instance_id: str | None = Field(default=None, alias="RENDER_INSTANCE_ID")
    log_level: str = "INFO"

    # AWS
    aws_region: str = "eu-west-1"
    s3_bucket: str = ""
    dynamodb_registry_table: str = "cleandocs-registry"
    dynamodb_ingest_trail_table: str = "document-ingest-trail"
    sqs_ingest_queue_url: str = ""
    sqs_ingest_dlq_url: str = ""
    step_functions_ingestion_arn: str = ""
    step_functions_file_processing_arn: str = ""
    bedrock_knowledge_base_id: str = ""
    bedrock_data_source_id: str = ""
    embedding_model_id: str = "amazon.titan-embed-text-v2:0"
    classifier_model_id: str = ""
    enrichment_model_id: str = "eu.amazon.nova-micro-v1:0"  # EU inference profile (on-demand ids are not offered in eu-west-1)
    enrichment_monthly_doc_cap: int = 1000

    # limits (home-lab defaults; Render Free has 512 MB RAM)
    max_file_size_mb: int = 25
    max_archive_expanded_size_mb: int = 100
    max_archive_file_count: int = 50
    max_archive_depth: int = 0
    max_archive_compression_ratio: int = 100
    max_pdf_pages: int = 300
    max_image_pixels: int = 100_000_000
    chunk_target_chars: int = 1200
    chunk_max_chars: int = 1800
    chunk_overlap_chars: int = 150

    # processing control
    processing_mode: ProcessingMode = ProcessingMode.SEQUENTIAL
    max_concurrent_files: int = 1
    gate_lease_seconds: int = 3600

    # cost guards (0 disables the feature)
    textract_monthly_page_cap: int = 300
    bedrock_monthly_token_cap: int = 500_000
    ocr_enabled: bool = True
    ocr_pages_per_call: int = 4  # keeps each request well under Step Functions' 60 s HTTP Task cap
    ai_enrichment_enabled: bool = True

    # service-to-service authentication (never commit a real value)
    service_auth_secret: SecretStr = SecretStr("")

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def max_archive_expanded_bytes(self) -> int:
        return self.max_archive_expanded_size_mb * 1024 * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
