import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from app.domain.services.skills.package import read_package, package_digest
from scripts.build_data_skill import build
from app.domain.services.skills.registry import SkillRegistry

ROOT = Path(__file__).resolve().parents[2] / "data-skills/packages/tabular-quality"
# Compile source directly so tests do not add __pycache__ to the publishable package.
namespace = {"__name__": "quality_sample"}
exec(compile((ROOT / "scripts/profile_csv.py").read_text(), "profile_csv.py", "exec"), namespace)
profile = namespace["profile"]


def test_sample_has_independent_expected_counts_and_does_not_change_input():
    data = (ROOT / "references/sample.csv").read_bytes()
    report = profile(data)
    assert report["row_count"] == 4
    assert report["column_count"] == 3
    assert report["duplicate_rows"] == 1
    assert report["columns"] == [
        {"name": "id", "missing_count": 0}, {"name": "value", "missing_count": 2},
        {"name": "label", "missing_count": 1},
    ]
    assert report["input_sha256"] == hashlib.sha256(data).hexdigest()
    assert (ROOT / "references/sample.csv").read_bytes() == data


@pytest.mark.parametrize("data", [b"", b"a,a\n1,2", b"a,b\n1", b"a\n\xff", b"x" * (4 * 1024 * 1024 + 1), b"a\n" + b"x\n" * 10001], ids=["empty", "duplicate-header", "ragged", "encoding", "byte-budget", "row-budget"])
def test_rejects_invalid_or_excessive_input(data):
    with pytest.raises(ValueError):
        profile(data)


def test_bom_quoted_commas_and_header_only():
    assert profile(b'\xef\xbb\xbfa,b\r\n"x,y",NA\r\n')["columns"][1]["missing_count"] == 0
    assert profile(b"a,b\n")["row_count"] == 0


def test_package_build_is_reproducible_and_preserves_source_bytes(tmp_path):
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    receipt = build(ROOT, first)
    build(ROOT, second)
    assert first.read_bytes() == second.read_bytes()
    assert receipt["verification_status"] == "unverified"
    with zipfile.ZipFile(first) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    assert contents == read_package(ROOT)
    assert receipt["package_sha256"] == package_digest(contents)
    with pytest.raises(FileExistsError):
        build(ROOT, first)
    with pytest.raises(ValueError):
        build(ROOT, ROOT / "output.zip")


@pytest.mark.asyncio
async def test_built_package_imports_with_exact_identity_and_readable_resources(tmp_path):
    output = tmp_path / "release.zip"
    receipt = build(ROOT, output)
    registry = SkillRegistry(str(tmp_path / "registry"), user_id="sample-user")
    skill = (await registry.save_zip_skills(io.BytesIO(output.read_bytes()), user_id="sample-user"))[0]
    assert skill.name == "dataseek:tabular-quality@1.0.0"
    assert skill.manifest.category == "quality"
    assert skill.package_sha256 == receipt["package_sha256"]
    assert registry.read_script_content(skill.name, "profile_csv.py")[0]
    assert registry.read_reference(skill.name, "sample.csv")[0]
