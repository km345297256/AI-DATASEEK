"""Private, bounded checkpoints. A continuation is not another free-form prompt."""
from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from app.domain.models.plan import Plan
from app.core.config import get_settings
from app.domain.services.execution_identity import private_identity_hmac

SOURCE_ROOT = "/home/ubuntu/datasets/"
UPLOAD_ROOT = "/home/ubuntu/inputs/"
OUTPUT_ROOT = "/home/ubuntu/output/"
MAX_FILES = 64


def _safe_upload_path(value) -> bool:
    if (not isinstance(value, str) or not value.startswith(UPLOAD_ROOT)
            or len(value) > 2048 or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or str(PurePosixPath(value)) != value or ".." in PurePosixPath(value).parts):
        return False
    parts = PurePosixPath(value).relative_to(UPLOAD_ROOT).parts
    return len(parts) == 2 and bool(re.fullmatch(r"[0-9a-f]{24}", parts[0]))


def _safe_path(value, *, source_only=False, output_only=False, approved_upload_paths=()) -> bool:
    roots = (SOURCE_ROOT,) if source_only else (OUTPUT_ROOT,) if output_only else (SOURCE_ROOT, OUTPUT_ROOT)
    return (isinstance(value, str) and len(value) <= 2048
            and (value.startswith(roots) or (not output_only and value in approved_upload_paths and _safe_upload_path(value)))
            and "\\" not in value and not any(ord(char) < 32 for char in value)
            and ".." not in PurePosixPath(value).parts and str(PurePosixPath(value)) == value)


def _validated_fingerprints(records, *, expected_paths=None, approved_upload_paths=()) -> list[dict] | None:
    if not isinstance(records, list) or not records or len(records) > MAX_FILES:
        return None
    normalized, seen = [], set()
    for item in records:
        if (not isinstance(item, dict) or not _safe_path(item.get("path"), approved_upload_paths=approved_upload_paths)
                or item["path"] in seen or type(item.get("size")) is not int or item["size"] < 0
                or not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            return None
        seen.add(item["path"])
        normalized.append({key: item[key] for key in ("path", "size", "sha256")})
    if expected_paths is not None and seen != set(expected_paths):
        return None
    return sorted(normalized, key=lambda item: item["path"])


def _checkpoint_digest(checkpoint) -> str:
    immutable = {key: value for key, value in checkpoint.items() if key not in {"claimed_by", "checkpoint_digest"}}
    # PyMongo may restore UTC datetimes without tzinfo; use the same canonical
    # representation before save and after read.
    expiry = immutable.get("expires_at")
    if isinstance(expiry, datetime):
        expiry = expiry.replace(tzinfo=UTC) if expiry.tzinfo is None else expiry.astimezone(UTC)
        immutable["expires_at"] = expiry.replace(microsecond=(expiry.microsecond // 1000) * 1000).isoformat()
    return private_identity_hmac({"purpose": "analysis-checkpoint-content/v1", "checkpoint": immutable})


def configuration_digest(session) -> str:
    # Match the model factory's effective defaults, rather than hashing only
    # overrides (which leaves unprofiled sessions unlocked across deployment).
    # The existing keyed identity boundary never retains the credentialed data.
    settings = get_settings()
    overrides = dict(session.llm_overrides or {})
    effective = {
        "model_provider": (overrides.pop("model_provider", None) or settings.model_provider or "openai").lower().strip(),
        "model_name": overrides.pop("model_name", None) or settings.model_name,
        "api_base": overrides.pop("api_base", None) or settings.api_base,
        "api_key": overrides.pop("api_key", None) or settings.api_key,
        "extra_headers": settings.extra_headers,
    }
    for key in ("temperature", "max_tokens"):
        value = overrides.pop(key, None)
        effective[key] = value if value is not None else getattr(settings, key)
    effective["execution_max_tokens"] = max(effective["max_tokens"], settings.execution_max_tokens)
    return private_identity_hmac({"purpose": "analysis-checkpoint-configuration/v1",
                                  "effective_model": effective, "profile": overrides})


def _upload_scope_valid(message) -> bool:
    """Attachment presence is safe only with a complete server-owned manifest.

    A path-shaped string or an attachment ID alone cannot authorize input bytes.
    Bind every upload identity, namespace, staged path and size to the manifest.
    """
    context = getattr(message, "analysis_inputs", None)
    if context is None:
        return not (message.attachment_file_ids or message.attachments or message.attachment_file_infos)
    files = [item for source in context.sources if source.kind == "upload" for item in source.files]
    identities = [item.file_id for item in files]
    paths = [item.runtime_path for item in files]
    if (len(set(identities)) != len(identities) or len(set(paths)) != len(paths)
            or any(not item.file_id or not _safe_upload_path(item.runtime_path)
                   or type(item.size) is not int or item.size < 0 for item in files)
            or any(item.source_id != source.source_id for source in context.sources for item in source.files)
            or sorted(identities) != sorted(message.attachment_file_ids)
            or sorted(paths) != sorted(message.attachments)
            or len(message.attachment_file_infos) != len(files)):
        return False
    infos = {item.file_id: item for item in message.attachment_file_infos}
    return len(infos) == len(files) and all(
        item.file_id in infos and infos[item.file_id].file_path == item.runtime_path
        and infos[item.file_id].size == item.size for item in files
    )


def approved_upload_paths(message) -> list[str]:
    context = getattr(message, "analysis_inputs", None)
    return list(context.upload_paths) if context is not None and _upload_scope_valid(message) else []


def _input_manifest(message):
    context = getattr(message, "analysis_inputs", None)
    return context.model_dump(mode="json") if context is not None else None


def source_paths(message) -> list[str]:
    if not _upload_scope_valid(message):
        return []
    inventory = set()
    for dataset in message.datasets:
        if not _safe_path(dataset.sandbox_path, source_only=True):
            return []
        for item in dataset.files:
            if (not isinstance(item.path, str) or not item.path or PurePosixPath(item.path).is_absolute()
                    or any(part in {"", ".", ".."} for part in item.path.split("/"))):
                return []
            path = str(PurePosixPath(dataset.sandbox_path) / item.path)
            if not _safe_path(path, source_only=True):
                return []
            inventory.add(path)
    context = getattr(message, "analysis_inputs", None)
    if context is not None:
        context_datasets = {item.runtime_path for source in context.sources if source.kind == "dataset"
                            for item in source.files}
        if context_datasets != inventory:
            return []
        inventory.update(context.upload_paths)
    # The controller may identify a file by a catalog-relative path. Resolve
    # only inside this manifest, rejecting ambiguous suffixes and unsafe syntax.
    targets = set()
    aliases = {item.logical_path: item.runtime_path for item in context.files} if context is not None else {}
    for target in message.controller_target_files or inventory:
        if (not isinstance(target, str) or not target or len(target) > 2048
                or "\\" in target or any(ord(char) < 32 or ord(char) == 127 for char in target)
                or any(part in {".", "..", ""} for part in target.lstrip("/").split("/"))):
            return []
        if target in inventory:
            targets.add(target)
            continue
        if PurePosixPath(target).is_absolute():
            return []
        matches = {path for path in inventory if path.endswith("/" + target)}
        if target in aliases:
            matches.add(aliases[target])
        if len(matches) != 1:
            return []
        targets.update(matches)
    # A selected upload can depend on its sidecars or another submitted input.
    # Freeze the complete upload scope instead of only a selected primary file.
    # Exceeding the bounded snapshot capacity disables continuation, not work.
    if context is not None:
        targets.update(context.upload_paths)
    return sorted(targets)


async def fingerprints(sandbox, paths: list[str], *, approved_upload_paths=None) -> list[dict] | None:
    reader = getattr(sandbox, "analysis_fingerprints", None)
    uploads = approved_upload_paths or []
    if (not callable(reader) or not paths or len(paths) > MAX_FILES
            or not isinstance(uploads, list) or len(uploads) > MAX_FILES
            or any(not _safe_upload_path(path) for path in uploads)
            or any(not _safe_path(path, approved_upload_paths=uploads) for path in paths)
            or len(set(paths)) != len(paths)):
        return None
    try:
        result = await reader(paths, approved_upload_paths=uploads) if uploads else await reader(paths)
        data = result.data if result.success is True and isinstance(result.data, dict) else {}
        if type(data.get("version")) is not int or data["version"] != 1 or data.get("errors") != []:
            return None
        return _validated_fingerprints(data.get("files"), expected_paths=paths, approved_upload_paths=uploads)
    except Exception:
        return None


async def prepare_continuation(repository, sandbox, session_id, user_id, message):
    """Verify checkpoint identity, sources and progress before any tool executes."""
    if not message.resume_from:
        return None
    checkpoint = await repository.get_analysis_checkpoint(session_id, message.resume_from)
    session = await repository.find_by_id_and_user_id(session_id, user_id)
    try:
        current_configuration = configuration_digest(session) if session else None
    except Exception:
        raise ValueError("续作执行配置无法确认；系统未重新执行。") from None
    expiry = checkpoint.get("expires_at") if isinstance(checkpoint, dict) else None
    if isinstance(expiry, datetime) and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if (not isinstance(checkpoint, dict) or not checkpoint or not session or session.user_id != user_id or checkpoint.get("owner_id") != user_id
            or ("session_id" in checkpoint and checkpoint["session_id"] != session_id)
            or checkpoint.get("id") != message.resume_from or not re.fullmatch(r"[0-9a-f]{32}", message.resume_from)
            or type(checkpoint.get("version")) is not int or checkpoint["version"] != 1
            or not isinstance(expiry, datetime) or expiry <= datetime.now(UTC)
            or not getattr(message, "client_message_id", None)
            or checkpoint.get("claimed_by") != message.client_message_id
            or checkpoint.get("configuration_digest") != current_configuration
            or checkpoint.get("sandbox_id") != sandbox.id or session.sandbox_id != sandbox.id):
        raise ValueError("续作进度不可用或执行配置已变化，请检查原任务。")
    try:
        source_seq = checkpoint.get("source_seq")
        resume_event_seq = getattr(message, "_accepted_event_seq", None)
        if (type(source_seq) is not int or source_seq < 1 or type(resume_event_seq) is not int
                or resume_event_seq <= source_seq):
            raise ValueError("checkpoint_event_boundary_missing")
        if checkpoint.get("checkpoint_digest") != _checkpoint_digest(checkpoint):
            raise ValueError("checkpoint_changed")
        if "budget_lineage_id" in checkpoint:
            if (not re.fullmatch(r"[0-9a-f]{32}", str(checkpoint["budget_lineage_id"]))
                    or type(checkpoint.get("budget_origin_seq")) is not int
                    or not 1 <= checkpoint["budget_origin_seq"] <= source_seq):
                raise ValueError("checkpoint_budget_identity_invalid")
            for key, limit in (("budget_read_evidence", 64), ("budget_artifact_evidence", 128)):
                values = checkpoint.get(key)
                if (not isinstance(values, list) or len(values) > limit
                        or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in values)):
                    raise ValueError("checkpoint_budget_evidence_invalid")
        plan = Plan.model_validate(checkpoint["plan"])
        if "answer_evidence" in checkpoint:
            # Frozen observations are meaningful only inside the exact original
            # execution. The outer HMAC covers this payload and the complete
            # plan/source/output snapshot; all byte checks still run below.
            if checkpoint.get("session_id") != session_id or checkpoint.get("reason_code") != "delivery_failed":
                raise ValueError("checkpoint_answer_evidence_scope_changed")
            from app.domain.services.analysis_answer_review import AnswerEvidence
            AnswerEvidence.from_checkpoint_snapshot(checkpoint["answer_evidence"],
                                                     step_ids={step.id for step in plan.steps})
        sources = source_paths(message)
        progress = checkpoint["progress"]
        verified_paths = progress["verified_files"]
        if (not sources or sorted(sources) != sorted(checkpoint["source_paths"])
                or any(not _safe_path(path, output_only=True) for path in verified_paths)
                or len(set(verified_paths)) != len(verified_paths)
                or not any(not step.success for step in plan.steps)
                or len({step.id for step in plan.steps}) != len(plan.steps)
                or progress["completed_step_ids"] != [step.id for step in plan.steps if step.success]
                or [item["id"] for item in progress["unfinished_steps"]] != [step.id for step in plan.steps if not step.success]
                or not _upload_scope_valid(message)
                or checkpoint.get("attachment_file_ids") != list(message.attachment_file_ids)
                or checkpoint.get("analysis_input_manifest") != _input_manifest(message)
                or checkpoint.get("skills") != list(message.skills)
                or checkpoint.get("mcp_servers") != list(message.mcp_servers)
                or checkpoint.get("mcp_access_all") is not message.mcp_access_all
                or checkpoint.get("dataset_ids") != [item.dataset_id for item in message.datasets]
                or checkpoint.get("target_files") != list(message.controller_target_files)
                or (checkpoint.get("controller_requires_artifacts") is not None
                    and type(checkpoint["controller_requires_artifacts"]) is not bool)
                or not isinstance(checkpoint.get("goal"), str) or not checkpoint["goal"].strip()):
            raise ValueError("checkpoint_scope_changed")
        expected = _validated_fingerprints(checkpoint.get("fingerprints"), expected_paths=sources + verified_paths,
                                           approved_upload_paths=approved_upload_paths(message))
        if not expected:
            raise ValueError("checkpoint_snapshot_incomplete")
        expected_map = {item["path"]: item for item in expected}
        for item in checkpoint.get("delivered", []):
            fingerprint = expected_map.get(item.get("file_path"))
            if (not fingerprint or item["file_path"] not in verified_paths or not item.get("file_id")
                    or item.get("size") != fingerprint["size"]
                    or (item.get("metadata") or {}).get("artifact_sha256") != fingerprint["sha256"]):
                raise ValueError("checkpoint_delivery_changed")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("续作进度不完整或执行范围已变化；系统未重新执行。") from None
    async def require_current():
        check = getattr(repository, "is_analysis_checkpoint_current", None)
        try:
            if not callable(check) or await check(
                session_id, message.resume_from, user_id, message.client_message_id,
                source_seq=source_seq, resume_event_seq=resume_event_seq,
            ) is not True:
                raise ValueError("checkpoint_superseded")
        except Exception:
            raise ValueError("原任务进度已失效或已有新输入；系统未重新执行。") from None
    await require_current()
    actual = await fingerprints(sandbox, [item["path"] for item in expected],
                                approved_upload_paths=approved_upload_paths(message))
    if not actual or actual != expected:
        raise ValueError("源数据或已保存成果已变化，不能安全续作；系统未重新执行。")
    await require_current()
    message.message = checkpoint["goal"]
    message.controller_requires_artifacts = checkpoint.get("controller_requires_artifacts")
    message._resume_checkpoint = checkpoint
    return checkpoint


async def save_checkpoint(repository, sandbox, session_id, user_id, message, plan,
                          *, source_fingerprints, records, delivered, reason_code, source_seq,
                          answer_evidence=None):
    saver = getattr(repository, "save_analysis_checkpoint", None)
    if (not callable(saver) or not source_fingerprints or not plan
            or not _upload_scope_valid(message)
            or not message.message.strip() or type(source_seq) is not int or source_seq < 1
            or not any(not step.success for step in plan.steps) or len({step.id for step in plan.steps}) != len(plan.steps)
            or not isinstance(records, list) or any(not isinstance(item, dict) for item in records)):
        return None
    session = await repository.find_by_id_and_user_id(session_id, user_id)
    if not session or session.user_id != user_id or session.sandbox_id != sandbox.id:
        return None
    sources = source_paths(message)
    source_fingerprints = _validated_fingerprints(source_fingerprints, expected_paths=sources,
                                                 approved_upload_paths=approved_upload_paths(message))
    if not sources or not source_fingerprints:
        return None
    context = getattr(message, "analysis_inputs", None)
    upload_sizes = {item.runtime_path: item.size for source in context.sources if source.kind == "upload"
                    for item in source.files} if context is not None else {}
    if any(item["path"] in upload_sizes and item["size"] != upload_sizes[item["path"]]
           for item in source_fingerprints):
        return None
    progress_records = [item for item in records if item.get("valid") is True]
    progress_paths = [item["path"] for item in progress_records]
    if (any(not _safe_path(path, output_only=True) for path in progress_paths)
            or len(set(progress_paths)) != len(progress_paths)):
        return None
    # Sources are rechecked against the pre-execution snapshot. Even a local
    # read-only mount may have been changed by its host owner during a task.
    current = await fingerprints(sandbox, list(dict.fromkeys(
        [item["path"] for item in source_fingerprints] + progress_paths)),
        approved_upload_paths=approved_upload_paths(message))
    if not current:
        return None
    current_map = {item["path"]: item for item in current}
    if any(current_map.get(item["path"]) != item for item in source_fingerprints):
        return None
    for item in progress_records:
        current_item = current_map[item["path"]]
        if item.get("sha256") != current_item["sha256"] or item.get("size") != current_item["size"]:
            return None
    for item in delivered:
        current_item = current_map.get(item.file_path)
        if (item.file_path not in progress_paths or not item.file_id or not current_item
                or item.size != current_item["size"]
                or (item.metadata or {}).get("artifact_sha256") != current_item["sha256"]):
            return None
    checkpoint = {
        "id": uuid.uuid4().hex, "version": 1, "owner_id": user_id, "session_id": session_id,
        "sandbox_id": sandbox.id, "configuration_digest": configuration_digest(session),
        "expires_at": datetime.now(UTC) + timedelta(hours=24), "claimed_by": None,
        "goal": message.message, "source_seq": source_seq,
        "controller_requires_artifacts": message.controller_requires_artifacts,
        "dataset_ids": [item.dataset_id for item in message.datasets],
        "target_files": list(message.controller_target_files),
        "source_paths": [item["path"] for item in source_fingerprints],
        "skills": list(message.skills), "mcp_servers": list(message.mcp_servers),
        "mcp_access_all": message.mcp_access_all,
        "attachment_file_ids": list(message.attachment_file_ids),
        "analysis_input_manifest": _input_manifest(message),
        "plan": plan.model_dump(mode="json"), "fingerprints": current,
        "reason_code": reason_code,
        "delivered": [item.model_dump(mode="json") for item in delivered],
        "progress": {
            "verified_files": progress_paths,
            "completed_step_ids": [step.id for step in plan.steps if step.success],
            "unfinished_steps": [{"id": step.id, "evidence": (step.result or "")[:4000],
                                   "missing": [item.model_dump() for item in step.outcome.missing] if step.outcome else []}
                                  for step in plan.steps if not step.success],
        },
    }
    if message._budget_lineage_id:
        from app.domain.services.analysis_recovery import current_analysis_recovery
        recovery = current_analysis_recovery()
        checkpoint.update(budget_lineage_id=message._budget_lineage_id,
                          budget_origin_seq=message._budget_origin_seq,
                          budget_read_evidence=sorted(recovery.progress.evidence) if recovery else [],
                          budget_artifact_evidence=sorted(recovery.artifact_evidence) if recovery else [])
    if reason_code == "delivery_failed" and answer_evidence is not None:
        from app.domain.services.analysis_answer_review import AnswerEvidence
        if isinstance(answer_evidence, AnswerEvidence):
            try:
                checkpoint["answer_evidence"] = answer_evidence.checkpoint_snapshot(
                    step_ids={step.id for step in plan.steps})
            except ValueError:
                # A missing/incomplete snapshot cannot become execution proof.
                # Keep upload recovery available; its review remains honest
                # about any analytical evidence that cannot be restored.
                pass
    if len(json.dumps(checkpoint, default=str).encode()) > 256 * 1024:
        checkpoint.pop("answer_evidence", None)
    checkpoint["checkpoint_digest"] = _checkpoint_digest(checkpoint)
    if len(json.dumps(checkpoint, default=str).encode()) > 256 * 1024:
        return None
    await saver(session_id, checkpoint)
    return checkpoint["id"]
