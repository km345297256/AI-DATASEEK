#!/usr/bin/env python3
"""Run one model-authored dataset analysis program with a strict result contract."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path


MAX_PROGRAM_BYTES = 256 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_ATTACHMENTS = 32
OUTPUT_ROOT = Path("/home/ubuntu/output").resolve()


def _install_json_compatibility() -> None:
    """Allow model-authored programs to persist NumPy scalar/array evidence."""
    encoder = json.JSONEncoder
    original_default = encoder.default

    def default(self, value):
        item = getattr(value, "item", None)
        if callable(item):
            try:
                return item()
            except Exception:
                pass
        tolist = getattr(value, "tolist", None)
        if callable(tolist):
            try:
                return tolist()
            except Exception:
                pass
        return original_default(self, value)

    encoder.default = default


def _error(message: str) -> int:
    print(json.dumps({"success": False, "error": message}, ensure_ascii=False))
    return 2


def _safe_output_path(value: object, output_dir: Path) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        return None
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    try:
        resolved.relative_to(output_dir)
    except ValueError:
        return None
    return resolved if resolved.is_file() else None


def _load_result(result_path: Path, output_dir: Path) -> dict:
    if not result_path.is_file() or result_path.stat().st_size > MAX_RESULT_BYTES:
        raise ValueError("analysis result manifest is missing or too large")
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("analysis result manifest must be an object")
    result = payload.get("result")
    if not isinstance(result, str) or not result.strip():
        raise ValueError("analysis result must contain a substantive result")
    attachments = payload.get("attachments", [])
    if not isinstance(attachments, list) or len(attachments) > MAX_ATTACHMENTS:
        raise ValueError("analysis attachments are invalid")
    validated: list[str] = []
    for item in attachments:
        path = _safe_output_path(item, output_dir)
        if path is None:
            raise ValueError("analysis attachment is missing or outside the output directory")
        validated.append(str(path))
    return {
        "success": bool(payload.get("success", True)),
        "result": result,
        "attachments": validated,
        "evidence": payload.get("evidence", {}),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one bounded dataset analysis program")
    parser.add_argument("--program-base64", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--result-path", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = Path(args.output_dir)
    result_path = Path(args.result_path)
    try:
        output_dir = output_dir.resolve()
        result_path = result_path.resolve()
        output_dir.relative_to(OUTPUT_ROOT)
        result_path.relative_to(output_dir)
        output_dir.mkdir(parents=True, exist_ok=False)
        encoded = args.program_base64.encode("ascii", errors="strict")
        program = base64.b64decode(encoded, validate=True)
        if not program or len(program) > MAX_PROGRAM_BYTES:
            return _error("analysis program is empty or too large")
        source = program.decode("utf-8", errors="strict")
    except (ValueError, UnicodeError, OSError, base64.binascii.Error) as exc:
        return _error(f"invalid analysis program or output path: {type(exc).__name__}")

    os.environ["AI_DATASEEK_OUTPUT_DIR"] = str(output_dir)
    os.environ["AI_DATASEEK_RESULT_PATH"] = str(result_path)
    try:
        _install_json_compatibility()
        namespace = {"__name__": "__main__"}
        exec(compile(source, "<dataset-analysis>", "exec"), namespace, namespace)
        return_code = 0
    except Exception as exc:  # The caller receives a repairable, typed failure.
        print(json.dumps({"success": False, "error": f"analysis program failed: {type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return_code = 1

    if return_code:
        return return_code
    try:
        result = _load_result(result_path, output_dir)
    except Exception as exc:
        return _error(str(exc))
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
