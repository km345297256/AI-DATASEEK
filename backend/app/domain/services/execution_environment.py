from __future__ import annotations

import hashlib
import math
from typing import Any, Iterable

from app.application.services.dataset_request_resolver import (
    DECISION_PROMPT,
    FRONT_CONTROLLER_PROMPT_VERSION,
    FrontControllerResolution,
)
from app.core.config import get_settings
from app.domain.models.execution_environment import (
    CordisCatalogIdentity,
    ExecutionEnvironmentSnapshot,
    ModelExecutionIdentity,
    SandboxExecutionIdentity,
    ToolsetExecutionIdentity,
    safe_public_identifier,
    stable_sha256,
)
from app.domain.services.execution_identity import private_identity_hmac


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _safe_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _safe_image_reference(value: object) -> str | None:
    text = str(value or "").strip()
    return text if text and safe_public_identifier(text) == text else None


def _safe_image_digest(value: object) -> str | None:
    text = str(value or "").strip().lower()
    raw = text.removeprefix("sha256:")
    if len(raw) != 64 or any(character not in "0123456789abcdef" for character in raw):
        return None
    return f"sha256:{raw}" if text.startswith("sha256:") else raw


def _dataset_mount_identity(dataset_ids: Iterable[object]) -> tuple[int, str]:
    # Dataset ids are opaque database identifiers. If an invalid/free-text
    # value reaches this boundary, replace it before hashing so a user-provided
    # host path or secret cannot become digest input.
    raw_identifiers = {str(item) for item in dataset_ids}
    safe_identifiers = sorted({
        safe
        for item in raw_identifiers
        if (safe := safe_public_identifier(item)) != "redacted"
    })
    redacted_count = len(raw_identifiers) - len(safe_identifiers)
    # Preserve the mount count without putting unsafe values into a digest.
    # Only the number of rejected values is included in the identity.
    digest_input = {
        "identifiers": safe_identifiers,
        "redacted_count": redacted_count,
    }
    return len(raw_identifiers), stable_sha256(digest_input)


def _catalog_identity(flow: object) -> CordisCatalogIdentity:
    toolkit = getattr(flow, "plugin_toolkit", None)
    snapshot = getattr(toolkit, "catalog_snapshot", None)
    if snapshot is None:
        return CordisCatalogIdentity(
            status="legacy",
            engine="legacy-filesystem",
            version="legacy",
            plugin_count=0,
            tool_count=len(getattr(toolkit, "_definitions", {}) or {}),
        )
    revision = str(getattr(snapshot, "revision", "") or "")
    if not revision:
        return CordisCatalogIdentity(
            status="unavailable",
            engine="cordis",
            version="unavailable",
            plugin_count=0,
            tool_count=0,
        )
    return CordisCatalogIdentity(
        status="ready",
        engine="cordis",
        version=str(snapshot.version),
        revision=revision,
        manifest_digest=str(snapshot.manifest_digest),
        execution_bundle_digest=str(snapshot.execution_bundle_digest),
        plugin_count=int(snapshot.plugin_count),
        tool_count=int(snapshot.tool_count),
    )


def _sandbox_identity(
    sandbox: object,
    *,
    dataset_ids: Iterable[object],
) -> SandboxExecutionIdentity:
    node = getattr(sandbox, "node", None)
    raw_sandbox = getattr(sandbox, "sandbox", sandbox)
    node_type = getattr(getattr(node, "type", None), "value", None)
    runtime = str(node_type or getattr(raw_sandbox, "_execution_runtime_kind", "") or "")
    if not runtime:
        class_name = type(raw_sandbox).__name__.lower()
        if "worker" in class_name:
            runtime = "worker_agent"
        elif "docker" in class_name:
            runtime = "remote_docker" if getattr(raw_sandbox, "_docker_host", None) else "local_docker"
        else:
            runtime = "custom"
    if safe_public_identifier(runtime) != runtime:
        runtime = "custom"

    image_reference = _safe_image_reference(
        getattr(raw_sandbox, "_image_reference", None)
    )
    if image_reference is None and node is not None:
        runtime_config = getattr(node, "runtime_config", None)
        if isinstance(runtime_config, dict):
            image_reference = _safe_image_reference(runtime_config.get("image"))
    if image_reference is None and runtime in {
        "local_docker",
        "remote_docker",
        "worker_agent",
    }:
        image_reference = _safe_image_reference(get_settings().sandbox_image)

    image_digest = _safe_image_digest(
        getattr(raw_sandbox, "_image_digest", None)
    )
    mount_count, mounts_digest = _dataset_mount_identity(dataset_ids)
    configuration_digest = stable_sha256({
        "runtime": runtime,
        "image_reference": image_reference,
        "image_digest": image_digest,
        "dataset_mount_count": mount_count,
        "dataset_mounts_digest": mounts_digest,
    })
    return SandboxExecutionIdentity(
        runtime=runtime,
        image_reference=image_reference,
        image_digest=image_digest,
        dataset_mount_count=mount_count,
        dataset_mounts_digest=mounts_digest,
        configuration_digest=configuration_digest,
    )


def _agent_model_identity(role: str, agent: object) -> ModelExecutionIdentity:
    provider = safe_public_identifier(getattr(agent, "_model_provider", None))
    model = safe_public_identifier(getattr(agent, "_model_name", None))
    # BaseAgent captures this before applying any user profile override. Never
    # hash the effective prompt because it can contain private user material.
    builtin_prompt = getattr(agent, "_execution_builtin_system_prompt", None)
    prompt_digest = _text_sha256(builtin_prompt) if isinstance(builtin_prompt, str) else None
    raw_configuration = getattr(agent, "_execution_model_configuration", None)
    raw_configuration = raw_configuration if isinstance(raw_configuration, dict) else {}
    custom_prompt_hmac = raw_configuration.get("custom_prompt_hmac_sha256")
    if raw_configuration.get("has_custom_system_prompt") and not custom_prompt_hmac:
        raise RuntimeError(
            f"Agent role {role} did not capture its custom prompt identity"
        )
    configuration = {
        "provider": provider,
        "model": model,
        "temperature": _safe_number(raw_configuration.get("temperature")),
        "max_tokens": _safe_number(raw_configuration.get("max_tokens")),
        "max_iterations": _safe_number(raw_configuration.get("max_iterations")),
        "has_custom_system_prompt": bool(raw_configuration.get("has_custom_system_prompt")),
        "custom_prompt_hmac_sha256": custom_prompt_hmac,
        "builtin_prompt_digest": prompt_digest,
    }
    runtime = raw_configuration.get("model_runtime")
    if isinstance(runtime, dict):
        configuration["model_runtime"] = {
            "driver_version": safe_public_identifier(runtime.get("driver_version")),
            "estimator_version": safe_public_identifier(runtime.get("estimator_version")),
            **{key: _safe_number(runtime.get(key)) for key in (
                "context_capacity_tokens", "context_safety_tokens", "task_token_budget", "task_call_budget",
            )},
        }
    return ModelExecutionIdentity(
        role=role,
        provider=provider,
        model=model,
        prompt_digest=prompt_digest,
        custom_prompt_hmac_sha256=custom_prompt_hmac,
        configuration_digest=stable_sha256(configuration),
    )


def _front_controller_identity(
    resolution: FrontControllerResolution | None,
    llm_overrides: dict[str, Any] | None,
) -> ModelExecutionIdentity | None:
    if resolution is None:
        return None
    metadata = resolution.controller_metadata if isinstance(resolution.controller_metadata, dict) else {}
    provider = safe_public_identifier(metadata.get("model_provider"))
    model = safe_public_identifier(metadata.get("model_name"))
    prompt_version = safe_public_identifier(
        metadata.get("prompt_version") or FRONT_CONTROLLER_PROMPT_VERSION
    )
    overrides = llm_overrides if isinstance(llm_overrides, dict) else {}
    configured_max_tokens = overrides.get("max_tokens")
    effective_max_tokens = (
        min(configured_max_tokens, 1000)
        if isinstance(configured_max_tokens, int) and not isinstance(configured_max_tokens, bool)
        else 1000
    )
    prompt_digest = _text_sha256(DECISION_PROMPT)
    configuration = {
        "provider": provider,
        "model": model,
        "temperature": 0,
        "max_tokens": effective_max_tokens,
        "timeout_seconds": _safe_number(
            get_settings().dataset_request_resolver_timeout_seconds
        ),
        "prompt_version": prompt_version,
        "prompt_digest": prompt_digest,
    }
    return ModelExecutionIdentity(
        role="front_controller",
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        prompt_digest=prompt_digest,
        configuration_digest=stable_sha256(configuration),
    )


def _toolset_identity(
    flow: object,
    *,
    requested_mcp_servers: Iterable[object],
    requested_skill_count: int,
) -> ToolsetExecutionIdentity | None:
    captured = getattr(flow, "_execution_toolset_identity", None)
    if not isinstance(captured, dict):
        return None
    requested_mcp_names = sorted({str(name) for name in requested_mcp_servers})
    # Every captured field below is produced internally by PlanActFlow from the
    # actual post-discovery registry. Private source values are converted to a
    # server-keyed HMAC before this strict persisted model is constructed.
    return ToolsetExecutionIdentity(
        policy_version=str(captured["policy_version"]),
        policy_digest=str(captured["policy_digest"]),
        tool_names_digest=str(captured["tool_names_digest"]),
        tool_count=int(captured["tool_count"]),
        mcp_tool_count=int(captured["mcp_tool_count"]),
        requested_mcp_count=len(requested_mcp_names),
        requested_skill_count=max(0, requested_skill_count),
        active_skill_count=max(0, int(captured.get("active_skill_count", 0))),
        mcp_tools_hmac_sha256=captured.get("mcp_tools_hmac_sha256"),
        requested_mcp_hmac_sha256=(
            private_identity_hmac({"requested_mcp_servers": requested_mcp_names})
            if requested_mcp_names
            else None
        ),
        active_skill_hmac_sha256=captured.get("active_skill_hmac_sha256"),
        selection=captured.get("selection"),
    )


def create_agent_execution_snapshot(
    *,
    task_id: str,
    session_id: str,
    flow: object,
    sandbox: object,
    dataset_ids: Iterable[object],
    resolution: FrontControllerResolution | None,
    llm_overrides: dict[str, Any] | None,
    requested_mcp_servers: Iterable[object],
    requested_skill_count: int,
    trigger_event_seq: int | None = None,
) -> ExecutionEnvironmentSnapshot:
    models: list[ModelExecutionIdentity] = []
    controller = _front_controller_identity(resolution, llm_overrides)
    if controller is not None:
        models.append(controller)
    for role in ("planner", "execution", "vision"):
        agent = getattr(flow, role if role != "execution" else "executor", None)
        if agent is not None:
            models.append(_agent_model_identity(role, agent))
    for agent in getattr(flow, "_domain_agents", {}).values():
        models.append(_agent_model_identity(agent.name, agent))
    return ExecutionEnvironmentSnapshot.create(
        task_id=task_id,
        session_id=session_id,
        execution_mode="agent",
        catalog=_catalog_identity(flow),
        sandbox=_sandbox_identity(sandbox, dataset_ids=dataset_ids),
        models=tuple(models),
        toolset=_toolset_identity(
            flow,
            requested_mcp_servers=requested_mcp_servers,
            requested_skill_count=requested_skill_count,
        ),
        trigger_event_seq=trigger_event_seq,
    )


def create_lightweight_execution_snapshot(
    *,
    task_id: str,
    session_id: str,
    resolution: FrontControllerResolution,
    llm_overrides: dict[str, Any] | None,
    trigger_event_seq: int | None = None,
) -> ExecutionEnvironmentSnapshot:
    controller = _front_controller_identity(resolution, llm_overrides)
    return ExecutionEnvironmentSnapshot.create(
        task_id=task_id,
        session_id=session_id,
        execution_mode="lightweight",
        catalog=None,
        sandbox=None,
        models=(controller,) if controller is not None else (),
        toolset=None,
        trigger_event_seq=trigger_event_seq,
    )
