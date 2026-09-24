"""Static SVG artifact checks followed by bounded, offline rendering.

SVG is XML/vector content, not a Pillow raster. No script, stylesheet import,
external resource or XML entity is evaluated. The renderer sees only the parsed
and validated tree, without its original DTD or processing instructions.
"""
from __future__ import annotations

import base64
import io
import re
import time
import warnings
from typing import Callable
from app.services.svg_styles import (
    SvgStyleError, apply_static_styles, enforce_visibility, local_reference, resource_value, xml_name,
)
from app.services.svg_render import SvgRenderError, render_svg


SVG_NS = "http://www.w3.org/2000/svg"
MAX_ELEMENTS = 100_000
MAX_DEPTH = 128
_DRAWABLE = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "image", "use"}
_FORBIDDEN = {"script", "foreignobject", "iframe", "object", "embed", "audio", "video",
              "animate", "animatemotion", "animatetransform", "set", "discard", "handler"}
_METADATA_NS = {"http://www.w3.org/1999/02/22-rdf-syntax-ns#", "http://purl.org/dc/elements/1.1/",
                "http://creativecommons.org/ns#"}
_RESOURCE_ATTRIBUTES = {"style", "fill", "stroke", "filter", "clip-path", "mask", "cursor",
                        "marker", "marker-start", "marker-mid", "marker-end"}


class InvalidSvg(ValueError):
    def __init__(self, reason: str = "invalid_content", diagnostics: dict | None = None):
        self.reason = reason
        self.diagnostics = diagnostics or {}


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _embedded_raster(value: str, max_pixels: int, check: Callable[[], None]) -> tuple[int, str]:
    from PIL import Image
    match = re.fullmatch(r"data:image/(png|jpeg|gif|webp);base64,([A-Za-z0-9+/=\s]+)", value, re.I)
    if not match:
        raise InvalidSvg()
    payload = base64.b64decode(re.sub(r"\s", "", match[2]), validate=True)
    expected = {"png": "PNG", "jpeg": "JPEG", "gif": "GIF", "webp": "WEBP"}[match[1].lower()]
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(payload)) as image:
            if image.format != expected:
                raise InvalidSvg("format_mismatch")
            if image.width * image.height > max_pixels:
                raise InvalidSvg("image_size_limit")
            image.verify()
        with Image.open(io.BytesIO(payload)) as image:
            check()
            # Static scientific SVG images do not need embedded animations.
            if getattr(image, "n_frames", 1) != 1:
                raise InvalidSvg()
            image.load()
            # MuPDF SVG only accepts PNG/JPEG data URLs. Normalize every allowed
            # static raster locally so supported GIF/WebP pixels are actually
            # rendered, not silently omitted. The original artifact is unchanged.
            normalized = io.BytesIO()
            image.convert("RGBA").save(normalized, format="PNG")
            check()
            return image.width * image.height, "data:image/png;base64," + base64.b64encode(normalized.getvalue()).decode("ascii")


def svg_metadata(content: bytes, *, max_pixels: int, check: Callable[[], None], deadline: float | None = None) -> dict:
    from defusedxml import ElementTree as SafeTree
    from defusedxml.common import DefusedXmlException
    from xml.etree import ElementTree

    deadline = time.monotonic() + 30 if deadline is None else deadline
    depth = count = metadata_depth = embedded_pixels = 0
    ids: dict = {}
    references: list[str] = []
    try:
        # Matplotlib emits a standard public SVG DOCTYPE. Parsing it is safe
        # with entities/external access forbidden; it is never sent to MuPDF.
        parser = SafeTree.iterparse(io.BytesIO(content), events=("start", "end", "pi"),
                                    forbid_dtd=False, forbid_entities=True, forbid_external=True)
        for event, node in parser:
            check()
            if event == "pi":
                raise InvalidSvg()
            local = _local_name(node.tag).lower()
            if event == "start":
                depth += 1
                count += 1
                if depth > MAX_DEPTH or count > MAX_ELEMENTS or len(node.attrib) > 128:
                    raise InvalidSvg("image_size_limit")
                if count == 1 and node.tag not in {"svg", "{" + SVG_NS + "}svg"}:
                    raise InvalidSvg("format_mismatch")
                namespace = node.tag[1:].split("}", 1)[0] if node.tag.startswith("{") else ""
                if local in _FORBIDDEN:
                    raise InvalidSvg()
                if local == "metadata":
                    metadata_depth += 1
                if namespace not in {"", SVG_NS} and not (metadata_depth and namespace in _METADATA_NS):
                    raise InvalidSvg()
                for key, value in node.attrib.items():
                    name = _local_name(key).lower()
                    if name.startswith("on") or name in {"base", "src"}:
                        raise InvalidSvg()
                    if name == "id":
                        if not xml_name(value) or value in ids:
                            raise InvalidSvg()
                        ids[value] = node
                    if name == "href":
                        if local_reference(value):
                            references.append(value[1:])
                        elif local in {"image", "feimage"}:
                            pixels, normalized = _embedded_raster(value, max_pixels - embedded_pixels, check)
                            embedded_pixels += pixels
                            node.set(key, normalized)
                            if embedded_pixels > max_pixels:
                                raise InvalidSvg("image_size_limit")
                        else:
                            raise InvalidSvg()
                    if name != "style" and (name in _RESOURCE_ATTRIBUTES or re.search(r"url\s*\(", value, re.I)):
                        resource_value(value, references)
            else:
                if local == "metadata":
                    metadata_depth -= 1
                depth -= 1
        root = parser.root
        apply_static_styles(root, references, check)
    except (DefusedXmlException, ElementTree.ParseError, SvgStyleError):
        raise InvalidSvg() from None
    if any(target not in ids for target in references):
        raise InvalidSvg()
    # Bound reference expansion as well as physical XML nodes. A tiny recursive
    # defs/use graph must not send the renderer into a recursion/resource loop.
    pending = [(root, frozenset(), 0)]
    expansions = 0
    while pending:
        check()
        node, active, nesting = pending.pop()
        expansions += 1
        if expansions > MAX_ELEMENTS or nesting > MAX_DEPTH:
            raise InvalidSvg("image_size_limit")
        pending.extend((child, active, nesting + 1) for child in node)
        if _local_name(node.tag).lower() == "use":
            target = next((value[1:] for key, value in node.attrib.items()
                           if _local_name(key).lower() == "href" and value.startswith("#")), None)
            if target is None or target not in ids or target in active:
                raise InvalidSvg()
            pending.append((ids[target], active | {target}, nesting + 1))
    check()
    try:
        if not enforce_visibility(root, _DRAWABLE, check):
            raise InvalidSvg()
    except SvgStyleError:
        raise InvalidSvg() from None
    # Serializing removes the DTD and all XML parser directives. SVG attributes
    # have already been checked, including decoded character references.
    try:
        return render_svg(ElementTree.tostring(root, encoding="utf-8"), max_pixels=max_pixels,
                          check=check, deadline=deadline)
    except SvgRenderError as error:
        raise InvalidSvg(error.reason) from None
