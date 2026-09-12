"""Independent native-writer compatibility, not the format-level test builder."""
import hashlib
import json
import re
from pathlib import Path

import pytest

from app.services.pg_dump_reader import pg_dump_preview

FIXTURES = Path(__file__).parent / "fixtures/database-dumps"
NATIVE_HASHES = {
    "synthetic-postgres-none.dump": "b2e2db08384a26283e98d0e51aca30f8a945d43f9c43426b39bf52a4a1b8261f",
    "synthetic-postgres-gzip.dump": "453fec696badec68d8f63565989de63397c359ce03ac7a85d2811ecc34d62d8f",
    "synthetic-postgres-lz4.dump": "f6d40797c8663c60e10d7f08fca2e08467d99d78670bcf0991485a171846f83c",
    "synthetic-postgres-zstd.dump": "410ac9f2bdc33c1dda9cd724da8ce4e60139b05900073c2bbfab3f964bf2a91b",
    "synthetic-postgres.tar": "6d519a3b1d183310211536386cbfcd83d4e37f3af4f74ec10d5095c023579b82",
    "synthetic-postgres-identifiers.dump": "aea0db5e697a80228841ab3b79fdd6b6b819a6ebbe55a098a0e6fbd8365029f4",
}


@pytest.mark.parametrize("filename", list(NATIVE_HASHES))
def test_native_postgres_18_6_custom_compressions_tar_and_identifiers(filename):
    raw = (FIXTURES / filename).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == NATIVE_HASHES[filename]
    result = pg_dump_preview(raw, "tar" if filename.endswith(".tar") else "dump")
    assert result["metadata"]["archive_version"] == "1.16.0"
    assert result["metadata"]["data_verified"] is False
    listing_name = filename.removesuffix(".dump") + ".list"
    if filename.endswith(".tar"):
        listing_name = "synthetic-postgres-tar.list"
    listing = (FIXTURES / listing_name).read_text(encoding="utf8")
    toc_count = int(re.search(r"TOC Entries: (\d+)", listing).group(1))
    assert result["metadata"]["objects_total"] == toc_count
    # Deliberately only compare simple identifiers in this known native sample;
    # pg_restore's human listing is not parsed as a general-purpose protocol.
    public_objects = [node["attributes"] for node in result["tree"] if node["attributes"]["schema"] == "public" and node["attributes"]["object_type"] in {"TABLE", "TABLE DATA"}]
    assert public_objects == [
        {"object_type": "TABLE", "schema": "public", "name": "empty"},
        {"object_type": "TABLE", "schema": "public", "name": "measurements"},
        {"object_type": "TABLE DATA", "schema": "public", "name": "empty"},
        {"object_type": "TABLE DATA", "schema": "public", "name": "measurements"},
    ]
    for obj in public_objects:
        assert f" {obj['object_type']} public {obj['name']} postgres\n" in listing
    serialized = json.dumps(result, ensure_ascii=False)
    for private in ["postgres", "/private/original/location", "/Users/private-name", "COPY", "CREATE TABLE"]:
        assert private not in serialized


def test_native_quoted_identifiers_preserved_but_paths_and_comments_hidden():
    result = pg_dump_preview((FIXTURES / "synthetic-postgres-identifiers.dump").read_bytes())
    attrs = [node["attributes"] for node in result["tree"]]
    assert {"object_type": "TABLE", "schema": "science lab", "name": "测量 data"} in attrs
    assert {"object_type": "TABLE", "schema": "science lab", "name": "名称已隐藏"} in attrs
    assert {"object_type": "COMMENT", "schema": "无命名空间", "name": "名称已隐藏"} in attrs


def test_frontend_fixture_is_native_reader_output():
    root = Path(__file__).resolve().parents[2]
    browser = json.loads((root / "frontend/tests/browser/pg-dump-data.json").read_text(encoding="utf8"))
    for key, filename, fmt in [
        ("treeCustom", "synthetic-postgres-gzip.dump", "dump"),
        ("treeTar", "synthetic-postgres.tar", "tar"),
        ("treeIdentifiers", "synthetic-postgres-identifiers.dump", "backup"),
    ]:
        assert browser[key] == pg_dump_preview((FIXTURES / filename).read_bytes(), fmt)
