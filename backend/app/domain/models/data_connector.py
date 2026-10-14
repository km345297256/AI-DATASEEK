"""Data Connector v1: public metadata contracts, independent of Skills and transport."""
from typing import Any, Literal
from pydantic import Field
from app.domain.models.data_candidate import CandidateInput, StrictRecord


class ConnectorOperation(StrictRecord):
    name: str = Field(pattern=r'^[a-z][a-z_]{0,63}$')
    title: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    effects: list[Literal['network', 'session_metadata']]
    timeout_seconds: int = Field(ge=1, le=60)


class ConnectorManifest(StrictRecord):
    schema_version: Literal['data-connector-v1'] = 'data-connector-v1'
    id: str = Field(pattern=r'^[a-z][a-z0-9-]{0,63}$')
    version: str = Field(pattern=r'^\d+\.\d+\.\d+$')
    title: str
    description: str
    transport: Literal['native', 'mcp', 'http']
    auth: Literal['none', 'oauth2', 'api_key']
    domains: list[str]
    operations: list[ConnectorOperation]
    limitations: list[str]


class ConnectorResource(CandidateInput):
    """A metadata result, not a downloaded artifact. URLs are public references only."""
    id: str = Field(min_length=1, max_length=200)
    candidate_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    retrieved_at: int = Field(ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConnectorResult(StrictRecord):
    connector_id: str
    connector_version: str
    operation: str
    kind: Literal['metadata'] = 'metadata'
    resources: list[ConnectorResource] = Field(max_length=20)
    next_cursor: str = Field(default='', max_length=4096)
    total_count: int | None = Field(default=None, ge=0)
    files_downloaded: Literal[False] = False


class ConnectorCall(StrictRecord):
    operation: str = Field(pattern=r'^[a-z][a-z_]{0,63}$')
    arguments: dict[str, Any]
