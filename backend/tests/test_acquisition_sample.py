import hashlib
import io
import json
import zipfile

import pytest

from scripts.validate_data_acquisition_sample import ACCESSION, validate_package


def package(*, accession=ACCESSION, corrupt=False, missing_checksum=False, unsafe=False):
    prefix = "ncbi_dataset/data/"
    payload = prefix + ACCESSION + "/sample.fna"
    files = {
        prefix + "dataset_catalog.json": json.dumps({"assemblies": [{
            "accession": accession, "files": [{"filePath": ACCESSION + "/sample.fna",
                                                "fileType": "GENOMIC_NUCLEOTIDE_FASTA"}]}]}).encode(),
        prefix + "assembly_data_report.jsonl": json.dumps({"accession": accession}).encode(),
        payload: b">sample\nACGT\n",
    }
    manifest = "\n".join(f"{hashlib.md5(data, usedforsecurity=False).hexdigest()}  {name}"
                         for name, data in files.items() if not (missing_checksum and name == payload))
    if corrupt:
        files[payload] = b">sample\nAAAA\n"
    files["md5sum.txt"] = manifest.encode()
    if unsafe:
        files["../escape"] = b"unsafe"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()


def test_sample_integrity_without_claiming_network_provenance():
    data = package()
    result = validate_package(data)
    assert result["package_sha256"] == hashlib.sha256(data).hexdigest()
    assert result["accession"] == ACCESSION
    assert len(result["files"]) == 4
    assert "source_url" not in result


@pytest.mark.parametrize("options", [
    {"accession": "GCF_000009045.2"}, {"corrupt": True},
    {"missing_checksum": True}, {"unsafe": True},
])
def test_sample_rejects_invalid_package(options):
    with pytest.raises(ValueError):
        validate_package(package(**options))
