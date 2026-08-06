from __future__ import annotations


LEGACY_SANDBOX_NAME_PREFIX = "sandbox"


def is_sandbox_container_name(
    name: object,
    configured_prefix: str | None,
) -> bool:
    """Return whether a Docker name belongs to an AI-DataSeek sandbox.

    Deployments use project-specific prefixes such as
    ``ai-dataseek-sandbox``.  The legacy ``sandbox`` prefix remains accepted
    so upgrades continue to account for containers created before the rename.
    """

    if not isinstance(name, str):
        return False
    normalized_name = name.lstrip("/")
    prefixes = {LEGACY_SANDBOX_NAME_PREFIX}
    if isinstance(configured_prefix, str) and configured_prefix.strip():
        prefixes.add(configured_prefix.strip().rstrip("-"))
    for prefix in prefixes:
        marker = f"{prefix}-"
        if not normalized_name.startswith(marker):
            continue
        remainder = normalized_name.removeprefix(marker)
        # Compose's one-shot image builder is commonly named
        # ``<project>-sandbox-image-1`` and shares the configured prefix, but
        # it is not an analysis sandbox.
        if remainder == "image" or remainder.startswith("image-"):
            continue
        return True
    return False
