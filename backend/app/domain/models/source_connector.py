"""Bounded contracts for the first scientific source connector (metadata only)."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

Accession = Annotated[str, Field(pattern=r"^GC[AF]_[0-9]{9}\.[1-9][0-9]*$", max_length=32)]
AssemblySource = Literal['refseq', 'genbank', 'all']
AssemblyLevel = Literal['complete_genome', 'chromosome', 'scaffold', 'contig', 'all']


class SourceRecord(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)


class NcbiSearch(SourceRecord):
    taxon: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._'()-]*$")
    assembly_source: AssemblySource = 'refseq'
    assembly_level: AssemblyLevel = 'complete_genome'
    reference_only: bool = Field(default=False, strict=True)
    page_size: int = Field(default=5, ge=1, le=20, strict=True)
    page_token: str = Field(default='', max_length=4096)

    @field_validator('taxon')
    @classmethod
    def valid_numeric_taxon(cls, value):
        if value.isdigit() and not 0 < int(value) < 2**31:
            raise ValueError('Invalid taxonomy ID')
        return value

    @field_validator('page_token')
    @classmethod
    def bounded_token(cls, value):
        if any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise ValueError('Invalid page token')
        return value


class NcbiTaxon(SourceRecord):
    tax_id: int = Field(gt=0, lt=2**31, strict=True)
    scientific_name: str = Field(min_length=1, max_length=300)
    rank: str = Field(max_length=60)
    domain: str | None = Field(default=None, max_length=100)


class NcbiAssembly(SourceRecord):
    accession: Accession
    current_accession: Accession | None = None
    source_database: Literal['refseq', 'genbank']
    organism_name: str = Field(min_length=1, max_length=300)
    tax_id: int = Field(gt=0, strict=True)
    assembly_level: Literal['complete_genome', 'chromosome', 'scaffold', 'contig']
    reference_genome: bool | None
    assembly_status: str = Field(max_length=40)
    total_sequence_length: int | None = Field(default=None, ge=0)
    gc_percent: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)


class NcbiObservation(SourceRecord):
    connector: Literal['ncbi-datasets-v2'] = 'ncbi-datasets-v2'
    connector_version: Literal['1.0.0'] = '1.0.0'
    retrieved_at: int
    resolved_taxon_id: int | None = Field(default=None, gt=0)
    resolved_taxon_name: str | None = Field(default=None, max_length=300)
    taxonomy_report_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    report_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    # Retain the literal query/filters; pagination token is represented by a hash.
    query: NcbiSearch | None = None
    page_token_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    assembly: NcbiAssembly
    evidence: Literal['official_metadata_only'] = 'official_metadata_only'


class NcbiPage(SourceRecord):
    observations: list[NcbiObservation]
    total_count: int | None = Field(default=None, ge=0)
    next_page_token: str = Field(default='', max_length=4096)


class AcquisitionPlan(SourceRecord):
    candidate_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    accession: Accession
    metadata_sha256: str
    source_url: str
    format: Literal['genomic_fasta'] = 'genomic_fasta'
    reported_sequence_bases: int | None
    download_size_bytes: None = None
    transfer_limit_bytes: int = 100 * 1024 * 1024
    status: Literal['not_started'] = 'not_started'
    execution_available: Literal[False] = False
    reason: Literal['durable_acquisition_worker_pending'] = 'durable_acquisition_worker_pending'
