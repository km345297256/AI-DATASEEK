"""Public provenance receipts; never contain storage paths or caller assertions of trust."""
import hashlib
import ipaddress
import json
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

OpaqueId = Annotated[str, Field(pattern=r"^(?:(?:minio|gridfs):)?[A-Za-z0-9_-]{1,128}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def identity(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class CandidateInput(StrictRecord):
    title: str = Field(min_length=1, max_length=200)
    source_url: str = Field(min_length=1, max_length=2048)
    external_id: str = Field(min_length=1, max_length=200)

    @field_validator("source_url")
    @classmethod
    def public_source(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            host = (parsed.hostname or "").lower().rstrip(".")
            if (parsed.scheme != "https" or not host or parsed.username is not None
                    or parsed.password is not None or parsed.port not in (None, 443)
                    or "\\" in value or any(ord(c) < 33 for c in value)
                    or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
                raise ValueError()
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                if "." not in host or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in host):
                    raise ValueError()
            else:
                if not address.is_global:
                    raise ValueError()
            # This is a reference only, never fetched. Drop credentials in query/fragment.
            authority = f"[{host}]" if ":" in host else host
            return urlunsplit(("https", authority, parsed.path or "/", "", ""))
        except ValueError:
            raise ValueError("请填写不含登录信息的公开 HTTPS 来源链接") from None


class DataCandidate(CandidateInput):
    candidate_id: Digest
    asset_id: Digest
    created_at: int
    provenance: Literal["user_reference"] = "user_reference"


class VersionInput(StrictRecord):
    file_ids: list[OpaqueId] = Field(min_length=1, max_length=16)

    @field_validator("file_ids")
    @classmethod
    def unique_files(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("文件不能重复")
        return sorted(values)


class VersionFile(StrictRecord):
    file_id: OpaqueId
    size: int = Field(ge=0)
    sha256: Digest


class AcquisitionReceipt(StrictRecord):
    method: Literal["uploaded_files"] = "uploaded_files"
    source_verified: Literal[False] = False
    recorded_at: int


class DataVersion(StrictRecord):
    version_id: Digest
    candidate_id: Digest
    asset_id: Digest
    files: list[VersionFile]
    acquisition: AcquisitionReceipt
    retention: Literal["reference_only"] = "reference_only"


class CandidatePage(StrictRecord):
    candidates: list[DataCandidate]
    next_cursor: Digest | None = None


class VersionPage(StrictRecord):
    versions: list[DataVersion]
    next_cursor: Digest | None = None
