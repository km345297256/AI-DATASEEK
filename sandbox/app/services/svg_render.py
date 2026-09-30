"""Offline SVG rendering with cooperative cancellation and guaranteed reaping.

The native worker owns no wall-clock or CPU deadline. Input is a completed,
private temporary file: cancellation polling must never interrupt a pipe write
and accidentally discard the unsent tail of the document.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

MAX_PREVIEW_PNG_BYTES = 5 * 1024 * 1024


class SvgRenderError(ValueError):
    def __init__(self, reason):
        self.reason = reason


def render_svg(content: bytes, *, max_pixels: int, check) -> dict:
    check()
    if len(content) > 64 * 1024 * 1024:
        raise SvgRenderError("image_size_limit")
    worker = Path(__file__).with_name("svg_render_worker.py")
    try:
        with tempfile.TemporaryFile() as source:
            # Bounded writes keep explicit cancellation responsive during handoff.
            for offset in range(0, len(content), 1024 * 1024):
                check()
                source.write(content[offset:offset + 1024 * 1024])
            source.seek(0)
            check()
            return _render_worker(worker, source, max_pixels, check)
    except OSError:
        # A failed handoff/disk allocation is not malformed user content.
        raise SvgRenderError("validator_unavailable") from None


def render_svg_png(content: bytes, *, max_pixels: int, check) -> tuple[bytes, int, int]:
    """Render a previously validated static tree, never raw browser content.

    Pixels use an anonymous file, not stdout: waiting for a worker to exit while
    it writes a large image to a pipe would deadlock when the pipe fills.
    """
    check()
    if len(content) > 64 * 1024 * 1024:
        raise SvgRenderError("image_size_limit")
    worker = Path(__file__).with_name("svg_render_worker.py")
    try:
        with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as pixels:
            for offset in range(0, len(content), 1024 * 1024):
                check()
                source.write(content[offset:offset + 1024 * 1024])
            source.seek(0)
            metadata = _render_worker(worker, source, max_pixels, check, pixels=pixels)
            check()
            pixels.seek(0)
            content = pixels.read(MAX_PREVIEW_PNG_BYTES + 1)
            if (len(content) != metadata["png_size"] or len(content) > MAX_PREVIEW_PNG_BYTES
                    or not content.startswith(b"\x89PNG\r\n\x1a\n")):
                raise SvgRenderError("invalid_content")
            return content, metadata["width"], metadata["height"]
    except OSError:
        raise SvgRenderError("validator_unavailable") from None


def _render_worker(worker, source, max_pixels, check, *, pixels=None):
    command = [sys.executable, str(worker), str(max_pixels)]
    kwargs = {}
    if pixels is not None:
        command.append(str(pixels.fileno()))
        kwargs["pass_fds"] = (pixels.fileno(),)
    try:
        process = subprocess.Popen(command, stdin=source,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False, **kwargs)
    except (OSError, ValueError):
        raise SvgRenderError("validator_unavailable") from None
    try:
        while True:
            check()
            try:
                # This is a cancellation observation interval, not an execution
                # deadline. A healthy slow renderer can keep running forever.
                process.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
        check()
        # Only the fixed worker's small metadata JSON reaches stdout. The
        # renderer redirects native diagnostics and never emits image bytes.
        output = process.stdout.read(1025)
        if process.returncode or len(output) > 1024:
            raise SvgRenderError("invalid_content")
        try:
            value = json.loads(output)
        except (ValueError, UnicodeDecodeError):
            raise SvgRenderError("validator_unavailable") from None
        if not isinstance(value, dict):
            raise SvgRenderError("validator_unavailable")
        if value.get("valid") is not True:
            reason = value.get("reason")
            raise SvgRenderError(reason if reason in {"invalid_content", "image_size_limit", "validator_unavailable"}
                                 else "invalid_content")
        fields = {"valid", "width", "height"} | ({"png_size"} if pixels is not None else set())
        if set(value) != fields or any(type(value[key]) is not int or value[key] <= 0
                                                                for key in ("width", "height")):
            raise SvgRenderError("invalid_content")
        if pixels is not None:
            if (max(value["width"], value["height"]) > 2048 or type(value["png_size"]) is not int
                    or not 0 < value["png_size"] <= MAX_PREVIEW_PNG_BYTES):
                raise SvgRenderError("image_size_limit")
            return {key: value[key] for key in ("width", "height", "png_size")}
        return {"format": "SVG", "width": value["width"], "height": value["height"], "frames": 1,
                "validation_level": "safe_static_svg_render"}
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process.stdout is not None:
            process.stdout.close()
