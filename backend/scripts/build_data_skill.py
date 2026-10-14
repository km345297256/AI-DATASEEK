"""Build a validated reproducible ZIP; never execute package scripts or install it."""
import argparse
import io
import json
import zipfile
from pathlib import Path

from app.domain.services.skills.package import read_package, parse_manifest, package_digest


def build(root: Path, output: Path) -> dict:
    if output.resolve().is_relative_to(root.resolve()):
        raise ValueError("Output must be outside package directory")
    files = read_package(root)
    if not files.get("SKILL.md", b"").decode("utf-8").strip():
        raise ValueError("Missing skill instructions")
    manifest = parse_manifest(files["skill.yaml"])
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    with output.open("xb") as stream:
        stream.write(result.getvalue())
    return {"package_id": manifest.package_id, "version": manifest.version,
            "package_sha256": package_digest(files), "verification_status": "unverified"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        receipt = build(args.directory, args.output)
    except (ValueError, OSError, KeyError):
        print(json.dumps({"valid": False, "error": "Invalid package or unavailable output; existing files are not overwritten"}))
        return 1
    print(json.dumps({"valid": True, **receipt}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
