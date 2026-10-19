"""Data Connector v1: public metadata contracts, independent of Skills and transport."""
from typing import Any, Literal
from pydantic import Field, model_validator
from app.domain.models.data_candidate import CandidateInput, StrictRecord


class ConnectorOperation(StrictRecord):
    name: str = Field(pattern=r'^[a-z][a-z_]{0,63}$')
    title: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    effects: list[Literal['network', 'session_metadata', 'file_write']]
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


class ConnectorFile(StrictRecord):
    title: str = Field(default='', max_length=400)
    checksum_origin: Literal['computed_locally'] = 'computed_locally'
    file_id: str = Field(min_length=1, max_length=200)
    filename: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    size: int = Field(gt=0, le=64 * 1024 * 1024)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_url: str


class ConnectorResult(StrictRecord):
    connector_id: str
    connector_version: str
    operation: str
    kind: Literal['metadata', 'files'] = 'metadata'
    resources: list[ConnectorResource] = Field(max_length=20)
    next_cursor: str = Field(default='', max_length=4096)
    total_count: int | None = Field(default=None, ge=0)
    files_downloaded: bool = False
    files: list[ConnectorFile] = Field(default_factory=list, max_length=16)

    @model_validator(mode='after')
    def validate_download(self):
        if self.kind == 'metadata' and (self.files_downloaded or self.files):
            raise ValueError('Metadata cannot claim a download')
        if self.kind == 'files' and (not self.files_downloaded or not self.files):
            raise ValueError('A download requires persisted file receipts')
        return self


class ConnectorCall(StrictRecord):
    operation: str = Field(pattern=r'^[a-z][a-z_]{0,63}$')
    arguments: dict[str, Any]
