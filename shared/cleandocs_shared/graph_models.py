"""Knowledge-graph contracts (skeleton). Schemas only: no graph database exists in this release.

Every entity and relationship must carry evidence pointing back into the source document, so anything that is
later written to a graph stays citable.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class EntityType(str, Enum):
    PERSON = "PERSON"
    ORGANIZATION = "ORGANIZATION"
    DATE = "DATE"
    MONEY = "MONEY"
    EMAIL = "EMAIL"
    IDENTIFIER = "IDENTIFIER"


class RelationType(str, Enum):
    ISSUED_BY = "ISSUED_BY"
    BILLED_TO = "BILLED_TO"
    PAID_TO = "PAID_TO"
    DATED_ON = "DATED_ON"
    REFERENCES = "REFERENCES"
    MENTIONED_WITH = "MENTIONED_WITH"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceRef(_Strict):
    """Where in a document a fact was seen. Locators are optional because not every format has pages or sheets."""

    document_id: str
    chunk_ids: list[str] = Field(default_factory=list, description="indexed chunks that contain this evidence")
    block_id: str | None = None
    page: int | None = Field(default=None, ge=1)
    sheet: str | None = None
    section: str | None = Field(default=None, max_length=120)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    snippet: str | None = Field(default=None, max_length=160)
    archive_entry_path: str | None = None


class Entity(_Strict):
    entity_id: str
    type: EntityType
    text: str = Field(max_length=200, description="as written in the document")
    normalized: str = Field(max_length=200, description="canonical form used for grouping")
    confidence: float = Field(ge=0.0, le=1.0)
    method: str = Field(pattern="^(regex|llm|manual)$")
    evidence: list[EvidenceRef] = Field(min_length=1)


class Relationship(_Strict):
    relationship_id: str
    type: RelationType
    source_entity_id: str
    target_entity_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    method: str = Field(pattern="^(regex|llm|manual)$")
    evidence: list[EvidenceRef] = Field(min_length=1)


class GraphRequest(_Strict):
    document_id: str
    ingestion_id: str | None = None
    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)


class NotImplementedBody(_Strict):
    status: str = "NOT_IMPLEMENTED"
    operation: str
    message: str = "the knowledge graph is not implemented in this release"
