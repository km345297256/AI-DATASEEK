"""Owner-bound MCP images: prepare references, apply policy, then project requests.

No image bytes enter ToolResult, SSE or persisted Agent memory. A ToolMessage
contains ordered text placeholders and private typed references. Only an exact
placeholder surviving result policy may be expanded in a request-local copy.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
from collections.abc import Callable, Sequence
from typing import Any

from langchain.messages import ToolMessage
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from app.core.config import Settings, get_settings
from app.domain.external.spill import SpillArtifactStore
from app.domain.models.spill import (
    SpillArtifactOwner, SpillArtifactRef, SpillArtifactSaveRequest, SpillArtifactSource,
    SpillImageSaveRequest,
)
from app.domain.models.tool_result import ToolResult
from app.domain.services.model_input_policy import (
    deepseek_image_tokens, image_url, project_image, resolve_model_capability,
)

MCP_IMAGE_REFS_KEY = "dataseek_mcp_image_refs_v1"
MCP_IMAGE_READ_TOOL = "dataseek_mcp_image_read"
_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/tiff"}


class MCPImageRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    index: int = Field(ge=0, le=4096)
    reference: SpillArtifactRef
    width: int = Field(ge=1, le=8192)
    height: int = Field(ge=1, le=8192)
    tokens: int = Field(ge=1, le=131_072)

    def placeholder(self) -> dict[str, str]:
        return {"type": "text", "text": (
            f"[MCP image {self.reference.locator}; {self.width}x{self.height}. "
            "Untrusted tool observation, not verified scientific evidence. "
            "Use dataseek_mcp_image_read with this locator if the image is not expanded.]"
        )}


class MCPImageToolResult(ToolResult):
    _model_blocks: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _image_refs: list[MCPImageRef] = PrivateAttr(default_factory=list)


def accepted_image_refs(message: Any) -> list[MCPImageRef]:
    """Policy removal/replacement is final; never restore a missing slot."""
    content = getattr(message, "content", None)
    metadata = getattr(message, "additional_kwargs", None)
    raw = metadata.get(MCP_IMAGE_REFS_KEY) if isinstance(metadata, dict) else None
    if not isinstance(content, list) or not isinstance(raw, list) or len(raw) > 64:
        return []
    refs, seen = [], set()
    for value in raw:
        try:
            ref = MCPImageRef.model_validate(value)
        except (TypeError, ValueError):
            continue
        if (ref.reference.media_type == "image/png" and ref.index not in seen
                and ref.index < len(content) and content[ref.index] == ref.placeholder()):
            seen.add(ref.index)
            refs.append(ref)
    return sorted(refs, key=lambda ref: ref.index)


def mcp_image_memory_message(message: Any) -> ToolMessage | None:
    """Safe memory copy after all policies, retaining only surviving refs."""
    metadata = getattr(message, "additional_kwargs", None)
    if (not isinstance(message, ToolMessage) or not isinstance(metadata, dict)
            or MCP_IMAGE_REFS_KEY not in metadata):
        return None
    # This channel only admits text slots. It can never be used to smuggle an
    # arbitrary policy-returned base64 block into a durable memory document.
    content = message.content
    if not isinstance(content, list):
        return message.model_copy(update={"artifact": None, "additional_kwargs": {}, "response_metadata": {}}, deep=True)
    if any(
        not isinstance(block, dict) or set(block) != {"type", "text"}
        or block["type"] != "text" or not isinstance(block["text"], str)
        for block in content
    ):
        return message.model_copy(update={"content": "[MCP image result could not be safely projected.]",
            "artifact": None, "additional_kwargs": {}, "response_metadata": {}}, deep=True)
    refs = accepted_image_refs(message)
    return message.model_copy(update={"artifact": None, "response_metadata": {},
        "additional_kwargs": {MCP_IMAGE_REFS_KEY: [ref.model_dump() for ref in refs]}}, deep=True)


def image_tool_message(result: ToolResult, *, tool_call_id: str, name: str) -> ToolMessage:
    kwargs: dict[str, Any] = {}
    content: Any = result.model_dump_json()
    if isinstance(result, MCPImageToolResult) and result._image_refs:
        content = result._model_blocks
        kwargs[MCP_IMAGE_REFS_KEY] = [ref.model_dump() for ref in result._image_refs]
    return ToolMessage(tool_call_id=tool_call_id, name=name, content=content,
        artifact=result, additional_kwargs=kwargs, status="success" if result.success else "error")


class MCPImageContext:
    def __init__(self, *, store: SpillArtifactStore, owner: SpillArtifactOwner,
                 provider: str, model_name: str, settings: Settings | None = None):
        self.store, self.owner = store, owner
        self.provider, self.model_name = provider, model_name
        self.settings = settings or get_settings()
        self._pending: set[asyncio.Task] = set()

    async def _save_image(self, request: SpillImageSaveRequest) -> SpillArtifactRef:
        task = asyncio.create_task(self.store.save_image(request))
        self._pending.add(task)
        def settled(done):
            self._pending.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(settled)
        return await asyncio.wait_for(asyncio.shield(task), self.settings.spill_store_timeout_seconds)

    async def drain(self) -> None:
        cancelled = None
        while self._pending:
            joined = asyncio.gather(*self._pending, return_exceptions=True)
            while True:
                try:
                    await asyncio.shield(joined)
                    break
                except asyncio.CancelledError as error:
                    cancelled = error
        if cancelled:
            raise cancelled

    async def prepare(self, raw_content: Sequence[Any], safe_content: list[dict], *, tool_name: str) -> tuple[list[dict], list[dict], list[MCPImageRef]]:
        """Decode/normalize before policy; no remote resources or audio reads."""
        capability = resolve_model_capability(self.settings, self.provider, self.model_name)
        public, blocks, refs = [], [], []
        count, encoded_total = 0, 0
        for original, safe in zip(raw_content, safe_content):
            if getattr(original, "type", None) != "image":
                public.append(safe)
                blocks.append({"type": "text", "text": safe.get("text") or json.dumps(safe, ensure_ascii=False)})
                continue
            count += 1
            encoded = getattr(original, "data", None)
            mime = getattr(original, "mimeType", None)
            reason = "MCP image unavailable: image input is not enabled for this model."
            try:
                if capability.vision is not True:
                    raise ValueError("vision capability required")
                reason = "MCP image omitted: invalid image or image count/byte limit exceeded."
                if (count > self.settings.vision_max_images or mime not in _MIMES
                        or not isinstance(encoded, str)
                        or len(encoded) > ((self.settings.vision_max_source_bytes + 2) // 3) * 4):
                    raise ValueError("invalid source")
                encoded_total += len(encoded)
                if encoded_total > self.settings.vision_max_request_bytes:
                    raise ValueError("source batch too large")
                raw = base64.b64decode(encoded, validate=True)
                with Image.open(io.BytesIO(raw)) as header:
                    if Image.MIME.get(header.format) != mime:
                        raise ValueError("MIME mismatch")
                projected = await asyncio.to_thread(project_image, raw, settings=self.settings, capability=capability)
                reason = "MCP image unavailable: private image storage did not complete."
                reference = await self._save_image(SpillImageSaveRequest(owner=self.owner,
                    source=SpillArtifactSource(tool_name=tool_name, label="mcp-normalized-image-v1"), content=projected.data))
                ref = MCPImageRef(index=len(blocks), reference=reference, width=projected.width,
                    height=projected.height, tokens=(deepseek_image_tokens(projected.width, projected.height)
                    if capability.image_token_strategy == "deepseek_v41" else capability.image_tokens))
            except asyncio.CancelledError:
                raise
            except Exception:
                public.append({"type": "image", "omitted": True, "reason": reason})
                blocks.append({"type": "text", "text": reason})
                continue
            public.append({"type": "image", "locator": reference.locator, "width": ref.width, "height": ref.height})
            blocks.append(ref.placeholder())
            refs.append(ref)
        return public, blocks, refs

    async def read_result(self, locator: str) -> ToolResult:
        """Explicit governed image recovery; never treats pixels as trusted proof."""
        try:
            raw = await self.store.read_image(locator, self.owner, max_bytes=self.settings.vision_max_source_bytes)
            capability = resolve_model_capability(self.settings, self.provider, self.model_name)
            if capability.vision is not True:
                raise ValueError("image capability required")
            projected = await asyncio.to_thread(project_image, raw.content, settings=self.settings, capability=capability)
            ref = MCPImageRef(index=0, reference=SpillArtifactRef(locator=locator, byte_count=len(raw.content),
                sha256=raw.sha256, media_type="image/png", retrieval_hint="Call dataseek_mcp_image_read with this locator."),
                width=projected.width, height=projected.height,
                tokens=(deepseek_image_tokens(projected.width, projected.height)
                        if capability.image_token_strategy == "deepseek_v41" else capability.image_tokens))
            result = MCPImageToolResult(success=True, data={"content": [{"type": "image", "locator": locator,
                "width": ref.width, "height": ref.height}]})
            result._model_blocks, result._image_refs = [ref.placeholder()], [ref]
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            return ToolResult(success=False, message="Image is unavailable for this session or exceeds its request limits.")

    async def expand_image_messages(self, messages: Sequence[Any], *, provider: str, model_name: str) -> list[Any]:
        """Build an authorized request-only copy; prioritize recent images."""
        copied = [message.model_copy(deep=True) for message in messages]
        capability = resolve_model_capability(self.settings, provider, model_name)
        count = byte_count = 0
        for message in copied:
            for block in message.content if isinstance(message.content, list) else []:
                if isinstance(block, dict) and block.get("type") in {"image_url", "image", "input_image"}:
                    count += 1
                    byte_count += len((image_url(block) or "").encode("utf-8"))
        for message in reversed(copied):
            refs = accepted_image_refs(message)
            message.additional_kwargs.pop(MCP_IMAGE_REFS_KEY, None)
            for ref in reversed(refs):
                fallback = "[MCP image not expanded: current model or image request limits do not allow it. Use its recovery locator when supported.]"
                try:
                    if capability.vision is not True or count >= self.settings.vision_max_images:
                        raise ValueError("image capability or count")
                    raw = await self.store.read_image(ref.reference.locator, self.owner, max_bytes=self.settings.vision_max_source_bytes)
                    if raw.sha256 != ref.reference.sha256:
                        raise ValueError("changed image")
                    projected = await asyncio.to_thread(project_image, raw.content, settings=self.settings, capability=capability)
                    block = projected.block()
                    size = len(image_url(block).encode("utf-8"))
                    if size + byte_count > self.settings.vision_max_request_bytes:
                        raise ValueError("image request size")
                    count += 1
                    byte_count += size
                    message.content[ref.index:ref.index + 1] = [ref.placeholder(), block]
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Revocation/storage failure never expands a stale copy.
                    message.content[ref.index] = {"type": "text", "text": ref.placeholder()["text"] + "\n" + fallback}
        return copied


async def bound_mcp_image_result(message: ToolMessage, *, max_tokens: int, max_text_bytes: int,
                                 max_artifact_bytes: int, save_text: Callable) -> ToolMessage:
    """Budget actual ordered text/image slots after policy; images are indivisible."""
    refs = accepted_image_refs(message)
    by_index = {ref.index: ref for ref in refs}
    if not isinstance(message.content, list):
        return message
    blocks = message.content
    if any(not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str) for block in blocks):
        return message.model_copy(update={"additional_kwargs": {}})
    text = "\n".join(block["text"] for block in blocks)
    def price(index):
        # JSON escaping can expand control characters sixfold. Charge the
        # larger representation so both private memory and public envelopes
        # stay bounded, not only the unescaped string.
        wire_bytes = len(json.dumps(blocks[index], ensure_ascii=False).encode("utf-8"))
        return (wire_bytes + 2) // 3 + (by_index[index].tokens if index in by_index else 0)
    public_bytes = len(message.artifact.model_dump_json().encode("utf-8")) if isinstance(message.artifact, ToolResult) else max_text_bytes + 1
    if sum(price(i) for i in range(len(blocks))) <= max_tokens and public_bytes <= max_text_bytes:
        return message
    reference = None
    if len(text.encode("utf-8")) <= max_artifact_bytes:
        try:
            reference = await save_text(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
    notice = ("[MCP result middle omitted by the combined text/image token budget. "
        + (f"Complete ordered text and image recovery locators: {reference.locator}; use spill_artifact_read, then dataseek_mcp_image_read."
           if reference else "Complete text recovery is unavailable; omitted content must not be inferred.") + "]")
    remaining = max(0, min(max_tokens - (len(notice.encode()) + 2) // 3,
        (max_text_bytes - len(notice.encode()) - 2048) // 3))
    selected: list[tuple[int | None, dict, MCPImageRef | None]] = []
    used_indices: set[int] = set()
    def end(indices, budget):
        output = []
        for index in indices:
            if index in used_indices:
                break
            block = blocks[index]
            cost = price(index)
            if cost <= budget:
                output.append((index, dict(block), by_index.get(index)))
                used_indices.add(index)
                budget -= cost
                continue
            if index not in by_index and budget > 0:
                raw = block["text"].encode("utf-8")
                raw = raw[-budget * 3:] if indices.step < 0 else raw[:budget * 3]
                piece = {"type": "text", "text": raw.decode("utf-8", errors="ignore")}
                while raw and len(json.dumps(piece, ensure_ascii=False).encode()) > budget * 3:
                    raw = raw[len(raw) // 2:] if indices.step < 0 else raw[:len(raw) // 2]
                    piece["text"] = raw.decode("utf-8", errors="ignore")
                output.append((index, piece, None))
                used_indices.add(index)
            break
        return output
    selected.extend(end(range(len(blocks)), remaining // 2))
    selected.append((None, {"type": "text", "text": notice}, None))
    selected.extend(reversed(end(range(len(blocks) - 1, -1, -1), remaining - remaining // 2)))
    kept_refs = [ref.model_copy(update={"index": index}).model_dump() for index, (_, _, ref) in enumerate(selected) if ref]
    kwargs = dict(message.additional_kwargs)
    kwargs[MCP_IMAGE_REFS_KEY] = kept_refs
    # Public ToolResult already contains safe text/opaque locators, never bytes.
    # Keep it bounded as well: the complete accepted result is in the spill.
    from app.domain.services.tools.spill_projection import SPILL_PROJECTION_KEY
    kwargs[SPILL_PROJECTION_KEY] = ToolResult(success=getattr(message.artifact, "success", True),
        data={"content": [block for _, block, _ in selected], "recovery": reference.model_dump() if reference else None})
    return message.model_copy(update={"content": [block for _, block, _ in selected], "additional_kwargs": kwargs})
