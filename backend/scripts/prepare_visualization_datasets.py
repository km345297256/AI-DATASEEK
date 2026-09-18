"""Install checksum-pinned, reviewed public visualization samples offline.

The operator downloads and reviews sources in staging first. This command never
fetches a URL, executes a source, replaces a dataset, or connects to MongoDB.
Default is validation only. Registration is a separate read-only-bind import.
"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import mimetypes
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
from urllib.parse import parse_qsl, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.domain.models.dataset import CuratedDatasetSeed

SOURCE = "open-science"
KINDS = {"image", "map", "series", "table", "text", "structure", "document", "tree", "media", "graph"}
DOMAINS = {"general", "tabular", "geoscience", "image_science", "chemistry", "sequence", "space", "documents", "spectroscopy"}
MAX_BYTES = 128 * 1024**2
REPOSITORY = Path(__file__).resolve().parents[2]
SAMPLE_KINDS = {"scientific_original", "scientific_derived", "official_fixture", "project_fixture"}


def relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or str(path) != value or path.is_absolute() or any(p in {".", ".."} for p in path.parts) or "\\" in value or "\x00" in value:
        raise ValueError("Invalid relative file name")
    return path


def safe_root(path: Path) -> Path:
    if not path.is_absolute() or path == Path("/") or path.resolve() != path:
        raise ValueError("Root must be a specific non-symlink absolute directory")
    return path


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def file_info(path: str, size: int, digest: str, role: str = "data") -> dict:
    return {"path": path, "size": size, "sha256": digest, "role": role,
            "content_type": mimetypes.guess_type(path)[0] or "application/octet-stream"}


def public_url(value: str) -> None:
    parsed = urlsplit(value)
    keys = [unquote(key).lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)]
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port or parsed.fragment or any(token in key for key in keys for token in ("token", "signature", "credential", "secret", "access_key", "api_key", "password")):
        raise ValueError("Provenance must use public HTTPS URLs without credentials")


def fixture_origin(spec: dict) -> None:
    """Check a local fixture's provenance without pretending it was downloaded."""
    origin = spec.get("origin", {})
    kind = origin.get("kind")
    field = "repository_file" if kind == "project_fixture" else "generator"
    if kind not in {"project_fixture", "generated_fixture"}:
        raise ValueError("Unknown fixture origin")
    rel = relative(origin.get(field, ""))
    allowed = ("sandbox/tests/fixtures/",) if kind == "project_fixture" else ("backend/scripts/", "sandbox/tests/")
    if not str(rel).startswith(allowed) and not (kind == "project_fixture" and str(rel) == "LICENSE"):
        raise ValueError("Fixture origin is outside reviewed repository resources")
    path = REPOSITORY / rel
    if path.resolve() != path or not path.is_file():
        raise ValueError("Missing or symlinked fixture origin")
    expected = origin.get("sha256" if kind == "project_fixture" else "generator_sha256")
    if sha256(path) != expected or (kind == "project_fixture" and expected != spec["sha256"]):
        raise ValueError("Fixture origin checksum mismatch")
    if kind == "generated_fixture" and not origin.get("description"):
        raise ValueError("Generated fixtures require explicit synthetic scope")


def validate_derivations(files: list[dict], *, profile: str) -> None:
    """Every derivation must end at a reviewed source, without recursion."""
    specs = {spec["path"]: spec for spec in files}
    remaining, dependents = {}, {path: [] for path in specs}
    for path, spec in specs.items():
        parents = spec.get("derived_from", [])
        if "derived_from" in spec and (
            not isinstance(parents, list) or not parents
            or any(not isinstance(parent, str) or parent not in specs or parent == path for parent in parents)
            or len(set(parents)) != len(parents)
        ):
            raise ValueError("Derived file must reference distinct registered originals")
        if not parents and not (spec.get("url") or profile == "plugin-tests" and spec.get("origin")):
            raise ValueError("Derivation must terminate at a reviewed source")
        remaining[path] = len(parents)
        for parent in parents:
            dependents[parent].append(path)
    ready = [path for path, count in remaining.items() if count == 0]
    visited = 0
    while ready:
        path = ready.pop()
        visited += 1
        for child in dependents[path]:
            remaining[child] -= 1
            if remaining[child] == 0:
                ready.append(child)
    if visited != len(specs):
        raise ValueError("Cyclic file derivation is not permitted")


def validate_plugin_entry(entry: dict, plugin: dict) -> None:
    """Match complete registered suffixes (including ome.tiff) or names."""
    name = PurePosixPath(entry["path"]).name.casefold()
    names = {value.casefold() for value in plugin.get("filenames", [])}
    matches = name in names or any(
        name.endswith("." + extension.casefold().lstrip("."))
        for extension in plugin.get("extensions", [])
    )
    if entry["role"] != "data" or not matches:
        raise ValueError("Preview entry must be a data file with a registered plugin format")


def plan(item: dict, staging: Path, download_date: str, *, profile: str = SOURCE,
         max_bytes: int = MAX_BYTES, max_files: int = 32) -> tuple[dict, str]:
    dataset_id = item["dataset_id"]
    if not re.fullmatch(r"viz-[a-z0-9][a-z0-9-]{1,110}[a-z0-9]", dataset_id):
        raise ValueError("Invalid visualization dataset identity")
    if item["view_kind"] not in KINDS or item["domain"] not in DOMAINS:
        raise ValueError("Unknown visualization kind or domain")
    for key in ("name", "description", "publisher", "license", "license_details", "sample_scope", "plugin_id"):
        if not item.get(key):
            raise ValueError("Missing required provenance: " + key)
    public_url(item["source_url"])
    public_url(item["license_url"])
    if profile not in {SOURCE, "plugin-tests"}:
        raise ValueError("Unknown preparation profile")
    if not 1 <= max_bytes <= 16 * 1024**3 or not 1 <= max_files <= 19_999:
        raise ValueError("Invalid offline preparation budget")
    if profile == SOURCE and (max_bytes != MAX_BYTES or max_files != 32):
        raise ValueError("Extended budgets require the explicit plugin-tests profile")
    if profile == "plugin-tests" and item.get("sample_kind") not in SAMPLE_KINDS:
        raise ValueError("Plugin samples must distinguish observations from fixtures")
    if not 1 <= len(item["files"]) <= max_files:
        raise ValueError("Reviewed file count exceeds budget")
    declarations, paths = [], set()
    for spec in item["files"]:
        rel = relative(spec["path"])
        if spec["path"].casefold() in {p.casefold() for p in paths} or rel.parts[0].casefold() == "source.md":
            raise ValueError("Duplicate or reserved filename")
        paths.add(spec["path"])
        source = staging / dataset_id / rel
        if source.resolve() != source or not source.is_file():
            raise ValueError("Missing or symlinked source file")
        if not 0 < spec["size"] <= max_bytes or source.stat().st_size != spec["size"] or sha256(source) != spec["sha256"]:
            raise ValueError("Source checksum or size mismatch: " + dataset_id)
        if spec.get("url"):
            public_url(spec["url"])
        elif profile == "plugin-tests" and spec.get("origin"):
            fixture_origin(spec)
        elif not spec.get("derived_from"):
            raise ValueError("Every file needs a source URL or explicit derivation")
        declarations.append(file_info(spec["path"], spec["size"], spec["sha256"], spec.get("role", "data")))
    validate_derivations(item["files"], profile=profile)
    if item["entry_file"] not in paths or sum(f["size"] for f in declarations) > max_bytes:
        raise ValueError("Missing preview entry or oversized dataset")
    if profile == "plugin-tests":
        plugins = {p["id"]: p for path in (REPOSITORY / "plugin-host/visualizations").glob("*.json")
                   for p in [json.loads(path.read_text())]}
        plugin = plugins.get(item["plugin_id"])
        if not plugin or plugin["view_kind"] != item["view_kind"]:
            raise ValueError("Sample does not match a current visualization plugin")
        entry = next(f for f in declarations if f["path"] == item["entry_file"])
        mode = plugin.get("capabilities", {}).get("input_mode", "whole")
        if mode == "whole" and entry["size"] > plugin["limits"]["max_input_bytes"]:
            raise ValueError("Whole-file preview exceeds the unchanged plugin budget")
        validate_plugin_entry(entry, plugin)
        checks = item.get("test_steps")
        if not isinstance(checks, list) or not checks or not all(isinstance(s, str) and s.strip() for s in checks):
            raise ValueError("Plugin samples require manual test steps")
    transformation = item.get('transformation', '未修改来源数据。')
    if not isinstance(transformation, str):
        transformation = json.dumps(transformation, ensure_ascii=False, indent=2)
    source_note = "\n".join([
        f"# {item['name']}", "", item["description"], "",
        f"发布机构：{item['publisher']}", f"来源：{item['source_url']}",
        f"作者：{', '.join(item.get('authors', []))}",
        f"许可：{item['license']} ({item['license_url']})", item["license_details"],
        f"版本：{item.get('source_version', '')}", f"范围：{item['sample_scope']}",
        f"转换：{transformation}",
        f"下载快照日期：{download_date}", "",
        f"主预览文件：{item['entry_file']}", f"建议可视化插件：{item['plugin_id']}（{item['view_kind']}）",
        "在探查页面点击文件，必要时在视图选择器中选择上述插件。未更改已有插件启停偏好。",
        "", "可分析问题：", *[f"- {q}" for q in item.get("suggested_questions", [])], "",
        "## 文件完整性", "SHA-256 是下载快照的本地完整性基线，不是发布机构签名。", "",
        *[f"- {f['path']}：{f['size']} bytes，SHA-256 {f['sha256']}" for f in declarations], "",
    ])
    if profile == "plugin-tests":
        source_note += "\n## 样本性质与测试\n\n" + item["sample_kind"] + "\n\n"
        source_note += "\n".join(f"{i}. {step}" for i, step in enumerate(item["test_steps"], 1)) + "\n"
        source_note += "\n这是一套指定功能的测试入口，不代表该插件全部格式、边界条件或性能已验证。\n"
    note = source_note.encode("utf-8")
    declarations.append(file_info("SOURCE.md", len(note), hashlib.sha256(note).hexdigest(), "documentation"))
    descriptor_digest = hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    metadata = {"curated": True, "source_catalog": profile, "catalog_version": 1,
        "source_descriptor_sha256": descriptor_digest, "download_date": download_date,
        "inventory_complete": True, "recursive_file_count": len(declarations),
        "total_size_bytes": sum(f["size"] for f in declarations),
        "visualization_view_kind": item["view_kind"], "visualization_plugin_id": item["plugin_id"],
        "visualization_entry_file": item["entry_file"], "sample_note": item["sample_scope"],
        "provenance": item["files"],
        **{k: item[k] for k in ("publisher", "source_url", "license", "license_url", "license_details", "authors", "source_version", "sample_scope", "doi", "transformation", "suggested_questions") if k in item}}
    manifest = {"dataset_id": dataset_id, "external_id": item.get("doi", dataset_id),
        "data_center_id": "reviewed-open-science-catalog", "data_center_name": "开放科学可视化样例",
        "name": item["name"], "description": item["description"], "domain": item["domain"],
        "data_type": ", ".join(sorted({PurePosixPath(f['path']).suffix.lstrip('.') for f in declarations if f['role'] == 'data'})),
        "temporal_coverage": item.get("temporal_coverage", ""), "spatial_coverage": item.get("spatial_coverage", ""),
        "tags": ["可视化样例", "开放科学", item["view_kind"], item["plugin_id"], item["domain"]],
        "metadata": metadata, "files": sorted(declarations, key=lambda f: f["path"])}
    if profile == "plugin-tests":
        manifest["data_center_id"] = "reviewed-visualization-plugin-tests"
        manifest["data_center_name"] = "逐插件可视化测试样例"
        manifest["tags"] = ["插件测试样例", item["plugin_id"], item["sample_kind"], item["view_kind"], item["domain"]]
        metadata.update({"sample_kind": item["sample_kind"], "test_steps": item["test_steps"]})
    # Apply exactly the same field contract as the later registration stage.
    CuratedDatasetSeed.model_validate(manifest)
    return manifest, source_note


def verify_existing(directory: Path, manifest_path: Path, manifest: dict) -> bool:
    if not directory.exists() and not manifest_path.exists():
        return False
    if not directory.is_dir() or not manifest_path.is_file() or directory.resolve() != directory or manifest_path.resolve() != manifest_path:
        raise ValueError("Incomplete or symlinked existing destination; not overwritten")
    if json.loads(manifest_path.read_text()) != manifest:
        raise ValueError("Existing manifest differs; not overwritten")
    expected = {f["path"]: f for f in manifest["files"]}
    actual = {}
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("Symlink in existing dataset")
        if path.is_file():
            actual[str(path.relative_to(directory))] = (path.stat().st_size, sha256(path))
    if actual != {name: (f["size"], f["sha256"]) for name, f in expected.items()}:
        raise ValueError("Existing dataset differs; not overwritten")
    return True


def run(args) -> list[dict]:
    staging, data_root, catalog = map(safe_root, (args.staging_root, args.data_root, args.catalog_root))
    date.fromisoformat(args.download_date)
    items = json.loads(args.descriptors.read_text())
    ids = [item["dataset_id"] for item in items]
    if len(ids) != len(set(ids)) or not ids:
        raise ValueError("Empty or duplicate dataset identities")
    planned = []
    profile = getattr(args, "profile", SOURCE)
    for item in items:
        manifest, note = plan(item, staging, args.download_date, profile=profile,
                              max_bytes=getattr(args, "max_dataset_bytes", MAX_BYTES),
                              max_files=getattr(args, "max_files", 32))
        directory = safe_root(data_root / profile / item["dataset_id"])
        target = safe_root(catalog / profile / (item["dataset_id"] + ".json"))
        exists = verify_existing(directory, target, manifest)
        planned.append((item, manifest, note, directory, target, exists))
    # Validate the entire batch before creating any destination.
    results = []
    for item, manifest, note, directory, target, exists in planned:
        if args.apply and not exists:
            directory.mkdir(parents=True, exist_ok=False)
            for spec in item["files"]:
                dest = directory / relative(spec["path"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                with (staging / item["dataset_id"] / spec["path"]).open("rb") as source, dest.open("xb") as output:
                    shutil.copyfileobj(source, output)
                if dest.stat().st_size != spec["size"] or sha256(dest) != spec["sha256"]:
                    raise ValueError("Source changed during copy; incomplete output retained, not registered")
            with (directory / "SOURCE.md").open("x", encoding="utf-8") as output:
                output.write(note)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8") as output:
                json.dump(manifest, output, ensure_ascii=False, indent=2)
                output.write("\n")
            verify_existing(directory, target, manifest)
        results.append({"dataset_id": item["dataset_id"], "view_kind": item["view_kind"],
            "files": len(manifest["files"]), "bytes": manifest["metadata"]["total_size_bytes"],
            "status": "existing_verified" if exists else "prepared" if args.apply else "validated"})
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("descriptors", "staging-root", "data-root", "catalog-root"):
        parser.add_argument("--" + field, required=True, type=Path)
    parser.add_argument("--download-date", required=True)
    parser.add_argument("--profile", choices=[SOURCE, "plugin-tests"], default=SOURCE)
    parser.add_argument("--max-dataset-bytes", type=int, default=MAX_BYTES)
    parser.add_argument("--max-files", type=int, default=32)
    parser.add_argument("--apply", action="store_true")
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2))
