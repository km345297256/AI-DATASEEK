"""Download reviewed public descriptors; never run from an HTTP request.

Each descriptor is operator-reviewed JSON, not input from a website or model
tool call. Writes only new, specific dataset directories. Failed/incomplete
directories are retained for inspection and never registered or overwritten.
Original archives are retained; bounded safe ZIP/GZIP extraction makes the
analysis-ready files available without repeated decompression in each session.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import json
import mimetypes
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
from urllib.parse import urlsplit, urlunsplit
import zipfile

SOURCES = {
    "scidb": "ScienceDB 科学数据银行",
    "tpdc": "国家青藏高原科学数据中心",
    "chemdc": "国家基础学科公共科学数据中心·化学专题",
    "ngdc": "国家基因组科学数据中心 NGDC / CNCB",
}
DOMAINS = {"general", "tabular", "geoscience", "image_science", "chemistry", "sequence", "space", "documents", "spectroscopy"}
MAX_DOWNLOAD = 128 * 1024**2
MAX_EXTRACT = 256 * 1024**2
MAX_FILES = 10000


def safe_relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or "\x00" in value:
        raise ValueError("Unsafe relative path")
    return path


def public_url(value):
    url = urlsplit(value)
    allowed = ("scidb.cn", "tpdc.ac.cn", "casdc.cn", "cncb.ac.cn")
    if url.username or url.password or url.port or not url.hostname:
        raise ValueError("Not a reviewed public source URL")
    if not any(url.hostname == domain or url.hostname.endswith("." + domain) for domain in allowed):
        raise ValueError("Download host is not in the reviewed source families")
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname.endswith("casdc.cn")):
        raise ValueError("Only public HTTPS or ChemDC HTTP is supported")
    return value


def provenance_url(spec, source_url):
    """Never publish credentials or signed/temporary download query strings."""
    url = urlsplit(spec["url"])
    if any(part in url.query.lower() for part in ("token", "signature", "credential", "expires", "secret", "access_key")):
        return source_url
    return urlunsplit((url.scheme, url.netloc, url.path, url.query, ""))


def new_directory(path):
    # resolve(strict=False) also detects existing symlink parents.
    if path.resolve() != path.absolute():
        raise ValueError("Refusing symlinked destination")
    path.mkdir(parents=True, exist_ok=False)


def download(spec, destination):
    public_url(spec["url"])
    method = spec.get("method", "GET").upper()
    if method not in {"GET", "POST"}:
        raise ValueError("Unsupported public download method")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents overwrites even if a descriptor repeats a path.
    with destination.open("xb") as output:
        command = ["curl", "--fail", "--silent", "--show-error",
                   "--write-out", "%{stderr}%{http_code}",
                   "--connect-timeout", "20", "--max-time", "180",
                   "--max-filesize", str(MAX_DOWNLOAD), "--proto", "=http,https",
                   "--request", method]
        if method == "POST":
            command += ["--header", "Content-Type: application/json", "--data", json.dumps(spec.get("body", {}))]
        command.append(spec["url"])
        result = subprocess.run(command, stdout=output, stderr=subprocess.PIPE)
    if result.returncode:
        # curl may include signed URLs in errors. Keep output limited to code.
        raise ValueError(f"Download failed (curl {result.returncode})")
    if not result.stderr.endswith(b"200"):
        raise ValueError("Download requires a non-200 response or unreviewed redirect")
    size = destination.stat().st_size
    if not 0 < size <= MAX_DOWNLOAD:
        raise ValueError("Empty or oversized download")
    expected = spec.get("expected_size")
    if expected is not None and size != expected:
        raise ValueError(f"Downloaded size differs from source: {size} != {expected}")
    if spec.get("expected_md5"):
        digest = hashlib.md5()
        with destination.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
        if digest.hexdigest() != spec["expected_md5"].lower():
            raise ValueError("Download differs from the publisher's MD5")
    with destination.open("rb") as source:
        head = source.read(512).lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html", b'{"code"', b'{"status"', b'{"msg"')):
        raise ValueError("Source returned an error/login document instead of data")
    fmt = spec.get("format", "").lower()
    if fmt in {"zip", "xlsx"} and not zipfile.is_zipfile(destination):
        raise ValueError("Expected ZIP/XLSX container")
    if fmt.startswith("gzip") and not head.startswith(b"\x1f\x8b"):
        raise ValueError("Expected GZIP container")
    return size


def extract_archive(path, dataset_dir, remaining):
    outputs = []
    if path.suffix.lower() == ".zip":
        target = dataset_dir / "extracted" / path.stem
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES or sum(item.file_size for item in entries) > remaining:
                raise ValueError("Archive exceeds extraction limits")
            for entry in entries:
                relative = safe_relative(entry.filename)
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or entry.flag_bits & 1:
                    raise ValueError("Encrypted or symlink archive entries are not supported")
                if entry.is_dir():
                    continue
                output = target.joinpath(*relative.parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, output.open("xb") as sink:
                    shutil.copyfileobj(source, sink)
                outputs.append(output)
    elif path.suffix.lower() == ".gz":
        target = dataset_dir / "extracted" / path.stem
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with gzip.open(path, "rb") as source, target.open("xb") as sink:
            while block := source.read(1024 * 1024):
                written += len(block)
                if written > remaining:
                    raise ValueError("GZIP exceeds extraction limit")
                sink.write(block)
        if not written:
            raise ValueError("Empty compressed data")
        outputs.append(target)
    return outputs


def declaration(path, dataset_dir, role="data"):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return {"path": str(path.relative_to(dataset_dir)), "size": path.stat().st_size,
            "sha256": digest.hexdigest(), "role": role,
            "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream"}


def prepare(item, source, data_root, catalog_root):
    dataset_id = item["dataset_id"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,126}[a-z0-9]", dataset_id):
        raise ValueError("Unsafe dataset identity")
    if item["domain"] not in DOMAINS or not item.get("license") or not item.get("sample_scope"):
        raise ValueError("Missing domain, license or scope")
    directory = data_root / source / dataset_id
    manifest_path = catalog_root / source / f"{dataset_id}.json"
    descriptor_digest = hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    if manifest_path.exists():
        # Resume only a byte-identical complete registration; never silently
        # overwrite an existing manifest/data tree or heal mismatches.
        manifest = json.loads(manifest_path.read_text())
        if manifest["metadata"].get("source_descriptor_sha256") != descriptor_digest:
            raise ValueError("Existing manifest belongs to a different or unrecorded descriptor")
        actual = [declaration(path, directory, "documentation" if path.name == "SOURCE.md" else "data")
                  for path in sorted(directory.rglob("*")) if path.is_file()]
        if actual != manifest["files"]:
            raise ValueError("Existing downloaded dataset does not match manifest")
        return {"dataset_id": dataset_id, "source": source, "status": "already_verified", "files": len(actual), "bytes": sum(f["size"] for f in actual)}
    new_directory(directory)
    provenance = []
    extracted_size = 0
    for spec in item["files"]:
        relative = safe_relative(spec["path"])
        path = directory.joinpath(*relative.parts)
        download(spec, path)
        entry = declaration(path, directory)
        entry["download_url"] = provenance_url(spec, item["source_url"])
        provenance.append(entry)
        extracted = extract_archive(path, directory, MAX_EXTRACT - extracted_size)
        extracted_size += sum(p.stat().st_size for p in extracted)
    source_text = "\n".join([
        f"# {item['name']}", "", f"来源：{SOURCES[source]}",
        f"官方页面：{item['source_url']}", f"许可：{item['license']}",
        f"作者：{', '.join(item.get('authors', []))}",
        f"引用标识：{item.get('doi') or item.get('publication_url') or item.get('external_id', '')}",
        f"来源版本：{item.get('source_version') or item.get('release_time', '')}",
        f"许可说明：{item.get('license_details', '')}", f"许可链接：{item.get('license_url', '')}",
        f"集成范围：{item['sample_scope']}", "", item["description"], "",
        "下载日期：2026-09-07。保留原始下载文件；ZIP/GZIP 仅进行无损解压。",
        "本目录仅供本机分析，必须遵守来源的署名、非商业或其他使用条件。",
        "SHA-256 为本次下载计算的完整性基线，不等同于发布机构的数字签名。", "",
    ])
    with (directory / "SOURCE.md").open("x", encoding="utf-8") as output:
        output.write(source_text)
    files = [declaration(path, directory, "documentation" if path.name == "SOURCE.md" else "data")
             for path in sorted(directory.rglob("*")) if path.is_file()]
    if len(files) > MAX_FILES:
        raise ValueError("Dataset exceeds file inventory limit")
    manifest = {
        "dataset_id": dataset_id, "external_id": item.get("external_id", dataset_id),
        "data_center_id": f"reviewed-{source}-catalog", "data_center_name": SOURCES[source],
        "name": item["name"], "description": item["description"], "domain": item["domain"],
        "data_type": item.get("data_type", ""), "tags": [item["domain"], SOURCES[source], "公开数据"],
        "metadata": {"curated": True, "catalog_version": 1, "source_catalog": source,
                     "publisher": item.get("publisher", SOURCES[source]),
                     "source_url": item["source_url"], "license": item["license"],
                     "license_url": item.get("license_url", ""),
                     "license_details": item.get("license_details", ""),
                     "sample_scope": item["sample_scope"], "sample_note": item.get("sample_note", item["sample_scope"]),
                     "authors": item.get("authors", []), "doi": item.get("doi", ""),
                     "source_version": item.get("source_version", ""),
                     "source_descriptor_sha256": descriptor_digest,
                     **{key: item[key] for key in ("organism", "assembly_level", "bioproject", "publication_url", "release_time") if item.get(key)},
                     "download_date": "2026-09-07", "provenance": provenance,
                     "inventory_complete": True, "recursive_file_count": len(files),
                     "total_size_bytes": sum(f["size"] for f in files)}, "files": files,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("x", encoding="utf-8") as output:
        json.dump(manifest, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return {"dataset_id": dataset_id, "source": source, "status": "verified", "files": len(files), "bytes": sum(f["size"] for f in files)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--descriptor", type=Path, required=True)
    parser.add_argument("--source", choices=SOURCES, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--catalog-root", type=Path, required=True)
    args = parser.parse_args()
    if not args.data_root.is_absolute() or args.data_root.name != "open-catalog" or args.data_root.resolve() != args.data_root:
        parser.error("data-root must be a specific, non-symlink absolute open-catalog directory")
    raw = json.loads(args.descriptor.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else raw["datasets"]
    if not 1 <= len(items) <= 10 or len({x["dataset_id"] for x in items}) != len(items):
        parser.error("Each source batch must contain 1–10 distinct reviewed datasets")
    failed = False
    # Only two concurrent downloads per institution; no load-generating crawl.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(prepare, item, args.source, args.data_root, args.catalog_root): item["dataset_id"] for item in items}
        for future in concurrent.futures.as_completed(futures):
            try:
                print(json.dumps(future.result(), ensure_ascii=False), flush=True)
            except Exception as exc:
                failed = True
                print(json.dumps({"dataset_id": futures[future], "status": "not_registered", "error": str(exc)}, ensure_ascii=False), flush=True)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
