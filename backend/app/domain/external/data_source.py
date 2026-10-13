from typing import Protocol
from app.domain.models.source_connector import NcbiSearch, NcbiPage, NcbiObservation, NcbiTaxon


class SourceConnectorError(Exception):
    """Safe machine-readable failure; never exposes upstream response bodies."""
    def __init__(self, reason: str, retry_after_seconds: int | None = None, taxon_candidates: list[NcbiTaxon] | None = None):
        self.taxon_candidates = taxon_candidates or []
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        super().__init__(reason)


class GenomeSourceConnector(Protocol):
    async def search(self, request: NcbiSearch) -> NcbiPage: ...
    async def describe(self, accession: str) -> NcbiObservation: ...
