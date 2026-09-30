"""Read-only SVG preview: validated local XML to a bounded, inert PNG."""
from __future__ import annotations

import base64

from app.services.svg_render import render_svg_png
from app.services.svg_validation import static_svg_document


MAX_SVG_INPUT_BYTES = 8 * 1024 * 1024
MAX_SVG_SOURCE_PIXELS = 40_000_000


def svg_preview(content: bytes) -> dict:
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_SVG_INPUT_BYTES:
        raise ValueError("invalid_svg_preview_input")
    # This reader runs only in the fixed networkless preview worker. The outer
    # worker owns cancellation and process teardown; no arbitrary callback or
    # caller-supplied path/URL is exposed through its options.
    check = lambda: None
    document = static_svg_document(content, max_pixels=MAX_SVG_SOURCE_PIXELS, check=check)
    png, width, height = render_svg_png(document, max_pixels=MAX_SVG_SOURCE_PIXELS, check=check)
    return {"contract_version": 2, "type": "svg", "reader": "svg", "kind": "image",
            "media_type": "image/png", "data_base64": base64.b64encode(png).decode("ascii"),
            "metadata": {"source_format": "SVG", "width": width, "height": height, "rasterized": True},
            "warnings": [], "sampled": False}
