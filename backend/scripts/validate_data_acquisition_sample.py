"""Bounded NCBI acquisition smoke test; no biology analysis or application writes.

Run from backend: uv run --frozen python -m scripts.validate_data_acquisition_sample
JSON receipt goes to stdout; the package stays in memory and is discarded.
"""

import hashlib
import io
import json
import stat
import time
import zipfile
from datetime import datetime, timezone
from pathlib import PurePosixPath

import httpx

ACCESSION = "GCF_000009045.1"
SOURCE_URL = (
    f"https://api.ncbi.nlm.nih.gov/datasets/v2/genome/accession/{ACCESSION}/download"
    "?include_annotation_type=GENOME_FASTA"
)
MAX_PACKAGE_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 16 * 1024 * 1024


def validate_package(package: bytes) -> dict:
    """Check bytes and identity, without asserting how these bytes were obtained."""
    if len(package) > MAX_PACKAGE_BYTES:
        raise ValueError("Package exceeds sample budget")
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if not entries or len(entries) > 64 or len(set(names)) != len(names):
            raise ValueError("Invalid package members")
        if sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
            raise ValueError("Expanded package exceeds sample budget")
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in entry.filename
                    or entry.is_dir() or entry.flag_bits & 1
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError("Unsafe package member")
        files = {entry.filename: archive.read(entry) for entry in entries}
    prefix = "ncbi_dataset/data/"
    catalog = json.loads(files[prefix + "dataset_catalog.json"])
    assemblies = [item for item in catalog["assemblies"] if item.get("accession")]
    reports = [json.loads(line) for line in files[prefix + "assembly_data_report.jsonl"].splitlines() if line.strip()]
    if ([item["accession"] for item in assemblies] != [ACCESSION]
            or [item.get("accession") for item in reports] != [ACCESSION]):
        raise ValueError("Sample accession mismatch")
    payloads = [prefix + item["filePath"] for item in assemblies[0]["files"]
                if item.get("fileType") == "GENOMIC_NUCLEOTIDE_FASTA"]
    if not payloads or any(not files.get(name) for name in payloads):
        raise ValueError("Sample payload missing")
    checksums = {}
    for line in files["md5sum.txt"].decode("utf-8").splitlines():
        digest, name = line.split(maxsplit=1)
        name = name.lstrip("*")
        if name in checksums or name not in files:
            raise ValueError("Invalid checksum manifest")
        if hashlib.md5(files[name], usedforsecurity=False).hexdigest() != digest:
            raise ValueError("Provider checksum mismatch")
        checksums[name] = digest
    required = set(payloads) | {prefix + "dataset_catalog.json", prefix + "assembly_data_report.jsonl"}
    if not required.issubset(checksums):
        raise ValueError("Missing provider checksums")
    return {
        "accession": ACCESSION,
        "package_bytes": len(package),
        "package_sha256": hashlib.sha256(package).hexdigest(),
        "checks": ["zip_crc", "exact_accession", "payload_present", "provider_md5"],
        "files": [{"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                   "provider_checksum_verified": name in checksums} for name, data in sorted(files.items())],
    }


def main() -> int:
    started = time.monotonic()
    try:
        package = bytearray()
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
            with client.stream("GET", SOURCE_URL, headers={"Accept": "application/zip"}) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(65536):
                    if time.monotonic() - started > 60 or len(package) + len(chunk) > MAX_PACKAGE_BYTES:
                        raise ValueError("Sample download budget exceeded")
                    package.extend(chunk)
        receipt = validate_package(bytes(package))
    except (httpx.HTTPError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        print(json.dumps({"valid": False, "scope": "acquisition_sample_only"}))
        return 1
    receipt.update(valid=True, scope="acquisition_sample_only", source_url=SOURCE_URL,
                   fetched_at=datetime.now(timezone.utc).isoformat(),
                   production_version_registered=False, analysis_performed=False)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
