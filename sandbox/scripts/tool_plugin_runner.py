#!/usr/bin/env python3
"""Run trusted Tool plugins installed in the sandbox image."""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def tools_directory() -> Path:
    return Path(os.getenv("AI_DATASEEK_TOOLS_DIR", "/opt/ai-dataseek/tools")).resolve()


def load_registry(root: Path) -> dict[str, tuple[dict[str, Any], Path]]:
    registry: dict[str, tuple[dict[str, Any], Path]] = {}
    if not root.is_dir():
        raise RuntimeError(f"Tool plugin directory does not exist: {root}")
    for manifest_path in sorted(root.glob("*/manifest.json")):
        plugin_dir = manifest_path.parent.resolve()
        if root not in plugin_dir.parents:
            raise RuntimeError(f"Plugin escapes tool directory: {plugin_dir}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        handler_name = manifest.get("handler", "handler.py")
        handler_path = (plugin_dir / handler_name).resolve()
        if plugin_dir not in handler_path.parents or not handler_path.is_file():
            raise RuntimeError(f"Invalid plugin handler: {handler_path}")
        for definition in manifest.get("tools", []):
            name = definition.get("name") if isinstance(definition, dict) else None
            if not isinstance(name, str) or not name:
                raise RuntimeError(f"Invalid tool definition in {manifest_path}")
            if name in registry:
                raise RuntimeError(f"Duplicate plugin tool name: {name}")
            registry[name] = (definition, handler_path)
    return registry


def load_handler(path: Path):
    module_name = f"ai_dataseek_tool_{path.parent.name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load plugin handler: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    build_command = getattr(module, "build_command", None)
    if not callable(build_command):
        raise RuntimeError(f"Plugin handler has no build_command(): {path}")
    return build_command


def decode_arguments(value: str) -> dict[str, Any]:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
        result = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Invalid plugin arguments") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Plugin arguments must be a JSON object")
    return result


def run_tool(name: str, encoded_arguments: str) -> int:
    definition, handler_path = load_registry(tools_directory()).get(name, (None, None))
    if definition is None or handler_path is None:
        raise RuntimeError(f"Unknown plugin tool: {name}")
    command = load_handler(handler_path)(name, decode_arguments(encoded_arguments))
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise RuntimeError(f"Plugin {name} returned an invalid command")
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI-DataSeek Tool plugin runner")
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="List registered tools")
    list_parser.add_argument("--json", action="store_true")
    run_parser = subparsers.add_parser("run", help="Run one registered tool")
    run_parser.add_argument("name")
    run_parser.add_argument("--arguments-base64", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            names = sorted(load_registry(tools_directory()))
            print(json.dumps(names) if args.json else "\n".join(names))
            return 0
        return run_tool(args.name, args.arguments_base64)
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
