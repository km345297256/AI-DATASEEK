"""Model-side image projection and explicit, credential-free request sizing.

Original files and UI previews are never modified. DeepSeek's v41 strategy is
opt-in for a verified model, including models served by compatible gateways.
Its grid rules follow deepseek-harness@0d1f50007f, image-tokens.ts; provider
reported usage remains authoritative.
"""
from __future__ import annotations

import base64
import io
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import ModelRequestCapability, Settings


class ImageInputError(ValueError):
    """Safe public failure; never include a file ID, URL, or host path."""


class ImageInputSizeError(ImageInputError):
    """A historical image may remain referenced when it cannot fit safely."""


def resolve_model_capability(settings: Settings, provider: str, model_name: str) -> ModelRequestCapability:
    return settings.model_request_capabilities.get(provider, {}).get(model_name, ModelRequestCapability())


def _cells(pixels: int) -> int:
    return (pixels // 14 + 2) // 3


def _grid_tokens(width: int, height: int) -> int:
    return _cells(height) * (_cells(width) + 1) + 2


def _solve_grid(width: int, height: int) -> tuple[int, int]:
    aspect = height / width
    ideal_width = math.sqrt(1022 / aspect + 0.25) - 0.5
    ideal_height = ideal_width * aspect
    if ideal_width < 1:
        return 42, 511 * 42
    if ideal_height < 1:
        return 1021 * 42, 42
    scale = min(int(ideal_width) * 42 / width, int(ideal_height) * 42 / height)
    return int(width * scale / 14) * 14, int(height * scale / 14) * 14


def _padded(width: int, height: int) -> tuple[int, int]:
    return (width + 13) // 14 * 14, (height + 13) // 14 * 14


def deepseek_image_tokens(width: int, height: int) -> int:
    if width <= 0 or height <= 0:
        raise ImageInputError("Invalid image dimensions.")
    previous = None
    for _ in range(10):
        pixels = width * height
        if pixels < 544 * 544:
            scale = math.sqrt(544 * 544 / pixels)
            width, height = int(width * scale), int(height * scale)
        padded_width, padded_height = _padded(width, height)
        if _grid_tokens(padded_width, padded_height) > 1024:
            padded_width, padded_height = _solve_grid(width, height)
        current = (padded_width, padded_height)
        tokens = _grid_tokens(*current)
        if tokens > 1024:
            raise ImageInputError("Image geometry exceeds the model token grid.")
        if current == previous:
            return tokens
        previous = current
        width, height = current
    raise ImageInputError("Image token projection did not converge.")


def _long_edge(width: int, height: int, limit: int) -> tuple[int, int]:
    scale = min(1.0, limit / max(width, height))
    return max(1, int(width * scale + 0.5)), max(1, int(height * scale + 0.5))


def deepseek_request_dimensions(width: int, height: int) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        raise ImageInputError("Invalid image dimensions.")
    if _grid_tokens(*_padded(width, height)) <= 1024:
        return width, height
    solved_width, solved_height = _solve_grid(width, height)
    return _long_edge(width, height, solved_width if width >= height else solved_height)


@dataclass(frozen=True)
class ProjectedImage:
    data: bytes
    mime_type: str
    width: int
    height: int

    def block(self) -> dict[str, Any]:
        encoded = base64.b64encode(self.data).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{self.mime_type};base64,{encoded}"}}


def project_image(raw: bytes, *, settings: Settings, capability: ModelRequestCapability) -> ProjectedImage:
    if not raw:
        raise ImageInputError("Image attachment is empty or exceeds the source size limit.")
    if len(raw) > settings.vision_max_source_bytes:
        raise ImageInputSizeError("Image attachment exceeds the source size limit.")
    if capability.vision is False:
        raise ImageInputError("The selected model does not support image input.")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > settings.vision_max_source_pixels:
                raise ImageInputSizeError("Image attachment exceeds the decoded pixel limit.")
            # Do not silently discard animation frames or scientific TIFF pages.
            if getattr(source, "n_frames", 1) > 1:
                raise ImageInputError("Multi-frame images require selecting a frame or page before visual analysis.")
            source.load()
            image = ImageOps.exif_transpose(source)
            dimensions = _long_edge(image.width, image.height, settings.vision_max_image_edge)
            if capability.image_token_strategy == "deepseek_v41":
                dimensions = deepseek_request_dimensions(*dimensions)
            if dimensions != image.size:
                image = image.resize(dimensions, Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "transparency" in image.info else "RGB")
            image.info.clear()
            # Lossless model copy preserves chart text/lines and strips private
            # EXIF metadata. It is bounded independently of the source file.
            output = io.BytesIO()
            image.save(output, format="PNG")
            if output.tell() * 4 // 3 + 64 > settings.vision_max_request_bytes:
                raise ImageInputSizeError("Projected image exceeds the model request size limit.")
            return ProjectedImage(output.getvalue(), "image/png", image.width, image.height)
    except ImageInputError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, OverflowError):
        raise ImageInputError("Image attachment is invalid or cannot be safely decoded.") from None


def image_url(block: Mapping[str, Any]) -> str | None:
    value = block.get("image_url", block.get("url"))
    value = value.get("url") if isinstance(value, Mapping) else value
    return value if isinstance(value, str) else None


def request_image_estimator(messages, *, settings: Settings, capability: ModelRequestCapability):
    """Validate a request once; cache image prices across compaction passes.

    Never fetch remote URLs. Native provider blocks without local dimensions
    retain an operator-configured reservation rather than a guessed price.
    """
    prices: dict[str, int] = {}
    count = 0
    total_bytes = 0
    for message in messages:
        content = getattr(message, "content", None)
        blocks = content if isinstance(content, (list, tuple)) else (content,)
        for block in blocks:
            if not isinstance(block, Mapping) or block.get("type") not in {"image", "image_url", "input_image"}:
                continue
            count += 1
            if capability.vision is False:
                raise ImageInputError("The selected model does not support image input.")
            url = image_url(block)
            # Other adapters may use Anthropic-style base64 source blocks.
            source = block.get("source")
            data = source.get("data") if isinstance(source, Mapping) else block.get("data", block.get("base64", block.get("image")))
            total_bytes += (len(url.encode("utf-8")) if url else len(data) if isinstance(data, str)
                            else (len(data) + 2) // 3 * 4 if isinstance(data, (bytes, bytearray, memoryview)) else 0)
            if count > settings.vision_max_images or total_bytes > settings.vision_max_request_bytes:
                raise ImageInputError("Image inputs exceed the model request count or size limit.")
            if not url or url in prices or capability.image_token_strategy != "deepseek_v41" or not url.startswith("data:image/"):
                continue
            try:
                header, encoded = url.split(",", 1)
                if not header.endswith(";base64"):
                    continue
                raw = base64.b64decode(encoded, validate=True)
                with Image.open(io.BytesIO(raw)) as image:
                    if image.width * image.height > settings.vision_max_source_pixels:
                        raise ImageInputError("Image attachment exceeds the decoded pixel limit.")
                    prices[url] = deepseek_image_tokens(image.width, image.height)
            except (ValueError, OSError, Image.DecompressionBombError):
                raise ImageInputError("Image attachment is invalid or cannot be safely decoded.") from None

    def estimate(block):
        return prices.get(image_url(block), capability.image_tokens)

    return estimate
