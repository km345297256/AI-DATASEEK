"""Run from backend: uv run --frozen python -m scripts.validate_data_skill DIR."""

import argparse
import json
from pathlib import Path

from app.domain.services.skills.package import parse_manifest, read_package, package_digest


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Data Skill v1 package without executing scripts")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        files = read_package(args.directory)
        if not files.get("SKILL.md", b"").decode("utf-8").strip():
            raise ValueError("Missing skill instructions")
        manifest = parse_manifest(files["skill.yaml"])
    except (ValueError, OSError, KeyError):
        print(json.dumps({"valid": False, "error": "Invalid or incomplete Data Skill package"}))
        return 1
    print(json.dumps({
        "valid": True, "package_id": manifest.package_id, "version": manifest.version,
        "package_sha256": package_digest(files), "file_count": len(files),
        "verification_status": "unverified",
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
