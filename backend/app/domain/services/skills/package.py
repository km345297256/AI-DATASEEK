"""Bounded package reading and reproducible identities for Data Skills."""

import hashlib
import json
from pathlib import Path

import yaml

from app.domain.models.skill_manifest import SkillManifest

MAX_PACKAGE_BYTES = 16 * 1024 * 1024
MAX_PACKAGE_FILES = 512
MAX_MANIFEST_BYTES = 128 * 1024


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError("Skill YAML requires unique string keys")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def parse_manifest(raw: bytes) -> SkillManifest:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Skill manifest exceeds size limit")
    try:
        for token in yaml.scan(raw):
            if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
                raise ValueError("Skill YAML aliases and anchors are not supported")
        data = yaml.load(raw, Loader=UniqueKeyLoader)
        return SkillManifest.model_validate(data)
    except (yaml.YAMLError, UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid skill YAML") from exc


def read_package(root: Path) -> dict[str, bytes]:
    """Read publishable bytes; local ownership metadata is not a release file."""
    if root.is_symlink():
        raise ValueError("Skill packages cannot contain symbolic links")
    result = {}
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ValueError("Skill packages cannot contain symbolic links")
        if any(part.startswith(".") for part in relative.parts) or relative.as_posix() == "metadata.json":
            continue
        if not path.is_file():
            continue
        if len(result) >= MAX_PACKAGE_FILES:
            raise ValueError("Skill package contains too many files")
        with path.open("rb") as stream:
            content = stream.read(MAX_PACKAGE_BYTES - total + 1)
        total += len(content)
        if total > MAX_PACKAGE_BYTES:
            raise ValueError("Skill package exceeds size limit")
        result[relative.as_posix()] = content
    return result


def package_digest(files: dict[str, bytes]) -> str:
    entries = [(name, hashlib.sha256(data).hexdigest()) for name, data in sorted(files.items())]
    return hashlib.sha256(json.dumps(entries, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
