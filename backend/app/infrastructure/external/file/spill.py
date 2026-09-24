from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.external.file import FileStorage
from app.domain.external.spill import SpillArtifactStore
from app.domain.models.spill import (
    SPILL_LOCATOR_PREFIX,
    SpillArtifactChunk,
    SpillArtifactOwner,
    SpillArtifactRecord,
    SpillArtifactRef,
    SpillArtifactSaveRequest,
    SpillImageSaveRequest,
    SpillImageContent,
)
from app.domain.repositories.spill_artifact_repository import SpillArtifactRepository


logger = logging.getLogger(__name__)

_ARTIFACT_ID = re.compile(r"^[0-9a-f]{32}$")


def _opaque_reference(value: Any, namespace: str) -> str:
    digest = hashlib.sha256(
        str(value or "missing").encode("utf-8", errors="replace")
    ).hexdigest()[:12]
    return f"{namespace}:sha256:{digest}"


def _as_utc(value: datetime) -> datetime:
    """Normalize PyMongo's default naive UTC datetimes before comparison."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _artifact_id_from_locator(locator: str) -> str:
    if not isinstance(locator, str) or not locator.startswith(SPILL_LOCATOR_PREFIX):
        raise ValueError("Invalid spill artifact locator")
    artifact_id = locator.removeprefix(SPILL_LOCATOR_PREFIX)
    if not _ARTIFACT_ID.fullmatch(artifact_id):
        raise ValueError("Invalid spill artifact locator")
    return artifact_id


def _decode_utf8_page(raw: bytes, *, requested: int) -> tuple[str, int]:
    """Decode an exact byte range without splitting the trailing code point."""
    if not isinstance(raw, (bytes, bytearray)):
        raise RuntimeError("Spill storage returned non-byte content")
    raw = bytes(raw)
    if len(raw) != requested:
        raise RuntimeError("Spill artifact size does not match its durable record")

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        # The source boundary only accepts Python strings, so the sole valid
        # decoding error is a page ending in the middle of one code point.
        if error.reason != "unexpected end of data" or error.end != len(raw):
            raise RuntimeError("Spill artifact is not valid UTF-8") from error
        raw = raw[:error.start]
        content = raw.decode("utf-8")
    return content, len(raw)


async def _complete_despite_cancellation(awaitable: Any) -> tuple[Any, asyncio.CancelledError | None]:
    task = asyncio.ensure_future(awaitable)
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(task), cancellation
        except asyncio.CancelledError as error:
            if task.done() and task.cancelled():
                raise cancellation or error
            cancellation = error
            continue
        except Exception as error:
            if cancellation is not None:
                raise cancellation from error
            raise


class FileStorageSpillArtifactStore(SpillArtifactStore):
    """Private spill store layered over GridFS, MinIO, or hybrid storage."""

    def __init__(
        self,
        file_storage: FileStorage,
        repository: SpillArtifactRepository,
        *,
        identity_key: str,
        retention_hours: int = 168,
        read_chunk_bytes: int = 16 * 1024,
    ) -> None:
        if retention_hours <= 0:
            raise ValueError("spill retention hours must be positive")
        if read_chunk_bytes < 1024:
            raise ValueError("spill read chunk bytes must be at least 1024")
        if not isinstance(identity_key, str) or not identity_key:
            raise ValueError("spill artifact identity key must not be empty")
        self._file_storage = file_storage
        self._repository = repository
        self._identity_key = identity_key.encode("utf-8")
        self._retention = timedelta(hours=retention_hours)
        self._read_chunk_bytes = read_chunk_bytes

    async def save_text(self, request: SpillArtifactSaveRequest) -> SpillArtifactRef:
        content = request.content.encode("utf-8")
        if request.media_type.startswith("image/"):
            raise ValueError("Image spill artifacts require the binary image boundary")
        return await self._save_content(request, content)

    async def save_image(self, request: SpillImageSaveRequest) -> SpillArtifactRef:
        if not request.content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Image spill requires normalized PNG bytes")
        return await self._save_content(request, request.content)

    async def _save_content(self, request: SpillArtifactSaveRequest | SpillImageSaveRequest, content: bytes) -> SpillArtifactRef:
        digest = hashlib.sha256(content).hexdigest()
        artifact_id = self._artifact_id(request, digest)
        storage_user_id = self._storage_identity(request.owner, "user")
        now = datetime.now(UTC)
        existing = await self._repository.find_by_artifact_id(artifact_id)
        if existing is not None:
            if existing.status == "active" and _as_utc(existing.expires_at) > now:
                try:
                    file_info = await self._file_storage.get_file_info(
                        existing.storage_file_id,
                        self._storage_user(existing),
                    )
                    if file_info is None:
                        raise FileNotFoundError("Spill backing object was not found")
                    self._validate_storage_metadata(existing, file_info)
                    probe_length = min(1, existing.byte_count)
                    if probe_length:
                        probe, ranged_info = await self._file_storage.download_file_range(
                            existing.storage_file_id,
                            self._storage_user(existing),
                            offset=0,
                            length=probe_length,
                        )
                        self._validate_storage_metadata(existing, ranged_info)
                        if len(probe) != probe_length:
                            raise FileNotFoundError("Spill backing object was incomplete")
                    return self._reference(existing)
                except (FileNotFoundError, PermissionError, RuntimeError):
                    if not await self._delete_record(existing):
                        raise RuntimeError("Stale spill artifact could not be replaced")
                    existing = None
            if existing is not None and not await self._delete_record(existing):
                raise RuntimeError("Stale spill artifact could not be replaced")
        metadata = {
            "source": "tool_output_spill",
            "spill_schema_version": 1,
            "spill_artifact_id": artifact_id,
            # Existing FileStorage providers place these values in object keys
            # and ownership metadata. Use keyed internal principals so a
            # browser user cannot address the spill through generic file APIs.
            "session_id": storage_user_id,
            "owner_user_id": storage_user_id,
            "spill_sha256": digest,
            "spill_byte_count": len(content),
            # Descriptive provenance is hashed because provider-controlled
            # names and call ids may contain private values.
            "source_tool_ref": _opaque_reference(
                request.source.tool_name,
                "tool",
            ),
            "source_call_ref": _opaque_reference(
                request.source.tool_call_id,
                "call",
            ),
        }
        uploaded = await self._file_storage.upload_file(
            io.BytesIO(content),
            f"spill-{artifact_id}.{'png' if request.media_type == 'image/png' else 'txt'}",
            storage_user_id,
            content_type=request.media_type,
            metadata=metadata,
        )
        storage_file_id = uploaded.file_id
        if not isinstance(storage_file_id, str) or not storage_file_id:
            raise RuntimeError("Spill storage did not return a durable file id")
        record: SpillArtifactRecord | None = None
        try:
            record = SpillArtifactRecord(
                artifact_id=artifact_id,
                storage_file_id=storage_file_id,
                storage_user_id=storage_user_id,
                owner_user_id=request.owner.user_id,
                owner_session_id=request.owner.session_id,
                byte_count=len(content),
                sha256=digest,
                media_type=request.media_type,
                source_tool_ref=metadata["source_tool_ref"],
                source_call_ref=metadata["source_call_ref"],
                created_at=now,
                expires_at=now + self._retention,
            )
            if int(uploaded.size or 0) != len(content):
                raise RuntimeError("Spill storage did not persist the complete result")
        except Exception:
            removed = await self._best_effort_delete_storage(
                storage_file_id,
                storage_user_id,
            )
            if not removed and record is not None:
                await self._persist_cleanup_tombstone(record)
            raise

        assert record is not None

        try:
            _, cancelled = await _complete_despite_cancellation(
                self._repository.save(record)
            )
            if cancelled is not None:
                raise cancelled
        except asyncio.CancelledError:
            try:
                await _complete_despite_cancellation(self._rollback_cancelled_save(
                    record,
                    storage_file_id,
                    storage_user_id,
                ))
            except BaseException as error:
                logger.warning(
                    "Spill cancellation rollback failed artifact=%s error_type=%s",
                    _opaque_reference(artifact_id, "artifact"),
                    type(error).__name__,
                )
            raise
        except Exception:
            try:
                winner, lookup_confirmed = await self._lookup_for_reconciliation(
                    artifact_id
                )
            except asyncio.CancelledError:
                try:
                    await _complete_despite_cancellation(
                        self._rollback_cancelled_save(
                            record,
                            storage_file_id,
                            storage_user_id,
                        )
                    )
                except BaseException:
                    pass
                raise
            if not lookup_confirmed:
                # The insert may have committed before its transport failed.
                # Never delete the only copy while that outcome is unknown.
                await self._persist_cleanup_tombstone(
                    record,
                    reconcile_artifact_id=artifact_id,
                )
                raise
            winner_is_usable = (
                winner is not None
                and winner.status == "active"
                and winner.owner_user_id == request.owner.user_id
                and winner.owner_session_id == request.owner.session_id
                and winner.sha256 == digest
            )
            # A write can commit and then report a transport error. If the
            # durable winner points at this exact upload, it is already the
            # desired result and must not be rolled back.
            winner_points_to_upload = (
                winner is not None
                and hmac.compare_digest(
                    winner.storage_file_id,
                    storage_file_id,
                )
            )
            if winner_is_usable and winner_points_to_upload:
                return self._reference(winner)
            if winner_points_to_upload:
                # A durable mapping still owns the object, even if its other
                # fields are not usable for this caller. Leave lifecycle
                # cleanup to that mapping rather than destroying its bytes.
                raise

            removed = await self._best_effort_delete_storage(
                storage_file_id,
                storage_user_id,
            )
            if not removed:
                await self._persist_cleanup_tombstone(record)
            if winner_is_usable:
                return self._reference(winner)
            raise

        return self._reference(record)

    async def _lookup_for_reconciliation(
        self,
        artifact_id: str,
        *,
        attempts: int = 3,
    ) -> tuple[SpillArtifactRecord | None, bool]:
        """Retry an uncertain metadata lookup and report whether it succeeded."""
        last_error_type = "unknown"
        for attempt in range(max(1, attempts)):
            try:
                return await self._repository.find_by_artifact_id(artifact_id), True
            except Exception as error:
                last_error_type = type(error).__name__
            if attempt + 1 < max(1, attempts):
                await asyncio.sleep(0.05 * (2**attempt))
        logger.warning(
            "Spill metadata reconciliation unavailable artifact=%s error_type=%s",
            _opaque_reference(artifact_id, "artifact"),
            last_error_type,
        )
        return None, False

    async def _rollback_cancelled_save(
        self,
        record: SpillArtifactRecord,
        storage_file_id: str,
        storage_user_id: str,
    ) -> None:
        """Reconcile a repository write whose caller was cancelled."""
        winner, lookup_confirmed = await self._lookup_for_reconciliation(
            record.artifact_id
        )
        if not lookup_confirmed:
            await self._persist_cleanup_tombstone(
                record,
                reconcile_artifact_id=record.artifact_id,
            )
            return
        if winner is not None and hmac.compare_digest(
            winner.storage_file_id,
            storage_file_id,
        ):
            # The metadata commit completed before cancellation. A concurrent
            # idempotent caller may already have received this same locator;
            # retain the active artifact until normal owner/TTL cleanup.
            if winner.status != "active":
                await self._delete_record(winner)
            return
        removed = await self._best_effort_delete_storage(
            storage_file_id,
            storage_user_id,
        )
        if not removed:
            await self._persist_cleanup_tombstone(record)

    async def _persist_cleanup_tombstone(
        self,
        record: SpillArtifactRecord,
        *,
        reconcile_artifact_id: str | None = None,
    ) -> None:
        cleanup_id = hmac.new(
            self._identity_key,
            f"spill-cleanup-v1:{record.storage_file_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:32]
        tombstone = record.model_copy(update={
            "artifact_id": cleanup_id,
            "reconcile_artifact_id": reconcile_artifact_id,
            "status": "deleting",
            "cleanup_attempted_at": None,
        })
        try:
            existing = await self._repository.find_by_artifact_id(cleanup_id)
            if existing is None:
                await self._repository.save(tombstone)
        except Exception as error:
            logger.warning(
                "Spill cleanup tombstone persistence failed artifact=%s error_type=%s",
                _opaque_reference(cleanup_id, "artifact"),
                type(error).__name__,
            )

    @staticmethod
    def _reference(record: SpillArtifactRecord) -> SpillArtifactRef:
        return SpillArtifactRef(
            locator=record.locator,
            byte_count=record.byte_count,
            sha256=record.sha256,
            media_type=record.media_type,
            retrieval_hint=(
                "Call dataseek_mcp_image_read with this locator to inspect the image."
                if record.media_type == "image/png" else
                "Call spill_artifact_read with this locator and offset 0; "
                "continue from next_byte until eof is true."
            ),
        )

    def _artifact_id(self, request: SpillArtifactSaveRequest | SpillImageSaveRequest, digest: str) -> str:
        identity = json.dumps(
            {
                "schema_version": 1,
                "user_id": request.owner.user_id,
                "session_id": request.owner.session_id,
                "tool_name": request.source.tool_name,
                "tool_call_id": request.source.tool_call_id,
                "label": request.source.label,
                "content_sha256": digest,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._identity_key, identity, hashlib.sha256).hexdigest()[:32]

    def _storage_identity(
        self,
        owner: SpillArtifactOwner,
        namespace: str,
    ) -> str:
        identity = json.dumps(
            {
                "schema_version": 1,
                "namespace": namespace,
                "user_id": owner.user_id,
                "session_id": owner.session_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hmac.new(self._identity_key, identity, hashlib.sha256).hexdigest()
        return f"spill-{namespace}-{digest}"

    @staticmethod
    def _storage_user(record: SpillArtifactRecord) -> str:
        return record.storage_user_id or record.owner_user_id

    async def read_text(
        self,
        locator: str,
        owner: SpillArtifactOwner,
        *,
        offset: int = 0,
        max_bytes: int | None = None,
    ) -> SpillArtifactChunk:
        artifact_id = _artifact_id_from_locator(locator)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Spill offset must be a non-negative integer")
        record = await self._repository.find_by_artifact_id(artifact_id)
        if record is None:
            raise FileNotFoundError("Spill artifact was not found or has expired")
        if record.media_type == "image/png":
            raise ValueError("Use dataseek_mcp_image_read for image spill artifacts")
        if not (
            hmac.compare_digest(record.owner_user_id, owner.user_id)
            and hmac.compare_digest(record.owner_session_id, owner.session_id)
        ):
            raise PermissionError("Spill artifact belongs to a different session")
        if record.status != "active":
            raise FileNotFoundError("Spill artifact was not found or has expired")
        if _as_utc(record.expires_at) <= datetime.now(UTC):
            await self._delete_record(record)
            raise FileNotFoundError("Spill artifact was not found or has expired")
        if offset > record.byte_count:
            raise ValueError("Spill offset exceeds the artifact size")

        requested_limit = self._read_chunk_bytes if max_bytes is None else max_bytes
        if (
            isinstance(requested_limit, bool)
            or not isinstance(requested_limit, int)
            or requested_limit < 4
        ):
            raise ValueError("Spill max_bytes must be an integer of at least 4")
        limit = min(requested_limit, self._read_chunk_bytes)

        if offset == record.byte_count:
            file_info = await self._file_storage.get_file_info(
                record.storage_file_id,
                self._storage_user(record),
            )
            if file_info is None:
                raise FileNotFoundError("Spill artifact storage object was not found")
            content, consumed = "", 0
        else:
            requested = min(limit, record.byte_count - offset)
            raw, file_info = await self._file_storage.download_file_range(
                record.storage_file_id,
                self._storage_user(record),
                offset=offset,
                length=requested,
            )
            content, consumed = _decode_utf8_page(raw, requested=requested)
        self._validate_storage_metadata(record, file_info)

        # A session deletion may have revoked the locator while the object was
        # being read. Recheck the tombstone before returning any bytes.
        latest = await self._repository.find_by_artifact_id(artifact_id)
        if latest is None or latest.status != "active":
            raise FileNotFoundError("Spill artifact was not found or has expired")
        if not (
            hmac.compare_digest(latest.owner_user_id, owner.user_id)
            and hmac.compare_digest(latest.owner_session_id, owner.session_id)
            and hmac.compare_digest(latest.storage_file_id, record.storage_file_id)
            and hmac.compare_digest(latest.sha256, record.sha256)
        ):
            raise PermissionError("Spill artifact ownership changed during read")
        if _as_utc(latest.expires_at) <= datetime.now(UTC):
            await self._delete_record(latest)
            raise FileNotFoundError("Spill artifact was not found or has expired")

        next_byte = offset + consumed
        return SpillArtifactChunk(
            locator=record.locator,
            content=content,
            start_byte=offset,
            next_byte=next_byte,
            total_bytes=record.byte_count,
            eof=next_byte >= record.byte_count,
            sha256=record.sha256,
            media_type=record.media_type,
        )

    async def read_image(self, locator: str, owner: SpillArtifactOwner, *, max_bytes: int) -> SpillImageContent:
        """Binary sibling of read_text with the same revocation boundary."""
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("Image read limit must be a positive integer")
        artifact_id = _artifact_id_from_locator(locator)
        record = await self._repository.find_by_artifact_id(artifact_id)

        def authorized(value: SpillArtifactRecord | None) -> SpillArtifactRecord:
            if (value is None or value.status != "active"
                    or _as_utc(value.expires_at) <= datetime.now(UTC)):
                raise FileNotFoundError("Image artifact was not found or has expired")
            if not (hmac.compare_digest(value.owner_user_id, owner.user_id)
                    and hmac.compare_digest(value.owner_session_id, owner.session_id)):
                raise PermissionError("Image artifact belongs to a different session")
            if value.media_type != "image/png" or not 0 < value.byte_count <= max_bytes:
                raise ValueError("Image artifact is not a bounded normalized image")
            return value

        record = authorized(record)
        raw, info = await self._file_storage.download_file_range(
            record.storage_file_id, self._storage_user(record), offset=0, length=record.byte_count,
        )
        self._validate_storage_metadata(record, info)
        if (not isinstance(raw, bytes) or len(raw) != record.byte_count
                or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), record.sha256)):
            raise RuntimeError("Image artifact integrity check failed")
        latest = authorized(await self._repository.find_by_artifact_id(artifact_id))
        if not (hmac.compare_digest(latest.storage_file_id, record.storage_file_id)
                and hmac.compare_digest(latest.sha256, record.sha256)):
            raise PermissionError("Image artifact ownership changed during read")
        return SpillImageContent(content=raw, sha256=record.sha256)

    async def delete_owner(self, owner: SpillArtifactOwner) -> int:
        records = await self._repository.list_by_owner(
            owner.user_id,
            owner.session_id,
        )
        removed = 0
        for record in records:
            if await self._delete_record(record):
                removed += 1
        return removed

    async def reap_expired(self, *, limit: int = 100) -> int:
        records = await self._repository.list_expired(
            datetime.now(UTC),
            limit=max(1, limit),
        )
        removed = 0
        for record in records:
            if await self._delete_record(record):
                removed += 1
        return removed

    def _validate_storage_metadata(
        self,
        record: SpillArtifactRecord,
        file_info: Any,
    ) -> None:
        metadata = getattr(file_info, "metadata", None)
        if not isinstance(metadata, dict):
            raise PermissionError("Spill artifact storage metadata is unavailable")
        expected = {
            "source": "tool_output_spill",
            "spill_artifact_id": record.artifact_id,
            "session_id": (
                self._storage_user(record)
                if record.storage_user_id
                else record.owner_session_id
            ),
            "owner_user_id": self._storage_user(record),
            "spill_sha256": record.sha256,
        }
        if any(str(metadata.get(key)) != str(value) for key, value in expected.items()):
            raise PermissionError("Spill artifact storage metadata did not match")
        if int(getattr(file_info, "size", -1)) != record.byte_count:
            raise RuntimeError("Spill artifact size does not match its durable record")

    async def _delete_record(self, record: SpillArtifactRecord) -> bool:
        # Revoke reads first, but retain the private storage mapping until the
        # underlying object is gone. Failed cleanup therefore stays retryable.
        deleting = await self._repository.mark_deleting(
            record.artifact_id,
            expected_storage_file_id=record.storage_file_id,
        )
        if deleting is None:
            return True
        if deleting.reconcile_artifact_id is not None:
            try:
                mapped = await self._repository.find_by_artifact_id(
                    deleting.reconcile_artifact_id
                )
            except Exception as error:
                logger.warning(
                    "Spill cleanup reconciliation failed artifact=%s error_type=%s",
                    _opaque_reference(deleting.artifact_id, "artifact"),
                    type(error).__name__,
                )
                return False
            if mapped is not None and hmac.compare_digest(
                mapped.storage_file_id,
                deleting.storage_file_id,
            ):
                # The uncertain write did commit. Drop only the auxiliary
                # cleanup tombstone; the original mapping owns the object.
                await self._repository.delete(
                    deleting.artifact_id,
                    expected_storage_file_id=deleting.storage_file_id,
                )
                return True
        try:
            deleted = await self._file_storage.delete_file(
                deleting.storage_file_id,
                self._storage_user(deleting),
            )
        except Exception as error:
            logger.warning(
                "Spill storage cleanup failed artifact=%s error_type=%s",
                _opaque_reference(deleting.artifact_id, "artifact"),
                type(error).__name__,
            )
            return False
        if not deleted:
            # Distinguish an already-absent object from an authorization or
            # provider failure without exposing the private id. A present
            # object keeps its tombstone for the next reaper pass.
            try:
                remaining = await self._file_storage.get_file_info(
                    deleting.storage_file_id,
                    None,
                )
            except Exception as error:
                logger.warning(
                    "Spill storage verification failed artifact=%s error_type=%s",
                    _opaque_reference(deleting.artifact_id, "artifact"),
                    type(error).__name__,
                )
                return False
            if remaining is not None:
                logger.warning(
                    "Spill storage cleanup left a live object artifact=%s",
                    _opaque_reference(deleting.artifact_id, "artifact"),
                )
                return False
        await self._repository.delete(
            deleting.artifact_id,
            expected_storage_file_id=deleting.storage_file_id,
        )
        return True

    async def _best_effort_delete_storage(self, file_id: str, user_id: str) -> bool:
        last_error_type = "object_still_present"
        for attempt in range(3):
            try:
                if await self._file_storage.delete_file(file_id, user_id):
                    return True
                remaining = await self._file_storage.get_file_info(file_id, None)
                if remaining is None:
                    return True
            except Exception as error:
                last_error_type = type(error).__name__
            if attempt < 2:
                await asyncio.sleep(0.05 * (2**attempt))
        logger.warning(
            "Spill rollback cleanup failed object=%s error_type=%s",
            _opaque_reference(file_id, "object"),
            last_error_type,
        )
        return False
