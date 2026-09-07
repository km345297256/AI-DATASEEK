"""Offline download/archive safety tests: no HTTP requests or application DB."""
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import PurePosixPath
import stat
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest

from scripts import prepare_external_datasets as preparation


PAYLOAD = b"year,value\n2024,1\n"
PUBLIC_URL = "https://china.scidb.cn/download?fileId=reviewed-test-file"


@pytest.fixture
def fake_curl(monkeypatch):
    """curl writes solely to its explicitly passed exclusive output handle."""
    def configure(payload=PAYLOAD, *, status=200, returncode=0, stderr=None):
        def execute(command, *, stdout, stderr):
            stdout.write(payload)
            return SimpleNamespace(returncode=returncode, stderr=response_stderr)

        response_stderr = str(status).encode() if stderr is None else stderr
        runner = Mock(side_effect=execute)
        monkeypatch.setattr(preparation.subprocess, "run", runner)
        return runner

    configure()
    return configure


@pytest.fixture
def descriptor():
    return {
        "dataset_id": "scidb-reviewed-example",
        "name": "Reviewed measurements",
        "description": "One independently published measurement table.",
        "domain": "tabular",
        "data_type": "csv",
        "source_url": "https://www.scidb.cn/en/detail?dataSetId=reviewed-example",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "sample_scope": "Complete published table, not a synthetic example.",
        "files": [{
            "path": "measurements.csv", "url": PUBLIC_URL, "method": "GET", "format": "csv",
            "expected_size": len(PAYLOAD), "expected_md5": hashlib.md5(PAYLOAD).hexdigest(),
        }],
    }


def write_zip(path, members):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(name, data)
    return path


@pytest.mark.parametrize("path", [
    "", "../outside.csv", "nested/../../outside.csv", "/absolute.csv",
    "nested/../outside.csv", "..", "nested\\outside.csv", "file\x00.csv",
])
def test_relative_paths_reject_traversal_and_ambiguous_separators(path):
    with pytest.raises(ValueError, match="Unsafe relative path"):
        preparation.safe_relative(path)


@pytest.mark.parametrize("path", ["measurements.csv", "嵌套/含 空格.csv", "nested/table(1).csv"])
def test_normal_relative_paths_preserve_dataset_file_names(path):
    assert preparation.safe_relative(path) == PurePosixPath(path)


@pytest.mark.parametrize("url", [
    PUBLIC_URL,
    "https://data.tpdc.ac.cn/files/sample.zip",
    "https://ngdc.cncb.ac.cn/gwh/sample.fa.gz",
    "https://www.casdc.cn/public/data.csv",
    "http://chemdc.casdc.cn/public/data.csv",
])
def test_public_url_accepts_only_reviewed_source_families(url):
    assert preparation.public_url(url) == url


@pytest.mark.parametrize("url", [
    "https://example.org/data.csv", "https://scidb.cn.example.org/data.csv",
    "https://not-scidb.cn/data.csv", "http://china.scidb.cn/data.csv",
    "http://data.tpdc.ac.cn/data.csv", "ftp://china.scidb.cn/data.csv",
    "file:///etc/passwd", "https://localhost/data.csv", "https://127.0.0.1/data.csv",
    "https://[::1]/data.csv", "https://user:secret@china.scidb.cn/data.csv",
    "https://china.scidb.cn:443/data.csv", "//china.scidb.cn/data.csv",
])
def test_unreviewed_source_urls_are_rejected(url):
    with pytest.raises(ValueError):
        preparation.public_url(url)


def test_valid_download_checks_publisher_size_and_md5(tmp_path, descriptor, fake_curl):
    runner = fake_curl()
    target = tmp_path / "measurements.csv"
    assert preparation.download(descriptor["files"][0], target) == len(PAYLOAD)
    assert target.read_bytes() == PAYLOAD
    assert runner.call_count == 1
    command = runner.call_args.args[0]
    assert command[0] == "curl" and command[-1] == PUBLIC_URL
    assert "--fail" in command and "--max-filesize" in command


@pytest.mark.parametrize("status", [201, 204, 206, 301, 302, 303, 307, 308, 403, 404, 500])
def test_download_rejects_every_non_200_response(tmp_path, descriptor, fake_curl, status):
    runner = fake_curl(status=status)
    with pytest.raises(ValueError, match="non-200|redirect"):
        preparation.download(descriptor["files"][0], tmp_path / "rejected.csv")
    assert runner.call_count == 1


def test_download_never_follows_an_unreviewed_redirect(tmp_path, descriptor, fake_curl):
    runner = fake_curl(status=302)
    with pytest.raises(ValueError, match="redirect"):
        preparation.download(descriptor["files"][0], tmp_path / "redirect.csv")
    command = runner.call_args.args[0]
    assert "--location" not in command and "-L" not in command
    assert "--location-trusted" not in command
    assert runner.call_count == 1


def test_curl_failure_does_not_expose_response_or_signed_url(tmp_path, descriptor, fake_curl):
    fake_curl(returncode=22, stderr=b"failed https://example.org/?token=private-secret 403")
    with pytest.raises(ValueError, match="curl 22") as error:
        preparation.download(descriptor["files"][0], tmp_path / "error.csv")
    assert "token" not in str(error.value) and "private-secret" not in str(error.value)


def test_download_rejects_wrong_size(tmp_path, descriptor, fake_curl):
    fake_curl(PAYLOAD + b"extra")
    with pytest.raises(ValueError, match="size differs"):
        preparation.download(descriptor["files"][0], tmp_path / "wrong-size.csv")


def test_download_rejects_same_size_but_wrong_md5(tmp_path, descriptor, fake_curl):
    fake_curl(PAYLOAD.replace(b",1", b",2"))
    with pytest.raises(ValueError, match="MD5"):
        preparation.download(descriptor["files"][0], tmp_path / "wrong-content.csv")


@pytest.mark.parametrize("payload", [b"", PAYLOAD])
def test_download_rejects_empty_and_oversized_response(tmp_path, fake_curl, monkeypatch, payload):
    fake_curl(payload)
    monkeypatch.setattr(preparation, "MAX_DOWNLOAD", 1)
    with pytest.raises(ValueError, match="Empty or oversized"):
        preparation.download({"url": PUBLIC_URL}, tmp_path / "oversized.csv")


@pytest.mark.parametrize("payload", [
    b"<!DOCTYPE html><html>login</html>", b" \n<HTML>access denied</HTML>",
    b'{"code":403,"message":"not authorized"}',
    b'{"status":"error"}', b'{"msg":"please log in"}',
])
def test_download_rejects_login_and_api_error_documents(tmp_path, fake_curl, payload):
    fake_curl(payload)
    with pytest.raises(ValueError, match="error/login document"):
        preparation.download({"url": PUBLIC_URL}, tmp_path / "not-data.csv")


@pytest.mark.parametrize("format", ["zip", "xlsx", "gzip"])
def test_download_rejects_wrong_container_signature(tmp_path, fake_curl, format):
    fake_curl()
    with pytest.raises(ValueError, match="Expected"):
        preparation.download({"url": PUBLIC_URL, "format": format}, tmp_path / "wrong.bin")


def test_download_does_not_overwrite_existing_file(tmp_path, descriptor, fake_curl):
    target = tmp_path / "existing.csv"
    target.write_bytes(b"user data")
    runner = fake_curl()
    with pytest.raises(FileExistsError):
        preparation.download(descriptor["files"][0], target)
    assert target.read_bytes() == b"user data"
    runner.assert_not_called()


def test_archive_unpack_preserves_original_and_safe_nested_contents(tmp_path):
    data = tmp_path / "dataset"
    data.mkdir()
    archive = write_zip(data / "original.zip", [("nested/data.csv", PAYLOAD)])
    original = archive.read_bytes()
    outputs = preparation.extract_archive(archive, data, len(PAYLOAD))
    assert len(outputs) == 1 and outputs[0].read_bytes() == PAYLOAD
    assert outputs[0].relative_to(data).as_posix() == "extracted/original/nested/data.csv"
    assert archive.read_bytes() == original


@pytest.mark.parametrize("name", ["../../escape.csv", "/escape.csv", "nested/../../../escape.csv", "nested\\escape.csv"])
def test_zip_traversal_is_rejected_without_creating_outside_files(tmp_path, name):
    data = tmp_path / "dataset"
    data.mkdir()
    archive = write_zip(data / "malicious.zip", [(name, PAYLOAD)])
    with pytest.raises(ValueError, match="Unsafe relative path"):
        preparation.extract_archive(archive, data, 1024)
    assert not (tmp_path / "escape.csv").exists()
    assert sorted(path.name for path in data.iterdir()) == ["malicious.zip"]


def test_zip_symlink_entries_are_never_materialized(tmp_path):
    member = zipfile.ZipInfo("link-to-outside")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = write_zip(tmp_path / "symlink.zip", [(member, "../../outside")])
    with pytest.raises(ValueError, match="symlink"):
        preparation.extract_archive(archive, tmp_path, 1024)
    assert not (tmp_path / "extracted").exists()


def test_zip_expanded_byte_limit_is_checked_before_extracting(tmp_path):
    archive = write_zip(tmp_path / "oversized.zip", [("one.csv", PAYLOAD), ("two.csv", PAYLOAD)])
    with pytest.raises(ValueError, match="extraction limits"):
        preparation.extract_archive(archive, tmp_path, len(PAYLOAD))
    assert not (tmp_path / "extracted").exists()


def test_zip_entry_count_limit_is_checked_before_extracting(tmp_path, monkeypatch):
    monkeypatch.setattr(preparation, "MAX_FILES", 1)
    archive = write_zip(tmp_path / "many.zip", [("one.csv", PAYLOAD), ("two.csv", PAYLOAD)])
    with pytest.raises(ValueError, match="extraction limits"):
        preparation.extract_archive(archive, tmp_path, 1024)
    assert not (tmp_path / "extracted").exists()


def test_zip_does_not_overwrite_existing_extracted_file(tmp_path):
    archive = write_zip(tmp_path / "repeat.zip", [("data.csv", PAYLOAD)])
    existing = tmp_path / "extracted" / "repeat" / "data.csv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"preserve me")
    with pytest.raises(FileExistsError):
        preparation.extract_archive(archive, tmp_path, 1024)
    assert existing.read_bytes() == b"preserve me"


def test_gzip_unpack_obeys_remaining_byte_quota(tmp_path):
    archive = tmp_path / "data.csv.gz"
    archive.write_bytes(gzip.compress(PAYLOAD * 4))
    with pytest.raises(ValueError, match="GZIP exceeds extraction limit"):
        preparation.extract_archive(archive, tmp_path, len(PAYLOAD))
    partial = tmp_path / "extracted" / "data.csv"
    assert partial.stat().st_size <= len(PAYLOAD)
    assert archive.is_file()


def test_gzip_unpack_accepts_exact_boundary_without_modifying_original(tmp_path):
    archive = tmp_path / "data.csv.gz"
    compressed = gzip.compress(PAYLOAD)
    archive.write_bytes(compressed)
    outputs = preparation.extract_archive(archive, tmp_path, len(PAYLOAD))
    assert len(outputs) == 1 and outputs[0].read_bytes() == PAYLOAD
    assert archive.read_bytes() == compressed


def test_new_directory_rejects_existing_user_directory(tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "user.txt"
    marker.write_bytes(b"untouched")
    with pytest.raises(FileExistsError):
        preparation.new_directory(destination)
    assert marker.read_bytes() == b"untouched"


def test_new_directory_rejects_symlinked_ancestor(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked destination"):
        preparation.new_directory(alias / "new-dataset")
    assert not (real / "new-dataset").exists()


def test_prepare_resumes_only_identical_complete_data(tmp_path, descriptor, fake_curl):
    runner = fake_curl()
    data_root, catalog_root = tmp_path / "open-catalog", tmp_path / "manifests"
    first = preparation.prepare(descriptor, "scidb", data_root, catalog_root)
    second = preparation.prepare(deepcopy(descriptor), "scidb", data_root, catalog_root)
    assert first["status"] == "verified" and second["status"] == "already_verified"
    assert first["bytes"] == second["bytes"] and first["files"] == second["files"]
    assert runner.call_count == 1
    manifest = json.loads((catalog_root / "scidb" / f"{descriptor['dataset_id']}.json").read_text())
    assert len(manifest["metadata"]["source_descriptor_sha256"]) == 64


@pytest.mark.parametrize("field", ["license", "sample_scope", "source_version", "url", "expected_md5"])
def test_prepare_rejects_changed_descriptor_without_overwriting(tmp_path, descriptor, fake_curl, field):
    runner = fake_curl()
    data_root, catalog_root = tmp_path / "open-catalog", tmp_path / "manifests"
    preparation.prepare(descriptor, "scidb", data_root, catalog_root)
    manifest_path = catalog_root / "scidb" / f"{descriptor['dataset_id']}.json"
    original_manifest = manifest_path.read_bytes()
    changed = deepcopy(descriptor)
    if field in {"url", "expected_md5"}:
        changed["files"][0][field] = "changed-review-value"
    else:
        changed[field] = "changed-review-value"
    with pytest.raises(ValueError, match="different or unrecorded descriptor"):
        preparation.prepare(changed, "scidb", data_root, catalog_root)
    assert runner.call_count == 1
    assert manifest_path.read_bytes() == original_manifest
    assert (data_root / "scidb" / descriptor["dataset_id"] / "measurements.csv").read_bytes() == PAYLOAD


def test_prepare_rejects_same_size_local_corruption_on_resume(tmp_path, descriptor, fake_curl):
    runner = fake_curl()
    data_root, catalog_root = tmp_path / "open-catalog", tmp_path / "manifests"
    preparation.prepare(descriptor, "scidb", data_root, catalog_root)
    target = data_root / "scidb" / descriptor["dataset_id"] / "measurements.csv"
    corrupted = PAYLOAD.replace(b",1", b",2")
    target.write_bytes(corrupted)
    with pytest.raises(ValueError, match="does not match manifest"):
        preparation.prepare(descriptor, "scidb", data_root, catalog_root)
    assert runner.call_count == 1 and target.read_bytes() == corrupted


def test_prepare_never_claims_or_overwrites_unmanifested_existing_directory(tmp_path, descriptor, fake_curl):
    runner = fake_curl()
    data_root, catalog_root = tmp_path / "open-catalog", tmp_path / "manifests"
    existing = data_root / "scidb" / descriptor["dataset_id"]
    existing.mkdir(parents=True)
    marker = existing / "important.txt"
    marker.write_bytes(b"existing user contents")
    with pytest.raises(FileExistsError):
        preparation.prepare(descriptor, "scidb", data_root, catalog_root)
    runner.assert_not_called()
    assert marker.read_bytes() == b"existing user contents"
    assert not (catalog_root / "scidb" / f"{descriptor['dataset_id']}.json").exists()


def test_prepare_does_not_publish_manifest_after_download_failure(tmp_path, descriptor, fake_curl):
    fake_curl(status=403)
    data_root, catalog_root = tmp_path / "open-catalog", tmp_path / "manifests"
    with pytest.raises(ValueError, match="non-200"):
        preparation.prepare(descriptor, "scidb", data_root, catalog_root)
    assert not (catalog_root / "scidb" / f"{descriptor['dataset_id']}.json").exists()


def test_signed_download_url_is_not_written_as_public_provenance():
    source = "https://data.tpdc.ac.cn/en/data/reviewed-example"
    spec = {"url": "https://data.tpdc.ac.cn/files/data?X-Amz-Signature=private-value"}
    assert preparation.provenance_url(spec, source) == source


def test_plain_public_download_identifier_is_retained_as_provenance():
    assert preparation.provenance_url({"url": PUBLIC_URL}, "https://www.scidb.cn/") == PUBLIC_URL
