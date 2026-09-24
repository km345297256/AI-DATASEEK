"""C/native SVG rendering with a hard parent deadline and guaranteed reaping."""
import json
import math
from pathlib import Path
import subprocess
import sys
import time


class SvgRenderError(ValueError):
    def __init__(self, reason):
        self.reason = reason


def render_svg(content: bytes, *, max_pixels: int, check, deadline: float) -> dict:
    check()
    if time.monotonic() >= deadline:
        raise SvgRenderError("validation_deadline")
    if len(content) > 64 * 1024 * 1024:
        raise SvgRenderError("image_size_limit")
    worker = Path(__file__).with_name("svg_render_worker.py")
    try:
        process = subprocess.Popen([sys.executable, str(worker), str(max_pixels),
            str(max(1, math.ceil(deadline - time.monotonic())))], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False)
    except (OSError, ValueError):
        raise SvgRenderError("validator_unavailable") from None
    try:
        pending = content
        while True:
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SvgRenderError("validation_deadline")
            try:
                output, _ = process.communicate(input=pending, timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                pending = None
        check()
        if time.monotonic() >= deadline:
            raise SvgRenderError("validation_deadline")
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
        if set(value) != {"valid", "width", "height"} or any(type(value[key]) is not int or value[key] <= 0
                                                                for key in ("width", "height")):
            raise SvgRenderError("invalid_content")
        return {"format": "SVG", "width": value["width"], "height": value["height"], "frames": 1,
                "validation_level": "safe_static_svg_render"}
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        for pipe in (process.stdin, process.stdout):
            if pipe is not None:
                pipe.close()
